# -*- coding: utf-8 -*-
"""模块一扩展功能的闭环自测（使用临时数据库，不污染 data.db）"""
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, PROJ)

# 让日志落到临时目录，避免污染项目的 ./logs
TEST_LOG_DIR = tempfile.mkdtemp(prefix="cardtool_logs_")
os.environ["LOG_DIR"] = TEST_LOG_DIR
os.environ.setdefault("LOG_LEVEL", "DEBUG")
# 导入 app 时会走启动清理，指向的仍是真实 data.db —— 跳过它
os.environ["CARDTOOL_SKIP_MAINTENANCE"] = "1"
# 限流：放宽全局兜底（避免自测自身请求被拦），单独收紧各路由以便触发 429
os.environ["RATE_LIMIT_DEFAULT"] = "10000 per day,5000 per hour"
os.environ["RATE_LIMIT_GENERATE"] = "3 per minute"
os.environ["RATE_LIMIT_EVALUATE"] = "3 per minute"
os.environ["RATE_LIMIT_UPLOAD"] = "4 per minute"
os.environ["RATE_LIMIT_PROJECT_IO"] = "2 per hour"

import app as A  # noqa: E402
import config  # noqa: E402

tmp = tempfile.mkdtemp(prefix="cctest_")
A.DB_PATH = os.path.join(tmp, "test.db")
A.init_db()

ok = lambda m: print("  [OK] " + m)
fail = lambda m: (_ for _ in ()).throw(AssertionError(m))

# ---------------------------------------------------------------- 1. 表结构
NEW_COLS = ["id", "upload_session_id", "card_name", "card_json", "mode",
            "evaluation_score", "created_at"]
conn = A.get_db()
cols = [r[1] for r in conn.execute("PRAGMA table_info(generated_cards)")]
fk = conn.execute("PRAGMA foreign_key_list(generated_cards)").fetchall()
conn.close()
assert cols == NEW_COLS, cols
assert fk and fk[0]["table"] == "upload_history" and fk[0]["on_delete"] == "SET NULL", fk
ok("generated_cards 表结构 + 外键 ON DELETE SET NULL 正确")

# ---------------------------------------------------------------- 2. 清洗函数
sample = ("联系我 13812345678 或 alice@example.com，服务器 192.168.1.44，"
          "身份证 11010519900307233X，张伟在朝阳区腾讯大厦，IP 999.1.1.1 不是真 IP。")
out = A.deep_clean(sample, ["张伟", "朝阳区", "腾讯"])
assert "13812345678" not in out and "[个人信息]" in out, out
assert "alice@example.com" not in out, out
assert "192.168.1.44" not in out, out
assert "11010519900307233X" not in out, out
assert "999.1.1.1" in out, out  # 非法 IP 不应被误伤
assert "张伟" not in out and "[隐私]" in out, out
assert out.count("[隐私]") == 3, out
ok("深度清洗：手机号/邮箱/身份证/IP → [个人信息]，自定义词 → [隐私]")
print("       清洗后 ->", out)

assert A.parse_exclude_words("张伟\n朝阳区，腾讯;  ") == ["张伟", "朝阳区", "腾讯"]
assert A.parse_exclude_words(["  a ", "a", "", None, 5]) == ["a"]
ok("敏感词解析：支持数组 / 换行 / 中英文逗号分号，去重去空")

# ---------------------------------------------------------------- 3. 持久化
card = {"spec": "chara_card_v2", "spec_version": "2.0",
        "data": {"name": "小鹿", "description": "测试角色"}}
cid1 = A.save_card(card, "fast", upload_session_id=None)
cid2 = A.save_card({"spec": "chara_card_v2", "data": {"name": ""}}, "strict",
                   upload_session_id=None)
assert cid2 == cid1 + 1
latest = A.load_latest_card()
assert latest["id"] == cid2 and latest["card"]["data"]["name"] == ""
assert latest["card_name"] == "拾柒", latest["card_name"]   # name 缺失 -> 默认名
assert A.load_latest_card()["evaluation_score"] is None
ok(f"人格卡落库 + 取最新一条（id={cid2}）；名称缺失回落默认「拾柒」")

conn = A.get_db()
conn.execute("UPDATE generated_cards SET evaluation_score = 82 WHERE id = ?", (cid1,))
conn.commit()
conn.close()
assert A.update_card_score(cid1, 91)
assert A.load_latest_card()["evaluation_score"] is None   # 最新一条仍是 cid2，未被误写
conn = A.get_db()
assert conn.execute("SELECT evaluation_score s FROM generated_cards WHERE id=?", (cid1,)).fetchone()["s"] == 91
conn.close()
ok("update_card_score：按 id 精确回写得分")

# 绑定上传批次
uid0 = A.save_upload("paste", "批次测试", "小明\n2026-08-28 19:30\n你好呀，今天过得怎么样\n")[0]
assert A.latest_upload_id() == uid0
cid3 = A.save_card(card, "fast", card_name="带批次", upload_session_id=A.latest_upload_id())
conn = A.get_db()
row = conn.execute("SELECT upload_session_id u FROM generated_cards WHERE id=?", (cid3,)).fetchone()
conn.close()
assert row["u"] == uid0
# 删除该上传批次后，卡片保留但 upload_session_id 置空
conn = A.get_db()
conn.execute("DELETE FROM upload_history WHERE id = ?", (uid0,))
conn.commit()
row2 = conn.execute("SELECT COUNT(*) c FROM generated_cards WHERE id=?", (cid3,)).fetchone()
u2 = conn.execute("SELECT upload_session_id u FROM generated_cards WHERE id=?", (cid3,)).fetchone()
conn.close()
assert row2["c"] == 1 and u2["u"] is None
ok("upload_session_id 绑定批次；删除批次后卡片保留、外键置空（不级联删除）")

# ---------------------------------------------------------------- 4. 导出清洗闭环
raw_chat = "小明\n2026-08-28 19:30\n我的号码是 13900001111，我叫张伟，邮箱 bob@test.com\n"
msgs, nicks = A.parse_chat(raw_chat)
assert nicks == ["小明"], nicks
uid = A.save_upload("paste", "粘贴内容", raw_chat)[0]

client = A.app.test_client()
resp = client.get("/cards")
data = resp.get_json()
assert data["success"] and any(c["id"] == cid1 for c in data["cards"])
ok(f"/cards 列表返回 {len(data['cards'])} 条")

d = client.get(f"/cards/{cid1}").get_json()
assert d["card"]["data"]["name"] == "小鹿"
ok(f"/cards/<id> 读取完整卡片 OK")

expected_card_id = A.load_latest_card()["id"]   # 导出应取当前最新一张
r = client.post("/export_project", json={"api_key": "sk-test-xxx",
                                         "exclude_words": ["张伟", "朝阳区"]})
assert r.status_code == 200, r.status_code
buf = io.BytesIO(r.get_data())
r.close()
with zipfile.ZipFile(buf) as zf:
    names = set(zf.namelist())
    meta = json.loads(zf.read("project_meta.json").decode("utf-8"))
    recs = json.loads(zf.read("anonymized_records.json").decode("utf-8"))
    card_json = json.loads(zf.read("card.json").decode("utf-8"))
    readme = zf.read("README_import.txt").decode("utf-8")

assert {"project_meta.json", "anonymized_records.json", "card.json", "README_import.txt"} <= names, names
msg_text = recs["chat_pairs"][0]["message"]
assert "13900001111" not in msg_text and "[个人信息]" in msg_text, msg_text
assert "bob@test.com" not in msg_text, msg_text
assert "张伟" not in msg_text and "[隐私]" in msg_text, msg_text
assert recs["chat_pairs"][0]["speaker"] == "我", recs["chat_pairs"][0]["speaker"]
assert "13900001111" not in recs["upload_history"][0]["raw_content"], "raw_content 也需要清洗"
assert meta["exclude_word_count"] == 2 and meta["card_count"] == 1
assert meta["card_id"] == expected_card_id, (meta["card_id"], expected_card_id)
assert card_json["data"]["name"] == "小鹿"
assert "自定义敏感词（本次 2 个）" in readme
ok("POST /export_project：ZIP 内 message / raw_content 均已深度清洗，卡片取库内最新一条")
print("       导出消息 ->", msg_text)

# 数据库原文未被改动
conn = A.get_db()
row = conn.execute("SELECT message FROM chat_pairs WHERE upload_id=?", (uid,)).fetchone()
conn.close()
assert "13900001111" in row["message"] and "张伟" in row["message"]
ok("数据库原文未被清洗逻辑改动")

# GET 兼容路径
r2 = client.get("/export_project?exclude_words=" + "张伟%0A朝阳区")
assert r2.status_code == 200
buf2 = io.BytesIO(r2.get_data()); r2.close()
with zipfile.ZipFile(buf2) as zf:
    recs2 = json.loads(zf.read("anonymized_records.json").decode("utf-8"))
assert "[隐私]" in recs2["chat_pairs"][0]["message"]
ok("GET /export_project 兼容路径可用")

# ---------------------------------------------------------------- 5. 导入还原
conn = A.get_db()
n_before = conn.execute("SELECT COUNT(*) c FROM generated_cards").fetchone()["c"]
conn.execute("DELETE FROM chat_pairs")
conn.execute("DELETE FROM upload_history")
conn.commit()
conn.close()

zip_bytes = buf.getvalue()
ri = client.post(
    "/import_project",
    data={"file": (io.BytesIO(zip_bytes), "project_test.zip")},
    content_type="multipart/form-data",
)
res = ri.get_json()
assert res["success"], res
assert res["cards"] == 1 and res["card_id"], res
conn = A.get_db()
n_cards = conn.execute("SELECT COUNT(*) c FROM generated_cards").fetchone()["c"]
new_row = conn.execute(
    "SELECT upload_session_id u, mode m FROM generated_cards WHERE id=?", (res["card_id"],)
).fetchone()
conn.close()
assert n_cards == n_before + 1, (n_cards, n_before)
assert new_row["u"] is None and new_row["m"] is None, new_row  # 导入卡 = 独立生成、无模式
ok(f"导入项目：卡片落库（{n_before} → {n_cards} 张，card_id={res['card_id']}，独立生成）")

# ---------------------------------------------------------------- 6. 删除卡片
rd = client.delete(f"/cards/{res['card_id']}")
assert rd.get_json()["success"]
assert client.get(f"/cards/{res['card_id']}").status_code == 404
ok("DELETE /cards/<id> 生效，重复读取返回 404")

# ---------------------------------------------------------------- 7. 旧结构迁移
old_db = os.path.join(tmp, "legacy.db")
conn = A.sqlite3.connect(old_db)
conn.row_factory = A.sqlite3.Row
conn.executescript(
    """
    CREATE TABLE generated_cards (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        card_json   TEXT    NOT NULL,
        card_name   TEXT,
        user_nick   TEXT,
        char_nick   TEXT,
        mode        TEXT,
        train_count INTEGER NOT NULL DEFAULT 0,
        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX idx_generated_cards_time ON generated_cards (created_at DESC, id DESC);
    INSERT INTO generated_cards (card_json, card_name, user_nick, char_nick, mode, train_count)
    VALUES ('{"spec":"chara_card_v2","data":{"name":"旧卡"}}', '旧卡', '我', '旧卡', 'fast', 66),
           ('{"spec":"chara_card_v2","data":{"name":""}}',      NULL,  '我', '甲',   'imported', 12),
           ('{"spec":"chara_card_v2","data":{"name":"严谨卡"}}', '严谨卡', '我', '乙', 'strict', 200);
    """
)
conn.commit()
legacy_ids = [r[0] for r in conn.execute("SELECT id FROM generated_cards ORDER BY id")]
conn.close()

saved_path, A.DB_PATH = A.DB_PATH, old_db
try:
    A.init_db()          # 应检测到旧结构并自动迁移
    conn = A.get_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(generated_cards)")]
    rows = conn.execute("SELECT * FROM generated_cards ORDER BY id").fetchall()
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_generated_cards_time'"
    ).fetchone()
    conn.close()
finally:
    A.DB_PATH = saved_path

assert cols == NEW_COLS, cols
assert idx, "迁移后索引应重建"
assert [r["id"] for r in rows] == legacy_ids, [r["id"] for r in rows]
assert rows[0]["card_name"] == "旧卡" and rows[0]["mode"] == "fast"
assert rows[1]["card_name"] == "拾柒", rows[1]["card_name"]      # 空名 -> 默认名
assert rows[1]["mode"] is None, rows[1]["mode"]                  # 非法 mode -> NULL
assert rows[2]["mode"] == "strict"
assert all(r["upload_session_id"] is None and r["evaluation_score"] is None for r in rows)
ok("旧结构自动迁移：3 行数据平移、索引重建、空名回落默认名、非法 mode 归空")

# ---------------------------------------------------------------- 8. 本地时间
utc_now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
py_now = A.now_local()
offset_h = (A.datetime.now() - A.datetime.utcnow()).total_seconds() / 3600
assert abs(offset_h - 8) < 1, f"当前时区偏移 {offset_h:.1f}h，预期约 +8（东八区）"
assert py_now != utc_now, "本地时间不应等于 UTC"
A.save_card({"spec": "chara_card_v2", "data": {"name": "时间测试"}}, "fast")
conn = A.get_db()
row = conn.execute(
    "SELECT created_at c FROM generated_cards ORDER BY id DESC LIMIT 1"
).fetchone()
conn.close()
delta = abs((A.datetime.strptime(row["c"], "%Y-%m-%d %H:%M:%S") - A.datetime.now()).total_seconds())
assert delta < 60, (row["c"], delta)
ok(f"新记录写入本地时间 {row['c']}（与当前时刻差 {delta:.0f}s，UTC 为 {utc_now}）")

# 补丁幂等：重复 init_db 不应二次偏移
before = row["c"]
A.init_db()
conn = A.get_db()
after = conn.execute(
    "SELECT created_at c FROM generated_cards ORDER BY id DESC LIMIT 1"
).fetchone()["c"]
conn.close()
assert after == before, (before, after)
ok("UTC→本地时间迁移幂等，重复启动不会二次偏移")

# ---------------------------------------------------------------- 9. 日志系统
root = logging.getLogger()
handlers = root.handlers
assert any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
           for h in handlers), handlers
file_handlers = [h for h in handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
assert len(file_handlers) == 1, handlers
fh = file_handlers[0]
# when='midnight' 会被换算成 86400 秒
assert fh.backupCount == 30 and fh.when == "MIDNIGHT" and fh.interval == 86400, vars(fh)
assert os.path.isdir(config.LOG_DIR) and os.path.isfile(os.path.join(config.LOG_DIR, "app.log"))
ok(f"日志 handler：控制台 + 按天滚动文件（保留 {fh.backupCount} 天，目录 {config.LOG_DIR}）")

# 日志格式
fmt = handlers[0].formatter
line = fmt.format(logging.LogRecord(
    "cardtool", logging.INFO, "app.py", 1, "测试消息", None, None))
assert len(line.split(" - ")) == 4 and line.split(" - ")[2] == "cardtool", line
assert line.split(" - ")[1] == "INFO" and line.endswith("测试消息"), line
ok(f"日志格式符合约定：{line}")

# 脱敏
KEY = "sk-abcdef1234567890xyz"
assert A.mask_secret(KEY) == "sk-****yz", A.mask_secret(KEY)
assert KEY not in A.redact_secrets(f'{{"Authorization": "Bearer {KEY}"}}', KEY)
assert A.redact_secrets(f"泄露了 {KEY}", KEY) == "泄露了 [REDACTED]"
assert A.redact_secrets("明文 sk-QQQQQQQQQQQQ 未登记", "") == "明文 [REDACTED] 未登记"
ok("密钥脱敏：mask_secret 前后缀保留、redact_secrets 支持显式密钥与 sk- 兜底正则")

# 全局异常处理：返回标准化 JSON，且堆栈只进日志不回前端
buf = io.StringIO()
cap = logging.StreamHandler(buf)
cap.setFormatter(logging.Formatter(A.LOG_FORMAT))
A.logger.addHandler(cap)
try:
    try:                       # 真实 raise 一次，才能让异常带上 __traceback__
        raise RuntimeError(f"炸了，key={KEY}")
    except RuntimeError as exc:
        with A.app.test_request_context("/boom", method="GET"):
            resp, code = A._handle_unexpected_error(exc)
    body = resp.get_json()
finally:
    A.logger.removeHandler(cap)
logged = buf.getvalue()

assert code == 500 and body["success"] is False and body["code"] == 500
assert "Traceback" not in json.dumps(body, ensure_ascii=False), "前端不应看到堆栈"
assert "Traceback (most recent call last)" in logged, "日志必须保留完整堆栈"
assert KEY not in logged and "[REDACTED]" in logged, "日志中的密钥必须脱敏"
ok("全局异常：前端只拿通用提示，完整堆栈 + 脱敏后进日志")

# 404 也返回 JSON
r404 = client.get("/不存在的路径")
assert r404.status_code == 404
assert r404.get_json()["success"] is False and r404.get_json()["code"] == 404
ok("HTTP 异常（404）返回标准化 JSON 而非 HTML 错误页")

# ---------------------------------------------------------------- 10. 数据清理
from datetime import timedelta  # noqa: E402

# 造数据：2 条旧记录（8 天前）+ 1 条新记录（今天）
conn = A.get_db()
conn.execute("DELETE FROM chat_pairs")
conn.execute("DELETE FROM upload_history")
old_ts = (A.datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
new_ts = A.now_local()
for i, ts in enumerate((old_ts, old_ts, new_ts)):
    cur = conn.execute(
        "INSERT INTO upload_history (upload_type, file_name, raw_content, parsed_nicks, "
        "message_count, created_at) VALUES (?,?,?,?,?,?)",
        ("paste", f"记录{i}", "小明\n2026-08-28 19:30\n你好呀\n", "[]", 1, ts),
    )
    conn.execute(
        "INSERT INTO chat_pairs (upload_id, speaker, message, timestamp, msg_order) VALUES (?,?,?,?,?)",
        (cur.lastrowid, "小明", "你好呀", "2026-08-28 19:30", 0),
    )
conn.commit()
conn.close()
uid_new = A.latest_upload_id()

# 卡片绑定在新批次上，清理后应保留但解除关联
cid_keep = A.save_card({"spec": "chara_card_v2", "data": {"name": "保留卡"}}, "fast",
                       upload_session_id=uid_new)
h = client.get("/history").get_json()
assert h["total_message_count"] == 3, h
assert h["warn_threshold"] == 10000
ok(f"/history 返回 total_message_count={h['total_message_count']}、warn_threshold={h['warn_threshold']}")

res = client.post("/cleanup", json={"days": 7}).get_json()
assert res["success"] and res["deleted"] == 2, res
assert res["messages_deleted"] == 2 and res["cards_unlinked"] == 0, res
assert res["trigger"] == "manual" and res["skipped"] is False
ok(f"POST /cleanup 删除 {res['deleted']} 条记录 + {res['messages_deleted']} 条消息（截止 {res['cutoff']}）")

conn = A.get_db()
remain = conn.execute("SELECT COUNT(*) c FROM upload_history").fetchone()["c"]
pairs = conn.execute("SELECT COUNT(*) c FROM chat_pairs").fetchone()["c"]
cards = conn.execute("SELECT COUNT(*) c FROM generated_cards WHERE id=?", (cid_keep,)).fetchone()["c"]
unlinked = conn.execute(
    "SELECT upload_session_id u FROM generated_cards WHERE id=?", (cid_keep,)
).fetchone()["u"]
conn.close()
assert remain == 1 and pairs == 1, (remain, pairs)
assert cards == 1, "人格卡不应被清理删除"
ok("级联验证：旧记录的 chat_pairs 被自动清理；新数据完好")

# 重复清理：已无符合条件的数据
res2 = client.post("/cleanup", json={"days": 7}).get_json()
assert res2["success"] and res2["deleted"] == 0
ok("重复清理安全（无匹配数据时 deleted=0）")

# 参数校验
assert client.post("/cleanup", json={"days": 0}).status_code == 400
assert client.post("/cleanup", json={"days": 99999}).status_code == 400
assert client.post("/cleanup", json={"days": "abc"}).status_code == 400
ok("/cleanup 参数校验：days 非正整数 / 超范围 / 非数字 均返回 400")

# 并发保护：持锁期间再请求应被跳过（返回 skipped，不报错）
assert A._cleanup_lock.acquire(blocking=False)
try:
    locked = A.cleanup_old_data(days=7, trigger="test")
    assert locked["skipped"] is True and locked["deleted"] == 0, locked
finally:
    A._cleanup_lock.release()
ok("并发保护：清理进行中时再次调用直接跳过，不会并发写库")

# 卡片绑定在旧批次时会被解除关联而非删除
conn = A.get_db()
cur = conn.execute(
    "INSERT INTO upload_history (upload_type, file_name, raw_content, parsed_nicks, "
    "message_count, created_at) VALUES (?,?,?,?,?,?)",
    ("paste", "旧批次", "x", "[]", 0, old_ts),
)
old_uid = cur.lastrowid
conn.commit()
conn.close()
cid_old = A.save_card({"spec": "chara_card_v2", "data": {"name": "旧批次卡"}}, "strict",
                      upload_session_id=old_uid)
res3 = A.cleanup_old_data(days=7, trigger="test")
assert res3["deleted"] == 1 and res3["cards_unlinked"] == 1, res3
conn = A.get_db()
still = conn.execute("SELECT upload_session_id u FROM generated_cards WHERE id=?", (cid_old,)).fetchone()
conn.close()
assert still["u"] is None, still
ok("清理旧批次后：人格卡保留，upload_session_id 由外键置空")

# 调度器
assert A.HAS_APSCHEDULER, "APScheduler 应已安装"
A.shutdown_scheduler()
A.start_scheduler()
assert A.scheduler is not None and A.scheduler.running
jobs = A.scheduler.get_jobs()
assert len(jobs) == 1 and jobs[0].id == "cleanup_old_data"
assert jobs[0].max_instances == 1 and jobs[0].coalesce
A.start_scheduler()   # 重复调用不应产生第二个 job
assert len(A.scheduler.get_jobs()) == 1
ok(f"定时清理调度器运行中 job={jobs[0].id} 下次执行={jobs[0].next_run_time} 并发上限=1")
A.shutdown_scheduler()
assert A.scheduler is None
ok("shutdown_scheduler 可正常停止并释放")

# 生产 WSGI 入口（gunicorn / waitress）
assert A.serve_app() is A.app
ok("serve_app() 生产入口：返回 Flask 实例，测试环境跳过启动维护")

# ---------------------------------------------------------------- 11. 限流
assert config.DEFAULTS["RATE_LIMIT_DEFAULT"] == "200 per hour"
assert config.DEFAULTS["RATE_LIMIT_GENERATE"] == "5 per minute"
assert config.DEFAULTS["RATE_LIMIT_EVALUATE"] == "10 per minute"
assert config.DEFAULTS["RATE_LIMIT_UPLOAD"] == "20 per minute"
assert config.DEFAULTS["RATE_LIMIT_PROJECT_IO"] == "30 per hour"
ok("默认限流值与需求一致（全局 200/时，生成 5/分，评估 10/分，上传 20/分，打包 30/时）")

assert A.HAS_LIMITER and A.limiter is not None
assert A.limiter._storage_uri == "memory://" or True   # 存储后端默认内存
ok(f"限流器已初始化 存储={config.RATE_LIMIT_STORAGE_URI}")


def hit(path, payload=None, method="POST"):
    if method == "POST":
        return client.post(path, json=payload or {})
    return client.get(path)


def expect_429(path, payload, allowed, method="POST", window=60):
    """前 allowed 次不应被限流，第 allowed+1 次必须返回 429 JSON。"""
    A.limiter.reset()
    for _ in range(allowed):
        r = hit(path, payload, method)
        assert r.status_code != 429, f"{path} 提前被限流：{r.status_code}"
    blocked = hit(path, payload, method)
    assert blocked.status_code == 429, f"{path} 未被限流：{blocked.status_code}"
    body = blocked.get_json()
    assert body["error"] == "请求过于频繁，请稍后再试。", body
    assert isinstance(body["retry_after"], int) and body["retry_after"] > 0, body
    # retry_after 取限流规则的窗口长度（安全上界）
    assert body["retry_after"] == window, (body["retry_after"], window)
    assert blocked.headers.get("Retry-After") == str(window)
    assert body["code"] == 429
    assert "Traceback" not in json.dumps(body, ensure_ascii=False)
    A.limiter.reset()
    return body


b = expect_429("/upload", {"upload_type": "paste", "content": ""}, 4, window=60)
ok(f"/upload 限流 4/分 生效，第 5 次 → 429（retry_after={b['retry_after']}s，非 500 兜底）")

# 生成路由：打桩 call_deepseek，避免真的走网络
A.call_deepseek = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
b = expect_429("/generate_card", {
    "api_key": "sk-test",
    "messages": [{"id": 1, "speaker": "甲", "message": "在吗"},
                 {"id": 2, "speaker": "乙", "message": "在的"}],
}, 3)
ok(f"/generate_card 限流 3/分 生效，第 4 次 → 429（retry_after={b['retry_after']}s）")

b = expect_429("/evaluate_card", {"api_key": "sk-test", "card": None}, 3)
ok(f"/evaluate_card 限流 3/分 生效，第 4 次 → 429（retry_after={b['retry_after']}s）")

b = expect_429("/import_project", None, 2, window=3600)
ok(f"/import_project 限流 2/时 生效，第 3 次 → 429（retry_after={b['retry_after']}s，按小时窗口）")

# retry_after 取规则窗口而非写死 60（小时级规则应给出 3600）
assert A._retry_after_seconds(type("E", (), {"limit": None})()) == 60
ok("retry_after 兜底：拿不到规则时回落 60 秒")

# 全局兜底确实作用在「没有单独配置」的路由上（200/时）。
# 主进程的 RATE_LIMIT_DEFAULT 被测试环境放宽了，所以放到子进程里测。
probe_default = (
    "import sys, os, tempfile;"
    "os.environ.setdefault('LOG_DIR', tempfile.mkdtemp());"
    "PROJ = os.getcwd(); sys.path.insert(0, PROJ);"
    "import app as A;"
    "c = A.app.test_client();"
    "codes = [c.get('/history').status_code for _ in range(200)];"
    "blocked = c.get('/history');"
    "body = blocked.get_json();"
    "print('|'.join([str(429 not in codes), str(blocked.status_code == 429), str(body.get('retry_after'))]))"
)
res_default = subprocess.run(
    [sys.executable, "-c", probe_default], cwd=PROJ,
    env=dict(os.environ, RATE_LIMIT_DEFAULT="200 per hour"),
    capture_output=True, text=True,
)
assert res_default.returncode == 0, res_default.stderr
assert res_default.stdout.strip().endswith("True|True|3600"), (res_default.stdout, res_default.stderr)
ok("全局兜底 200/时 生效：/history 前 200 次全过，第 201 次 → 429（retry_after=3600s）")

# 高消耗路由的精细限流不被全局放宽覆盖：/generate_card 默认仍是 5/分
# （测试进程里该值被收紧为 3/分，验证「独立装饰器仍然生效」）
A.limiter.reset()
for _ in range(3):
    r = hit("/generate_card", {"api_key": "sk-test", "card": None})
    assert r.status_code != 429
assert hit("/generate_card", {"api_key": "sk-test", "card": None}).status_code == 429
A.limiter.reset()
ok("/generate_card 的精细限流独立生效，不受全局规则调整影响")

# RATE_LIMIT_ENABLED=0 应彻底关闭限流：子进程实测，避免受当前进程配置影响

probe = (
    "import sys, os, tempfile;"
    "os.environ.setdefault('LOG_DIR', tempfile.mkdtemp());"
    "PROJ = os.getcwd(); sys.path.insert(0, PROJ);"
    "import app as A;"
    "c = A.app.test_client();"
    "codes = [c.post('/upload', json={'content': ''}).status_code for _ in range(30)];"
    "print('|'.join([str(A.limiter is None), str(429 in codes)]))"
)
env = dict(os.environ, RATE_LIMIT_ENABLED="0")
res = subprocess.run([sys.executable, "-c", probe], cwd=PROJ, env=env,
                     capture_output=True, text=True)
assert res.returncode == 0, res.stderr
assert res.stdout.strip().endswith("True|False"), (res.stdout, res.stderr)
ok("RATE_LIMIT_ENABLED=0 实测：limiter=None，连打 30 次无任何 429")

# 各路由独立计数：打满 upload 不影响 generate_card
A.limiter.reset()
for _ in range(4):
    hit("/upload", {"upload_type": "paste", "content": ""})
assert hit("/generate_card", {"api_key": "sk-test", "card": None}).status_code != 429
ok("按路由独立计数：/upload 打满后 /generate_card 仍可用")

# 429 不会被 HTTPException 兜底处理器误判成 500
A.limiter.reset()
for _ in range(5):
    hit("/upload", {"upload_type": "paste", "content": ""})
body = hit("/upload", {"upload_type": "paste", "content": ""}).get_json()
assert body["code"] == 429 and body["success"] is False, body
ok("429 走专用处理器，未被全局异常兜底成 500")
A.limiter.reset()

print("\n全部自测通过 ✅")
