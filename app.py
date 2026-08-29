# -*- coding: utf-8 -*-
"""
人格卡生成工具 · Flask 后端

功能：
1. 解析"昵称 / 时间戳 / 内容"三行格式的聊天记录
2. 将原始内容与解析后的对话对持久化到 SQLite（data.db）
3. 合并所有历史对话，调用 DeepSeek API 生成 SillyTavern V2 (chara_card_v2) 人格卡

注意：API Key 由前端传入，仅在本次请求内使用，后端不做任何持久化存储。
"""

from __future__ import annotations

import json
import logging
import atexit
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import zipfile
import random
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler

import requests
from flask import Flask, g, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

import config

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    HAS_APSCHEDULER = True
except ImportError:  # 未安装时降级：手动清理仍可用，仅定时任务失效
    BackgroundScheduler = None
    HAS_APSCHEDULER = False

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    HAS_LIMITER = True
except ImportError:  # 未安装时降级：不启用限流，服务照常运行
    Limiter = None
    HAS_LIMITER = False

# --------------------------------------------------------------------------- #
# 基础配置
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
REQUEST_TIMEOUT = 180
TOOL_VERSION = "1.0"

# 最近一次生成的人格卡（内存缓存，仅作兼容；权威数据以 generated_cards 表为准）
LATEST_CARD: dict = {}

# 后台调度器（APScheduler），未安装或未启用时为 None
scheduler = None

app = Flask(__name__)
# Flask >= 2.3 使用 app.json.ensure_ascii，保证中文不被转义为 \uXXXX
try:
    app.json.ensure_ascii = False
except AttributeError:  # pragma: no cover - 旧版本 Flask
    app.config["JSON_AS_ASCII"] = False


# --------------------------------------------------------------------------- #
# 日志系统
# --------------------------------------------------------------------------- #

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
logger = logging.getLogger("cardtool")


def setup_logging() -> None:
    """初始化日志：控制台 + 按天滚动文件（保留 30 天）。

    配置根记录器并清空已有 handler，避免 Werkzeug/Flask 默认 handler 造成重复输出。
    """
    os.makedirs(config.LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    log_file = os.path.join(config.LOG_DIR, "app.log")
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",      # 每天 0 点滚动
        interval=1,
        backupCount=config.LOG_RETENTION_DAYS,   # 保留最近 30 天
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = "%Y-%m-%d"            # 归档名：app.log.2026-08-29

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(console)
    root.addHandler(file_handler)

    # Flask 自带 logger 与 Werkzeug 访问日志都交给根记录器统一处理
    app.logger.handlers.clear()
    app.logger.propagate = True
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    logger.setLevel(root.level)
    logger.propagate = True


def client_ip() -> str:
    """客户端 IP：优先 X-Forwarded-For（反向代理场景），回退 remote_addr。"""
    fwd = request.headers.get("X-Forwarded-For", "") if request else ""
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.remote_addr if request else "") or "unknown"


def mask_secret(secret: str) -> str:
    """密钥脱敏：仅保留前 3 位与后 2 位，其余用 * 替代。"""
    s = secret or ""
    if not s:
        return "[EMPTY]"
    if len(s) <= 7:
        return s[:1] + "****"
    return f"{s[:3]}****{s[-2:]}"


REDACTED = "[REDACTED]"


def redact_secrets(text: str, *secrets) -> str:
    """把文本中出现的所有密钥替换为 [REDACTED]，用于日志脱敏。"""
    out = str(text or "")
    for s in secrets:
        if s and isinstance(s, str) and len(s) >= 6:
            out = out.replace(s, REDACTED)
    # 兜底：清掉任何看起来像 DeepSeek Key 的片段
    out = re.sub(r"sk-[A-Za-z0-9]{6,}", REDACTED, out)
    return out


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #

def get_db() -> sqlite3.Connection:
    """获取数据库连接（每次请求独立连接，开启外键级联）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def log_db_errors(label: str):
    """装饰器：捕获数据库异常，记录完整堆栈后原样抛出。"""
    def deco(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except sqlite3.Error:
                logger.exception("数据库操作失败 [%s]", label)
                raise
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


# --------------------------------------------------------------------------- #
# 时间处理
# --------------------------------------------------------------------------- #

def now_local() -> str:
    """本地时间字符串（SQLite 的 CURRENT_TIMESTAMP 是 UTC，故统一由 Python 写入）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    """初始化数据表结构。"""
    try:
        conn = get_db()
    except sqlite3.Error:
        logger.exception("初始化数据库失败，无法连接 %s", DB_PATH)
        raise
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS upload_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_type   TEXT    NOT NULL,           -- 'file' | 'paste'
                file_name     TEXT    NOT NULL,           -- 文件名，粘贴上传时为 '粘贴内容'
                raw_content   TEXT    NOT NULL,           -- 原始文本
                parsed_nicks  TEXT    NOT NULL DEFAULT '[]', -- JSON 数组
                message_count INTEGER NOT NULL DEFAULT 0,
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chat_pairs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER NOT NULL,
                speaker   TEXT    NOT NULL,
                message   TEXT    NOT NULL,
                timestamp TEXT,
                msg_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (upload_id) REFERENCES upload_history(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS generated_cards (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_session_id INTEGER,                        -- 关联 upload_history.id，可为空（独立生成/导入）
                card_name         TEXT    NOT NULL DEFAULT '拾柒', -- 人格卡名称
                card_json         TEXT    NOT NULL,               -- 完整 JSON 字符串（v2 规范）
                mode              TEXT,                           -- 'fast' | 'strict'（导入为空）
                evaluation_score  INTEGER,                        -- 评估得分，未评估为空
                created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (upload_session_id) REFERENCES upload_history(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_pairs_upload
                ON chat_pairs (upload_id, msg_order);

            CREATE INDEX IF NOT EXISTS idx_generated_cards_time
                ON generated_cards (created_at DESC, id DESC);
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # 向后兼容：旧库可能没有 holdout_ids 字段，安全补列
        try:
            conn.execute("ALTER TABLE upload_history ADD COLUMN holdout_ids TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        _migrate_generated_cards(conn)
        _migrate_utc_to_local(conn)
    except sqlite3.Error:
        logger.exception("建表/迁移失败")
        raise
    finally:
        conn.close()


def _patch_applied(conn: sqlite3.Connection, name: str) -> bool:
    """判断一次性迁移补丁是否已执行过。"""
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (name,)).fetchone()
    return row is not None


def _mark_patch(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
        (name, now_local()),
    )
    conn.commit()


def _migrate_utc_to_local(conn: sqlite3.Connection) -> None:
    """历史数据里 created_at 是 SQLite CURRENT_TIMESTAMP（UTC），统一换算为本地时间。

    用 app_meta 里的补丁标记保证只跑一次，重复执行不会二次偏移。
    """
    patch = "localtime_v1"
    if _patch_applied(conn, patch):
        return
    try:
        for table in ("upload_history", "generated_cards"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "created_at" not in cols:
                continue
            cur = conn.execute(
                f"UPDATE {table} "
                "SET created_at = COALESCE(datetime(created_at, 'localtime'), created_at) "
                "WHERE created_at IS NOT NULL AND TRIM(created_at) <> ''"
            )
            if cur.rowcount:
                logger.info("时间字段已换算为本地时间：%s（%d 行）", table, cur.rowcount)
        _mark_patch(conn, patch)
    except sqlite3.Error:
        logger.exception("UTC → 本地时间迁移失败")
        raise


def _migrate_generated_cards(conn: sqlite3.Connection) -> None:
    """把旧版 generated_cards（含 user_nick/char_nick/train_count）升级为当前结构。

    旧表数据按可保留字段平移：id、card_name、card_json、mode(仅 fast/strict)、created_at；
    upload_session_id 与 evaluation_score 置空。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(generated_cards)")]
    if not cols or "upload_session_id" in cols:
        return  # 不存在或已是新结构

    conn.commit()  # executescript 会隐式提交，先落盘已有事务
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_generated_cards_time;
        ALTER TABLE generated_cards RENAME TO generated_cards_old;

        CREATE TABLE generated_cards (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_session_id INTEGER,
            card_name         TEXT    NOT NULL DEFAULT '拾柒',
            card_json         TEXT    NOT NULL,
            mode              TEXT,
            evaluation_score  INTEGER,
            created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (upload_session_id) REFERENCES upload_history(id) ON DELETE SET NULL
        );

        INSERT INTO generated_cards (id, upload_session_id, card_name, card_json, mode, evaluation_score, created_at)
            SELECT id,
                   NULL,
                   COALESCE(NULLIF(TRIM(COALESCE(card_name, '')), ''), '拾柒'),
                   card_json,
                   CASE WHEN mode IN ('fast', 'strict') THEN mode ELSE NULL END,
                   NULL,
                   created_at
            FROM generated_cards_old;

        CREATE INDEX IF NOT EXISTS idx_generated_cards_time
            ON generated_cards (created_at DESC, id DESC);

        DROP TABLE generated_cards_old;
        """
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# 聊天记录解析
# --------------------------------------------------------------------------- #

_DATE = r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
_TIME = r"(?:上午|下午|凌晨|中午|晚上)?\s*\d{1,2}:\d{2}(?::\d{2})?"
TIMESTAMP_RE = re.compile(rf"^\s*{_DATE}(?:[\s,，T]+{_TIME})?\s*$")


def is_timestamp(line: str) -> bool:
    """判断某一行是否为时间戳行。"""
    return bool(TIMESTAMP_RE.match(line or ""))


def parse_chat(text: str):
    """
    解析聊天记录。

    格式约定：
        昵称
        2026年08月28日 19:30
        消息内容（可多行）
        （空行分隔）

    返回：(messages, nicks)
        messages -> [{"speaker": str, "message": str, "timestamp": str}, ...]
        nicks    -> 按出现频次排序的昵称列表（最多两个）
    """
    if not text:
        return [], []

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    messages: list[dict] = []
    n = len(lines)
    i = 0

    while i < n:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if not is_timestamp(stripped):
            i += 1
            continue

        # 1) 昵称 = 时间戳行之前最近的非空行
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        nick = lines[j].strip() if j >= 0 else ""

        # 2) 内容 = 时间戳之后直到空行，
        #    若下一行是时间戳，说明当前行其实是下一条消息的昵称，需要停下
        k = i + 1
        content_lines: list[str] = []
        while k < n and lines[k].strip():
            nxt = lines[k + 1].strip() if k + 1 < n else ""
            if is_timestamp(nxt):
                i = k          # 把当前行留给下一轮当作昵称
                break
            content_lines.append(lines[k].rstrip())
            k += 1
        else:
            i = k
        if k < n and not lines[k].strip():
            i = k

        message = "\n".join(content_lines).strip()
        if nick and message:
            messages.append({"speaker": nick, "message": message, "timestamp": stripped})
        else:
            i = max(i, k)

        i += 1

    # 统计昵称频次，取出现最多的两个（同频时保留首次出现顺序）
    counter: dict[str, int] = {}
    for msg in messages:
        counter[msg["speaker"]] = counter.get(msg["speaker"], 0) + 1
    nicks = [name for name, _ in sorted(counter.items(), key=lambda kv: (-kv[1],))][:2]

    return messages, nicks


# --------------------------------------------------------------------------- #
# 数据读写
# --------------------------------------------------------------------------- #

@log_db_errors("保存上传记录")
def save_upload(upload_type: str, file_name: str, raw_content: str):
    """保存一次上传：写 upload_history + 逐条写 chat_pairs。"""
    messages, nicks = parse_chat(raw_content)

    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO upload_history
                (upload_type, file_name, raw_content, parsed_nicks, message_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                upload_type,
                file_name or "粘贴内容",
                raw_content,
                json.dumps(nicks, ensure_ascii=False),
                len(messages),
                now_local(),
            ),
        )
        upload_id = cur.lastrowid

        conn.executemany(
            """
            INSERT INTO chat_pairs (upload_id, speaker, message, timestamp, msg_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (upload_id, m["speaker"], m["message"], m["timestamp"], idx)
                for idx, m in enumerate(messages)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return upload_id, messages, nicks


def merge_messages():
    """合并数据库中所有对话对，按上传时间 + 消息顺序排列。"""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT cp.id, cp.speaker, cp.message, cp.timestamp
            FROM chat_pairs cp
            JOIN upload_history uh ON uh.id = cp.upload_id
            ORDER BY uh.id ASC, cp.msg_order ASC
            """
        ).fetchall()

        # 昵称按出现频次降序返回，保证前端自动填充的 User/Char 与实际主次一致
        nick_rows = conn.execute(
            """
            SELECT speaker, COUNT(*) AS cnt, MIN(id) AS first_id
            FROM chat_pairs
            GROUP BY speaker
            ORDER BY cnt DESC, first_id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    messages = [{"id": r["id"], "speaker": r["speaker"], "message": r["message"], "timestamp": r["timestamp"]} for r in rows]
    nicks = [r["speaker"] for r in nick_rows]
    return messages, nicks


# --------------------------------------------------------------------------- #
# 数据健康度评分
# --------------------------------------------------------------------------- #

# 视为"无效"的语气词 / 表情占位（仅由这些组成的消息不计入有效样本）
_INVALID_TOKENS = (
    "嗯", "嗯嗯", "嗯啊", "哦", "哦哦", "啊", "啊啊", "额", "呃", "哈",
    "哈哈", "哈哈哈", "呵呵", "嘻嘻", "嘿嘿", "呜呜", "赞", "好的", "好",
    "行", "可以", "收到", "在", "在的", "对", "是的", "对对", "表情包",
    "表情", "[表情]", "(表情)", "👍", "👌", "😊", "😂", "🤣", "❤️", "🌹",
)


def _strip_noise(text: str) -> str:
    """去掉空白、标点与无效语气词后，剩余的实质内容。"""
    import re as _re
    cleaned = _re.sub(r"[\s\W_]+", "", text or "")
    for tok in _INVALID_TOKENS:
        cleaned = cleaned.replace(tok, "")
    return cleaned


def score_upload(messages: list) -> dict:
    """
    评估一组对话的数据健康度，返回等级与明细。

    维度（满分 100）：
    - 有效消息占比 40%：剔除无效语气词后仍含实质内容的消息数 / 总消息数
    - 轮次交替率 30%：相邻消息说话人不同的比例（区分独白 vs 对话）
    - 平均消息长度 30%：平均中文字符数 / 5（封顶 1）
    """
    total = len(messages)
    if total == 0:
        return {
            "score": 0, "grade": "D",
            "valid_count": 0, "total": 0,
            "valid_ratio": 0, "alt_ratio": 0, "avg_len": 0,
        }

    valid = 0
    length_sum = 0
    for m in messages:
        body = m.get("message") or ""
        length_sum += len(_strip_noise(body))
        if _strip_noise(body):
            valid += 1

    alternations = 0
    for i in range(1, total):
        if messages[i].get("speaker") != messages[i - 1].get("speaker") and \
           messages[i].get("speaker") and messages[i - 1].get("speaker"):
            alternations += 1
    alt_ratio = alternations / (total - 1) if total > 1 else (1.0 if total >= 1 else 0.0)
    avg_len = length_sum / total
    len_score = min(avg_len / 5.0, 1.0)

    score = round(40 * (valid / total) + 30 * alt_ratio + 30 * len_score)

    if score >= 85:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 60:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": score,
        "grade": grade,
        "valid_count": valid,
        "total": total,
        "valid_ratio": round(valid / total, 3),
        "alt_ratio": round(alt_ratio, 3),
        "avg_len": round(avg_len, 1),
    }


# --------------------------------------------------------------------------- #
# DeepSeek 调用
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """你是一位精通人格心理学与语言风格分析的 AI 专家。你的任务是根据用户提供的对话记录，生成一张严格符合 SillyTavern V2 规范（chara_card_v2）的人格卡 JSON。

【输入数据说明】

你将收到一段完整的对话记录，格式为"昵称：消息内容"。

其中一方是"用户"（User，昵称 {user}），另一方是你需要分析的目标角色（Char，昵称 {char}）。

用户已在前端指定了哪个昵称是 User，哪个是 Char。

【分析任务（强制执行，按顺序完成）】

基础人格提取：从对话内容中提取 Char 的核心性格、价值观、说话风格、常用句式。

语气词与回应模式统计（强制项，必须输出统计结果）：
a. 统计 Char 的以下语气词出现次数（单独计数）：嗯、哦、好、哈、呵、唉、哎、哇、哼、切。
b. 统计 Char 的单字回复（如"嗯"、"哦"、"好"）占总回复数的百分比。
c. 统计 Char 的标点使用偏好（如感叹号、波浪号、省略号、问号出现的频率）。
d. 统计 Char 的平均回复字数（单条消息的平均长度）。
e. 统计 User 主动发起话题的次数 vs Char 主动发起话题的次数（若 Char 主动发起次数 < 3，标记为"被动回应型"）。

人格标签生成（根据统计结果强制推断）：

若单字回复占比 > 30%，在 personality 中写入「回避型依恋」或「疏离感」。

若感叹号或波浪号使用频繁，在 personality 中写入「情绪外放型」。

若平均回复字数 < 10 字，在 personality 中写入「表达克制型」。

若 Char 从未主动发起话题，在 personality 中写入「被动回应型社交模式」。

高频语气词写入：将统计出的 Top 3 高频语气词作为「语言印记」写入 personality。

【输出格式要求】
你必须只输出一个合法的 JSON 对象，严格符合 chara_card_v2 规范。顶层结构必须为：
{"spec": "chara_card_v2", "spec_version": "2.0", "data": { ... }}
其中 data 对象严格按照以下结构填写：
{
"name": "角色的昵称（使用 Char 的昵称）",
"description": "角色的外貌、背景、身份等描述（从对话中推断，若无则留空）",
"personality": "此处为完整人格总结。必须包含：\\n- 核心性格（3-5个关键词）\\n- 语气词统计结果（Top 3 高频词及占比）\\n- 回应模式分析（单字回复占比、平均回复字数、主动/被动倾向）\\n- 根据统计结果强制写入的人格标签",
"first_mes": "角色第一次见面的开场白（从对话中选取最典型的一句，或根据人格推断）",
"mes_example": "<START>\\n用户: {User 的第一条消息}\\n角色: {Char 的对应回复}\\n<START>\\n用户: {User 的第二条消息}\\n角色: {Char 的对应回复}\\n<START>\\n用户: {User 的第三条消息}\\n角色: {Char 的对应回复}"
}

【重要约束】

你必须严格基于对话记录中的统计分析得出结论，禁止凭空捏造。

personality 字段必须包含上述统计数字（如"单字回复占比 35%"），使结果可量化。

如果对话记录少于 5 条，在 personality 末尾添加警告："样本量较小，人格分析仅供参考。"

data 中其余可选字段（tags、alternate_greetings、character_book、extensions 等）如无依据可留空数组或空对象，不得省略 spec 与 data 顶层键。"""


USER_PROMPT_TEMPLATE = """以下是 {user}（用户/我）与 {char}（目标角色）的聊天记录，共 {count} 条。

----- 聊天记录开始 -----
{conversation}
----- 聊天记录结束 -----

请仅依据以上内容，刻画 {char} 并输出 chara_card_v2 规范的 JSON。"""


def build_conversation(messages, user_nick: str, char_nick: str) -> str:
    lines = []
    for m in messages:
        speaker = m.get("speaker", "")
        if speaker == user_nick:
            label = user_nick
        elif speaker == char_nick:
            label = char_nick
        else:
            label = speaker
        body = (m.get("message") or "").replace("\n", " ⏎ ")
        lines.append(f"{label}: {body}")
    return "\n".join(lines)


def extract_json(raw: str):
    """从模型返回内容中稳健提取 JSON 对象。"""
    if raw is None:
        raise ValueError("模型返回内容为空")

    text = raw.strip()
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # 截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def call_deepseek(api_key: str, payload_messages: list, temperature: float = 0.7):
    """调用 DeepSeek Chat Completion 接口（全程脱敏埋点，不记录任何请求/响应正文）。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": payload_messages,
        "temperature": temperature,
        "max_tokens": 4096,
        "stream": False,
    }

    # 只记录体积与条数，绝不记录对话内容或 API Key
    prompt_chars = sum(len(m.get("content") or "") for m in payload_messages)
    logger.info(
        "DeepSeek 调用开始 model=%s 消息条数=%d prompt字符=%d key=%s",
        DEEPSEEK_MODEL, len(payload_messages), prompt_chars, mask_secret(api_key),
    )
    logger.debug("DeepSeek 请求体（已脱敏）%s", redact_secrets(json.dumps(body)[:500], api_key))

    started = time.perf_counter()
    try:
        resp = requests.post(
            DEEPSEEK_API_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT
        )
    except requests.exceptions.Timeout:
        logger.error("DeepSeek 调用超时 耗时=%.0fms 上限=%ds",
                     (time.perf_counter() - started) * 1000, REQUEST_TIMEOUT)
        raise
    except requests.exceptions.RequestException as exc:
        # 异常信息里可能带上完整 URL / headers，统一脱敏
        logger.error("DeepSeek 网络异常 耗时=%.0fms 错误=%s",
                     (time.perf_counter() - started) * 1000,
                     redact_secrets(str(exc), api_key))
        raise

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if resp.status_code == 200:
        try:
            usage = (resp.json() or {}).get("usage") or {}
        except Exception:
            usage = {}
        logger.info(
            "DeepSeek 调用成功 status=200 耗时=%dms tokens(prompt/compl/total)=%s/%s/%s",
            elapsed_ms,
            usage.get("prompt_tokens", "-"),
            usage.get("completion_tokens", "-"),
            usage.get("total_tokens", "-"),
        )
    else:
        logger.error(
            "DeepSeek 调用失败 status=%s 耗时=%dms 响应摘要=%s",
            resp.status_code, elapsed_ms,
            redact_secrets((resp.text or "")[:200], api_key),
        )

    if resp.status_code == 401:
        raise RuntimeError("API Key 无效或已过期（401 Unauthorized）")
    if resp.status_code == 402:
        raise RuntimeError("账户余额不足，请前往 DeepSeek 平台充值（402）")
    if resp.status_code == 422:
        raise RuntimeError(f"请求参数错误（422）：{resp.text[:200]}")
    if resp.status_code == 429:
        raise RuntimeError("请求过于频繁或速率受限（429），请稍后重试")
    if resp.status_code >= 500:
        raise RuntimeError(f"DeepSeek 服务暂不可用（{resp.status_code}），请稍后重试")
    if resp.status_code != 200:
        raise RuntimeError(f"调用失败（{resp.status_code}）：{resp.text[:200]}")

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("模型未返回任何内容")
    return choices[0]["message"]["content"]


# --------------------------------------------------------------------------- #
# 限流
# --------------------------------------------------------------------------- #

def init_limiter():
    """初始化 Flask-Limiter；返回 None 表示未启用（调用方自动退化为无限制流）。"""
    if not config.RATE_LIMIT_ENABLED:
        logger.info("限流已关闭（RATE_LIMIT_ENABLED=0）")
        return None
    if not HAS_LIMITER:
        logger.warning(
            "未安装 Flask-Limiter，限流不可用。执行 pip install Flask-Limiter 后重启即可启用。"
        )
        return None
    # 注意：Flask-Limiter >= 3 的签名是 Limiter(key_func, app=..., ...)，
    # 必须全部用关键字传参，旧写法 Limiter(app, ...) 会把 app 当成 key_func。
    lim = Limiter(
        key_func=get_remote_address,      # 取 request.remote_addr，不受伪造的 X-Forwarded-For 影响
        app=app,
        default_limits=config.RATE_LIMIT_DEFAULT,
        storage_uri=config.RATE_LIMIT_STORAGE_URI,
    )
    logger.info(
        "限流已启用 全局=%s 存储=%s",
        config.RATE_LIMIT_DEFAULT, config.RATE_LIMIT_STORAGE_URI,
    )
    return lim


limiter = init_limiter()


def rate_limit(limit_value: str):
    """路由限流装饰器；限流未启用时退化为恒等装饰器，业务代码无需分支判断。"""
    if limiter is None:
        return lambda fn: fn
    return limiter.limit(limit_value)


# --------------------------------------------------------------------------- #
# 请求埋点 / 全局异常处理
# --------------------------------------------------------------------------- #

@app.before_request
def _log_request_start():
    g.started_at = time.perf_counter()
    logger.info(
        "请求开始 %s %s endpoint=%s client_ip=%s ua=%s",
        request.method, request.path,
        request.endpoint or "-",
        client_ip(),
        (request.user_agent.string or "")[:80],
    )


@app.after_request
def _log_request_end(resp):
    elapsed_ms = round((time.perf_counter() - getattr(g, "started_at", time.perf_counter())) * 1000)
    level = logging.ERROR if resp.status_code >= 500 else (
        logging.WARNING if resp.status_code >= 400 else logging.INFO
    )
    extra = f" 耗时={elapsed_ms}ms" if config.LOG_TIMING else ""
    logger.log(
        level,
        "请求结束 %s %s status=%s%s size=%sB",
        request.method, request.path, resp.status_code, extra,
        resp.headers.get("Content-Length", "-"),
    )
    return resp


@app.teardown_request
def _log_request_error(exc):
    """请求上下文销毁时若仍有异常（已被 errorhandler 处理过则 exc 为 None），补一条 ERROR。"""
    if exc is not None:
        logger.error(
            "请求异常未处理 %s %s: %s",
            request.method if request else "-",
            request.path if request else "-",
            redact_secrets(traceback.format_exception_only(type(exc), exc)[-1].strip()),
        )


def _retry_after_seconds(exc) -> int:
    """估算需要等待的秒数。

    Flask-Limiter 4.x 的 RateLimitExceeded 没有 retry_after 属性，
    因此退回到被触发规则的窗口长度（"per minute" → 60，"per hour" → 3600）。
    这是一个安全上界：等满一个窗口必定可以再次请求。
    """
    inner = getattr(getattr(exc, "limit", None), "limit", None)
    granularity = getattr(inner, "GRANULARITY", None)

    # limits 库的 GRANULARITY 是 NamedTuple(seconds=3600, name='hour')，
    # 早期版本可能是 datetime.timedelta —— 两种都兜住
    raw = getattr(granularity, "seconds", None)
    if raw is None:
        try:
            raw = granularity.total_seconds()
        except (AttributeError, TypeError, ValueError):
            raw = 0
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = 0
    return seconds if seconds > 0 else 60


@app.errorhandler(429)
def _handle_rate_limit(exc):
    """限流超限：Flask-Limiter 默认返回 HTML，这里统一成 JSON 方便前端解析。

    Flask 找错误处理器时会优先按状态码精确匹配，因此本处理器优先于下面的
    HTTPException 兜底处理器，不会把 429 误判成 500。
    """
    retry_after = _retry_after_seconds(exc)

    logger.warning(
        "触发限流 %s %s client_ip=%s retry_after=%ss",
        request.method, request.path, client_ip(), retry_after,
    )
    resp = jsonify({
        "success": False,
        "error": "请求过于频繁，请稍后再试。",
        "retry_after": retry_after,
        "code": 429,
    })
    resp.headers["Retry-After"] = str(retry_after)
    return resp, 429


@app.errorhandler(HTTPException)
def _handle_http_exception(exc: HTTPException):
    """404 / 405 / 400 等：优先返回 JSON，避免前端拿到 HTML 错误页。"""
    logger.warning(
        "HTTP 异常 status=%s %s %s detail=%s",
        exc.code, request.method, request.path, exc.description,
    )
    if request.path == "/" or "text/html" in (request.headers.get("Accept") or ""):
        return exc
    return jsonify({
        "success": False,
        "error": exc.description or "请求无效",
        "code": exc.code,
    }), exc.code


@app.errorhandler(Exception)
def _handle_unexpected_error(exc: Exception):
    """兜底：记录完整堆栈到日志，前端只拿到一句通用提示，绝不回传堆栈。"""
    # 用异常对象本身取堆栈而非 format_exc()：后者在无活动异常的上下文中会返回 "NoneType: None"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error(
        "未捕获异常 %s %s type=%s\n%s",
        request.method if request else "-",
        request.path if request else "-",
        type(exc).__name__,
        redact_secrets(tb),
    )
    return jsonify({
        "success": False,
        "error": "服务器内部错误，详细信息已写入 logs/app.log",
        "code": 500,
    }), 500


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
@rate_limit(config.RATE_LIMIT_UPLOAD)          # 防止大量小文件上传攻击
def upload():
    payload = request.get_json(silent=True) or {}
    upload_type = (payload.get("upload_type") or "paste").strip()
    content = payload.get("content") or ""
    file_name = payload.get("file_name") or ""

    if upload_type not in ("file", "paste"):
        return jsonify({"success": False, "error": "upload_type 只能是 'file' 或 'paste'"}), 400
    if not content.strip():
        return jsonify({"success": False, "error": "内容为空，无法解析"}), 400
    if len(content) > 5_000_000:
        return jsonify({"success": False, "error": "内容过大（上限约 5MB）"}), 400

    if upload_type == "paste":
        file_name = "粘贴内容"

    # 只记文件名与体积，不记内容
    logger.info(
        "文件上传开始 type=%s file_name=%s size=%d字符 client_ip=%s",
        upload_type, file_name, len(content), client_ip(),
    )
    try:
        upload_id, messages, nicks = save_upload(upload_type, file_name, content)
    except Exception:
        logger.error(
            "文件上传失败 type=%s file_name=%s size=%d字符",
            upload_type, file_name, len(content),
        )
        raise
    score = score_upload(messages)
    logger.info(
        "文件上传成功 id=%s 解析消息=%d 条 昵称=%s 健康度=%s 分(%s)",
        upload_id, len(messages), nicks, score.get("score"), score.get("grade"),
    )

    if len(nicks) < 2:
        return jsonify(
            {
                "success": True,
                "warning": f"仅解析到 {len(nicks)} 个昵称，请手动补充另一个昵称。",
                "id": upload_id,
                "nicks": nicks,
                "message_count": len(messages),
                "score": score,
            }
        )

    return jsonify(
        {
            "success": True,
            "id": upload_id,
            "nicks": nicks,
            "message_count": len(messages),
            "extra_nicks": len(nicks) > 2,
            "score": score,
        }
    )


@app.route("/history", methods=["GET"])
def history():
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, upload_type, file_name, message_count, parsed_nicks, created_at
            FROM upload_history
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    items = []
    for idx, r in enumerate(rows, start=1):
        try:
            nicks = json.loads(r["parsed_nicks"] or "[]")
        except json.JSONDecodeError:
            nicks = []
        items.append(
            {
                "index": idx,
                "id": r["id"],
                "upload_type": r["upload_type"],
                "file_name": r["file_name"],
                "message_count": r["message_count"],
                "nicks": nicks,
                "created_at": r["created_at"],
            }
        )

    conn2 = get_db()
    try:
        total_messages = conn2.execute("SELECT COUNT(*) c FROM chat_pairs").fetchone()["c"]
    finally:
        conn2.close()

    return jsonify({
        "success": True,
        "items": items,
        "total_message_count": total_messages,
        "warn_threshold": config.MESSAGE_WARN_THRESHOLD,
    })


# --------------------------------------------------------------------------- #
# 数据清理（手动 + 定时）
# --------------------------------------------------------------------------- #

_cleanup_lock = threading.Lock()


def cleanup_old_data(days: int = None, trigger: str = "manual") -> dict:
    """删除 N 天前的 upload_history，关联的 chat_pairs 由外键 ON DELETE CASCADE 连带清理。

    注意：generated_cards 的外键是 ON DELETE SET NULL，人格卡不会被删除，只会解除批次关联。
    并发安全依赖两层：线程锁（非阻塞获取，抢不到直接跳过）+ 单事务提交。
    """
    days = int(days or config.CLEANUP_RETENTION_DAYS)
    if days < 1:
        raise ValueError("保留天数必须 >= 1")

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    if not _cleanup_lock.acquire(blocking=False):
        logger.warning("清理任务跳过：已有清理操作正在执行 trigger=%s", trigger)
        return {"deleted": 0, "messages_deleted": 0, "cards_unlinked": 0,
                "cutoff": cutoff, "days": days, "trigger": trigger, "skipped": True}
    try:
        conn = get_db()
        try:
            pairs_before = conn.execute("SELECT COUNT(*) c FROM chat_pairs").fetchone()["c"]
            cards_unlinked = conn.execute(
                "SELECT COUNT(*) c FROM generated_cards WHERE upload_session_id IN "
                "(SELECT id FROM upload_history WHERE created_at < ?)", (cutoff,)
            ).fetchone()["c"]
            cur = conn.execute("DELETE FROM upload_history WHERE created_at < ?", (cutoff,))
            deleted = cur.rowcount
            conn.commit()
            pairs_after = conn.execute("SELECT COUNT(*) c FROM chat_pairs").fetchone()["c"]
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("清理历史数据失败 days=%s cutoff=%s trigger=%s", days, cutoff, trigger)
        raise
    finally:
        _cleanup_lock.release()

    result = {
        "deleted": deleted,
        "messages_deleted": pairs_before - pairs_after,
        "cards_unlinked": cards_unlinked,
        "cutoff": cutoff,
        "days": days,
        "trigger": trigger,
        "skipped": False,
    }
    if deleted or pairs_before != pairs_after:
        logger.info(
            "清理完成 trigger=%s 删除记录=%d 关联消息=%d 解除批次关联卡片=%d 保留天数=%d 截止=%s",
            trigger, deleted, result["messages_deleted"], cards_unlinked, days, cutoff,
        )
    else:
        logger.debug("无需清理 trigger=%s 保留天数=%d 截止=%s", trigger, days, cutoff)
    return result


@app.route("/cleanup", methods=["POST"])
def cleanup():
    """手动清理：删除 N 天前的上传记录与关联消息（人格卡保留）。"""
    payload = request.get_json(silent=True) or {}
    raw_days = payload.get("days")
    try:
        days = int(raw_days) if raw_days not in (None, "") else config.CLEANUP_RETENTION_DAYS
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "days 必须是正整数"}), 400
    if days < 1 or days > 3650:
        return jsonify({"success": False, "error": "days 需在 1 ~ 3650 之间"}), 400

    logger.info("手动清理请求 days=%d client_ip=%s", days, client_ip())
    try:
        result = cleanup_old_data(days=days, trigger="manual")
    except sqlite3.Error:
        return jsonify({"success": False, "error": "清理失败，详细信息已写入 logs/app.log"}), 500

    if result["skipped"]:
        return jsonify({"success": False, "error": "已有清理任务在执行中，请稍后重试"}), 409
    return jsonify({"success": True, **result})


@app.route("/delete", methods=["POST"])
def delete():
    payload = request.get_json(silent=True) or {}
    delete_type = (payload.get("delete_type") or "").strip()
    record_id = payload.get("id")

    conn = get_db()
    try:
        if delete_type == "single":
            if not record_id:
                return jsonify({"success": False, "error": "缺少 id"}), 400
            cur = conn.execute("DELETE FROM upload_history WHERE id = ?", (record_id,))
            affected = cur.rowcount
        elif delete_type == "last":
            row = conn.execute("SELECT id FROM upload_history ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return jsonify({"success": False, "error": "暂无可删除的记录"}), 400
            conn.execute("DELETE FROM upload_history WHERE id = ?", (row["id"],))
            affected = 1
        elif delete_type == "all":
            conn.execute("DELETE FROM chat_pairs")
            cur = conn.execute("DELETE FROM upload_history")
            affected = cur.rowcount
        else:
            return jsonify({"success": False, "error": "delete_type 只能是 'single' | 'last' | 'all'"}), 400

        conn.commit()
    finally:
        conn.close()

    return jsonify({"success": True, "deleted": affected})


@app.route("/merge", methods=["GET"])
def merge():
    messages, nicks = merge_messages()
    return jsonify({"success": True, "messages": messages, "nicks": nicks, "count": len(messages)})


@app.route("/test_api", methods=["POST"])
def test_api():
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("api_key") or "").strip()

    if not api_key:
        return jsonify({"success": False, "valid": False, "error": "请输入 API Key"})
    if not api_key.startswith("sk-"):
        return jsonify({"success": False, "valid": False, "error": "API Key 格式不正确（应以 sk- 开头）"})

    try:
        content = call_deepseek(
            api_key,
            [{"role": "user", "content": "回复两个字：你好"}],
            temperature=0,
        )
        return jsonify({"success": True, "valid": True, "reply": (content or "").strip()[:100]})
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "valid": False, "error": "请求超时，请检查网络后重试"}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "valid": False, "error": "无法连接 DeepSeek，请检查网络/代理"}), 502
    except RuntimeError as exc:
        return jsonify({"success": False, "valid": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover
        return jsonify({"success": False, "valid": False, "error": f"连接失败：{exc}"}), 500


@app.route("/generate_card", methods=["POST"])
@rate_limit(config.RATE_LIMIT_GENERATE)       # 每次都会调 DeepSeek，成本最高
def generate_card():
    global LATEST_CARD
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("api_key") or "").strip()
    user_nick = (payload.get("user_nick") or "").strip() or "user"
    char_nick = (payload.get("char_nick") or "").strip() or "char"
    messages = payload.get("messages")
    mode = (payload.get("mode") or "fast").strip().lower()
    if mode not in ("fast", "strict"):
        mode = "fast"

    if not api_key:
        return jsonify({"success": False, "error": "请先填写 DeepSeek API Key"}), 400
    if not api_key.startswith("sk-"):
        return jsonify({"success": False, "error": "API Key 格式不正确（应以 sk- 开头）"}), 400

    # 前端未传消息、或 Strict 模式需要 id（前端合并结果不含 id），统一从数据库读取
    if not messages or any("id" not in m for m in messages):
        messages, _ = merge_messages()

    if not messages:
        return jsonify({"success": False, "error": "暂无对话内容，请先上传聊天记录"}), 400

    fallback_reason = None
    holdout_ids = []

    if mode == "strict":
        total = len(messages)
        if total < 50:
            mode = "fast"
            fallback_reason = "数据量不足（需 ≥ 50 条有效消息），已自动切换至快速模式"
        else:
            # 抽取「用户提问 -> 角色回复」配对，从中留出盲测集，避免训练数据泄露
            pairs = []
            for i in range(1, total):
                if messages[i - 1].get("speaker") == user_nick and messages[i].get("speaker") == char_nick:
                    pairs.append((messages[i - 1], messages[i]))
            if len(pairs) < 2:
                mode = "fast"
                fallback_reason = "对话轮次不足，已自动切换至快速模式"
            else:
                k = min(max(1, round(len(pairs) * 0.1)), 20)
                chosen = random.sample(pairs, k)
                holdout_ids = [p[1]["id"] for p in chosen]
                exclude = {p[0]["id"] for p in chosen} | {p[1]["id"] for p in chosen}
                messages = [m for m in messages if m["id"] not in exclude]

    train_count = len(messages)
    logger.info(
        "人格卡生成开始 请求模式=%s 实际模式=%s 训练样本=%d 盲测=%d 角色=%s",
        (payload.get("mode") or "fast"), mode, train_count, len(holdout_ids), char_nick,
    )
    if fallback_reason:
        logger.warning("严谨模式已回退到快速模式：%s", fallback_reason)
    conversation = build_conversation(messages, user_nick, char_nick)
    # 控制 prompt 体积，超长时只保留最近的部分（保留开头少量上下文 + 最近对话）
    if len(conversation) > 40000:
        head = conversation[:6000]
        tail = conversation[-34000:]
        conversation = head + "\n......（中间部分已省略）......\n" + tail

    system_prompt = SYSTEM_PROMPT.replace("{user}", user_nick).replace("{char}", char_nick)
    user_prompt = USER_PROMPT_TEMPLATE.format(
        user=user_nick, char=char_nick, count=len(messages), conversation=conversation
    )

    try:
        raw = call_deepseek(
            api_key,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        card = extract_json(raw)

        # 兜底补齐规范字段
        if not isinstance(card, dict):
            raise ValueError("模型返回内容不是 JSON 对象")
        card.setdefault("spec", "chara_card_v2")
        card["spec"] = "chara_card_v2"
        card["spec_version"] = "2.0"
        data = card.get("data")
        if not isinstance(data, dict):
            data = {}
        data.setdefault("name", char_nick)
        data.setdefault("tags", [])
        data.setdefault("alternate_greetings", [])
        data.setdefault("character_book", {"entries": []})
        data.setdefault("extensions", {})
        data.setdefault("creator", "人格卡生成工具")
        data.setdefault("character_version", "1.0")
        card["data"] = data
        LATEST_CARD = card

        # 持久化到数据库，避免重启后丢失
        card_id = save_card(card, mode=mode, upload_session_id=latest_upload_id())
        logger.info(
            "人格卡生成成功 card_id=%s 名称=%s 模式=%s 训练样本=%d 盲测=%d",
            card_id, data.get("name"), mode, train_count, len(holdout_ids),
        )

        return jsonify({
            "success": True,
            "card": card,
            "card_id": card_id,
            "raw": raw,
            "evaluation_mode": mode,
            "holdout_count": len(holdout_ids),
            "holdout_ids": holdout_ids,
            "train_count": train_count,
            "fallback_reason": fallback_reason,
        })
    except requests.exceptions.Timeout:
        logger.error("人格卡生成超时 模式=%s 样本=%d", mode, train_count)
        return jsonify({"success": False, "error": "请求超时，对话过长时可减少内容后重试"}), 504
    except requests.exceptions.ConnectionError:
        logger.error("人格卡生成失败：无法连接 DeepSeek")
        return jsonify({"success": False, "error": "无法连接 DeepSeek，请检查网络/代理"}), 502
    except RuntimeError as exc:
        logger.error("人格卡生成失败（DeepSeek 返回错误）：%s", redact_secrets(str(exc), api_key))
        return jsonify({"success": False, "error": str(exc)}), 400
    except json.JSONDecodeError as exc:
        logger.error("人格卡生成失败：模型返回非法 JSON %s", exc)
        return jsonify({"success": False, "error": f"模型返回的不是合法 JSON：{exc}", "raw": raw}), 502
    except Exception as exc:  # pragma: no cover
        logger.exception("人格卡生成出现未预期异常")
        return jsonify({"success": False, "error": f"生成失败：{exc}"}), 500


@app.route("/chat", methods=["POST"])
def chat():
    """基于人格卡进行试聊（不落库，仅当次转发到 DeepSeek）。"""
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("api_key") or "").strip()
    system_prompt = (payload.get("system_prompt") or "").strip()
    messages = payload.get("messages") or []

    if not api_key:
        return jsonify({"success": False, "error": "请先填写 DeepSeek API Key"}), 400
    if not api_key.startswith("sk-"):
        return jsonify({"success": False, "error": "API Key 格式不正确（应以 sk- 开头）"}), 400
    if not system_prompt:
        return jsonify({"success": False, "error": "尚未生成人格卡，无法试聊"}), 400
    if not messages or not any(m.get("role") and m.get("content") for m in messages):
        return jsonify({"success": False, "error": "请输入对话内容"}), 400

    chat_messages = [{"role": "system", "content": system_prompt}] + messages
    try:
        reply = call_deepseek(api_key, chat_messages, temperature=0.8)
        return jsonify({"success": True, "reply": (reply or "").strip()})
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "请求超时，请稍后再试"}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "error": "无法连接 DeepSeek，请检查网络/代理"}), 502
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover
        return jsonify({"success": False, "error": f"试聊失败：{exc}"}), 500


# --------------------------------------------------------------------------- #
# 人格保真度评估
# --------------------------------------------------------------------------- #

_EVAL_ROLE_PROMPT = """请严格扮演以下角色，仅依据其人格设定与语气回答用户的提问。
不要输出任何解释、分析或旁白，只输出「角色本人」会说的话。

【人格特质】
{personality}

【角色描述】
{description}

【示例对话】
{examples}
"""

_EVAL_COMPARE_PROMPT = """你是严格的角色一致性审查专家。针对同一个「用户提问」，下方给出「真实历史回复」与「人格卡模拟回复」。
请从三个维度逐项打分（每项 0-100，整数），并给出总体相似度（0-100，整数）与一句中文诊断。

维度定义：
1. 语气：说话风格、口癖、情绪强度、措辞习惯的相似度
2. 信息量：回答的充实程度、是否贴合语境、是否给出同等信息
3. 决策逻辑：面对同一问题时，立场、倾向、反应方式是否一致

【用户提问】
{question}

【真实历史回复】
{ground_truth}

【人格卡模拟回复】
{simulated}

请严格只输出 JSON（不要任何多余文字）：
{{"tone": <int>, "information": <int>, "logic": <int>, "overall": <int>, "verdict": "<一句话中文诊断>"}}"""


def _extract_turns(messages: list, user_nick: str, char_nick: str) -> list:
    """从合并后的消息中抽取「用户提问 -> 角色回复」配对。"""
    turns = []
    for i in range(len(messages) - 1):
        u, c = messages[i], messages[i + 1]
        if u.get("speaker") == user_nick and c.get("speaker") == char_nick:
            turns.append((u.get("message") or "", c.get("message") or ""))
    # 兜底：若几乎没有紧跟配对，则把任意「紧邻的一条非用户消息」当作回复、其前一句当作提问
    if len(turns) < 2:
        for i in range(len(messages) - 1):
            if messages[i + 1].get("speaker") == char_nick:
                turns.append((messages[i].get("message") or "", messages[i + 1].get("message") or ""))
    return turns


@app.route("/evaluate_card", methods=["POST"])
@rate_limit(config.RATE_LIMIT_EVALUATE)      # 评估同样会多次调 DeepSeek
def evaluate_card():
    """评估「真人 vs 人格卡」回复一致性（双轨评估）。"""
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("api_key") or "").strip()
    user_nick = (payload.get("user_nick") or "").strip() or "user"
    char_nick = (payload.get("char_nick") or "").strip() or "char"
    card = payload.get("card")
    mode = (payload.get("mode") or "fast").strip().lower()
    holdout_ids = payload.get("holdout_ids") or []

    if not api_key:
        return jsonify({"success": False, "error": "请先填写 DeepSeek API Key"}), 400
    if not api_key.startswith("sk-"):
        return jsonify({"success": False, "error": "API Key 格式不正确（应以 sk- 开头）"}), 400
    if not isinstance(card, dict) or not card.get("data"):
        return jsonify({"success": False, "error": "尚未生成人格卡，无法评估"}), 400

    data = card.get("data") or {}
    personality = data.get("personality") or ""
    description = data.get("description") or ""
    mes_example = data.get("mes_example") or ""
    if isinstance(mes_example, list):
        mes_example = "\n".join(str(x) for x in mes_example)

    # 构建评估用的「用户提问 -> 角色回复」配对
    if mode == "strict" and holdout_ids:
        holdout_set = set(int(x) for x in holdout_ids if str(x).isdigit())
        conn = get_db()
        try:
            all_rows = conn.execute(
                "SELECT id, speaker, message FROM chat_pairs ORDER BY upload_id ASC, msg_order ASC"
            ).fetchall()
        finally:
            conn.close()
        turns = []
        for i in range(1, len(all_rows)):
            if all_rows[i - 1]["speaker"] == user_nick and all_rows[i]["speaker"] == char_nick \
               and all_rows[i]["id"] in holdout_set:
                turns.append((all_rows[i - 1]["message"], all_rows[i]["message"]))
        evaluation_type = "strict_holdout"
        disclaimer = "采用留出验证法，此评分具备统计可信度"
    else:
        messages, _ = merge_messages()
        turns = _extract_turns(messages, user_nick, char_nick)
        evaluation_type = "fast_approximation"
        disclaimer = "此为同源近似估计，仅供参考"

    if len(turns) < 2:
        return jsonify({"success": False, "error": "对话轮次不足，无法做交叉验证"}), 400

    # 随机抽取最多 5 条作为验证集（不足则全取）
    sample = turns if len(turns) <= 5 else random.sample(turns, 5)

    # 第一步：让人格卡对每个「用户提问」生成模拟回复
    role_prompt = _EVAL_ROLE_PROMPT.format(
        personality=personality, description=description, examples=mes_example
    )
    sims = []
    for q, _ in sample:
        try:
            sim = call_deepseek(
                api_key,
                [
                    {"role": "system", "content": role_prompt},
                    {"role": "user", "content": (q or "（无上下文）")},
                ],
                temperature=0.8,
            )
        except Exception:
            sim = ""
        sims.append(sim or "")

    # 第二步：逐条对比真实回复与模拟回复
    comparisons = []
    for (q, truth), sim in zip(sample, sims):
        compare_prompt = _EVAL_COMPARE_PROMPT.format(
            question=q, ground_truth=truth, simulated=sim
        )
        try:
            raw = call_deepseek(
                api_key,
                [{"role": "user", "content": compare_prompt}],
                temperature=0.3,
            )
            r = extract_json(raw) or {}
        except Exception:
            r = {}
        comparisons.append(
            {
                "question": q,
                "ground_truth": truth,
                "simulated": sim,
                "tone": r.get("tone", 0),
                "information": r.get("information", 0),
                "logic": r.get("logic", 0),
                "overall": r.get("overall", 0),
                "verdict": r.get("verdict", ""),
            }
        )

    overalls = [c["overall"] for c in comparisons if isinstance(c["overall"], int)]
    score = round(sum(overalls) / len(overalls)) if overalls else 0

    # 回写评估得分到 generated_cards（传了 card_id 就写指定那张，否则写最新一张）
    card_id = payload.get("card_id")
    score_saved = update_card_score(card_id, score) if overalls else False
    logger.info(
        "保真度评估完成 类型=%s 得分=%s 样本=%d 回写card_id=%s 结果=%s",
        evaluation_type, score, len(comparisons), card_id or "最新",
        "成功" if score_saved else "未回写",
    )

    resp = {
        "success": True,
        "score": score,
        "score_saved": score_saved,
        "count": len(comparisons),
        "detail": comparisons,
        "evaluation_type": evaluation_type,
        "disclaimer": disclaimer,
    }
    if evaluation_type == "strict_holdout":
        resp["holdout_used"] = len(turns)
    return jsonify(resp)


# --------------------------------------------------------------------------- #
# 导出（多格式）
# --------------------------------------------------------------------------- #

@app.route("/export", methods=["POST"])
def export_card():
    payload = request.get_json(silent=True) or {}
    fmt = (payload.get("format") or "sillytavern").strip().lower()
    card = payload.get("card")

    if not isinstance(card, dict):
        return jsonify({"success": False, "error": "尚未生成人格卡"}), 400

    data = card.get("data") or {}
    name = (data.get("name") or "character")
    safe = re.sub(r'[\\/:*?"<>|]', "_", str(name))

    def _ok(content: str, filename: str):
        logger.info("人格卡导出 format=%s file_name=%s size=%d字符", fmt, filename, len(content))
        return jsonify({"success": True, "content": content, "filename": filename})

    if fmt == "text":
        personality = data.get("personality") or ""
        description = data.get("description") or ""
        mes_example = data.get("mes_example") or ""
        if isinstance(mes_example, list):
            mes_example = "\n".join(str(x) for x in mes_example)
        content = (
            "# 角色设定 · System Instructions\n\n"
            "## 角色描述\n" + (description or "（未提供）") + "\n\n"
            "## 性格特质\n" + (personality or "（未提供）") + "\n\n"
            "## 示例对话\n" + (mes_example or "（未提供）") + "\n"
        )
        return _ok(content, f"{safe}_prompt.md")

    if fmt == "simple":
        simple = {
            "name": name,
            "personality": data.get("personality") or "",
            "greeting": data.get("first_mes") or "",
        }
        return _ok(json.dumps(simple, ensure_ascii=False, indent=2), f"{safe}_simple.json")

    # 默认：完整 SillyTavern JSON
    return _ok(json.dumps(card, ensure_ascii=False, indent=2), f"{safe}.json")


# --------------------------------------------------------------------------- #
# 人格卡持久化 + 导出隐私清洗
# --------------------------------------------------------------------------- #

@log_db_errors("读取最新上传批次")
def latest_upload_id():
    """当前最新一次上传的 id；无上传记录时返回 None（卡片记为独立生成）。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM upload_history ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


@log_db_errors("保存人格卡")
def save_card(card: dict, mode: str = "fast", card_name: str = "",
              upload_session_id=None, evaluation_score=None) -> int:
    """将人格卡写入 generated_cards 表，返回记录 id。

    card_name 留空时取 data.name，仍为空则回落默认值「拾柒」。
    upload_session_id 为 None 表示独立生成（不绑定某次上传批次）。
    """
    data = card.get("data") if isinstance(card.get("data"), dict) else {}
    name = (card_name or "").strip() or (data.get("name") or "").strip() or "拾柒"
    if mode not in ("fast", "strict"):
        mode = None
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO generated_cards "
            "(upload_session_id, card_name, card_json, mode, evaluation_score, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(upload_session_id) if upload_session_id else None,
                name,
                json.dumps(card, ensure_ascii=False),
                mode,
                int(evaluation_score) if evaluation_score is not None else None,
                now_local(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


@log_db_errors("读取最新人格卡")
def load_latest_card():
    """读取最新一张人格卡：{"id", "card", "card_name", "mode", "evaluation_score", "created_at"}，无则 None。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, card_json, card_name, mode, evaluation_score, created_at "
            "FROM generated_cards ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        card = json.loads(row["card_json"])
    except Exception:
        card = None
    return {
        "id": row["id"],
        "card": card,
        "card_name": row["card_name"],
        "mode": row["mode"],
        "evaluation_score": row["evaluation_score"],
        "created_at": row["created_at"],
    }


def update_card_score(card_id, score) -> bool:
    """回写评估得分；card_id 为空时写入最新一张。失败不影响主流程。"""
    try:
        conn = get_db()
        try:
            if card_id:
                cur = conn.execute(
                    "UPDATE generated_cards SET evaluation_score = ? WHERE id = ?",
                    (int(score), int(card_id)),
                )
            else:
                cur = conn.execute(
                    "UPDATE generated_cards SET evaluation_score = ? "
                    "WHERE id = (SELECT id FROM generated_cards "
                    "            ORDER BY created_at DESC, id DESC LIMIT 1)",
                    (int(score),),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


# --- 深度匿名化规则（仅在导出时应用，不修改数据库原文） ---

PII_TAG = "[个人信息]"
PRIVACY_TAG = "[隐私]"

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
RE_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")          # 18 位身份证
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")            # 11 位手机号
RE_IPLIKE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def _is_ipv4(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and len(p) <= 3 and int(p) <= 255 for p in parts)


def scrub_pii(text: str) -> str:
    """正则清洗：邮箱 / 身份证 / 手机号 / IP → [个人信息]。"""
    t = text or ""
    t = RE_EMAIL.sub(PII_TAG, t)
    t = RE_IDCARD.sub(PII_TAG, t)
    t = RE_PHONE.sub(PII_TAG, t)
    t = RE_IPLIKE.sub(lambda m: PII_TAG if _is_ipv4(m.group(0)) else m.group(0), t)
    return t


def apply_exclude_words(text: str, exclude_words) -> str:
    """自定义敏感词 → [隐私]（区分大小写，整词直接替换）。"""
    t = text or ""
    for w in exclude_words or []:
        w = (w or "").strip()
        if w:
            t = t.replace(w, PRIVACY_TAG)
    return t


def deep_clean(text: str, exclude_words=None) -> str:
    """导出前的完整清洗：先正则去 PII，再替换自定义敏感词。"""
    return apply_exclude_words(scrub_pii(text), exclude_words)


def parse_exclude_words(raw) -> list:
    """把前端传来的参数（数组 / 换行或逗号分隔的字符串）统一成词表。"""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = re.split(r"[\r\n,，;；]+", raw)
    out, seen = [], set()
    for w in raw:
        if not isinstance(w, str):
            continue
        w = w.strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


@app.route("/cards", methods=["GET"])
def list_cards():
    """历史人格卡列表（不含完整 JSON，只返回摘要）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, upload_session_id, card_name, mode, evaluation_score, created_at "
            "FROM generated_cards ORDER BY created_at DESC, id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"success": True, "cards": [dict(r) for r in rows]})


@app.route("/cards/<int:card_id>", methods=["GET"])
def get_card(card_id: int):
    """读取某一张历史人格卡的完整 JSON。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, upload_session_id, card_json, card_name, mode, evaluation_score, created_at "
            "FROM generated_cards WHERE id = ?", (card_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"success": False, "error": "卡片不存在"}), 404
    try:
        card = json.loads(row["card_json"])
    except Exception:
        return jsonify({"success": False, "error": "卡片数据已损坏"}), 500
    return jsonify({
        "success": True,
        "id": row["id"],
        "upload_session_id": row["upload_session_id"],
        "card": card,
        "card_name": row["card_name"],
        "mode": row["mode"],
        "evaluation_score": row["evaluation_score"],
        "created_at": row["created_at"],
    })


@app.route("/cards/<int:card_id>", methods=["DELETE"])
def delete_card(card_id: int):
    """删除某一张历史人格卡。"""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM generated_cards WHERE id = ?", (card_id,))
        conn.commit()
    except sqlite3.Error:
        logger.exception("删除人格卡失败 card_id=%s", card_id)
        raise
    finally:
        conn.close()
    if cur.rowcount == 0:
        logger.warning("删除人格卡：记录不存在 card_id=%s", card_id)
        return jsonify({"success": False, "error": "卡片不存在"}), 404
    logger.info("删除人格卡成功 card_id=%s", card_id)
    return jsonify({"success": True, "id": card_id})


# --------------------------------------------------------------------------- #
# 项目导出 / 导入（打包迁移）
# --------------------------------------------------------------------------- #

@app.route("/export_project", methods=["GET", "POST"])
@rate_limit(config.RATE_LIMIT_PROJECT_IO)
def export_project():
    """导出当前项目：深度匿名化聊天记录 + 最新一张人格卡，打包为 ZIP。

    POST（推荐）：JSON 体 {api_key, exclude_words: [...]}，敏感词表可较长。
    GET（兼容）：query 参数 exclude_words 支持换行或逗号分隔。
    """
    api_key = ""
    exclude_words: list = []
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        api_key = (payload.get("api_key") or "").strip()
        exclude_words = parse_exclude_words(payload.get("exclude_words"))
    else:
        api_key = (request.args.get("api_key") or "").strip()
        exclude_words = parse_exclude_words(request.args.get("exclude_words"))

    if api_key:
        masked = (api_key[:6] + "****") if len(api_key) > 6 else "****"
        app.logger.info("导出项目，API Key 前缀: %s", masked)  # 仅记录脱敏前缀，不落库

    conn = get_db()
    try:
        ups = conn.execute(
            "SELECT id, upload_type, file_name, raw_content, parsed_nicks, message_count, created_at "
            "FROM upload_history ORDER BY id ASC"
        ).fetchall()
        pairs = conn.execute(
            "SELECT id, upload_id, speaker, message, timestamp, msg_order "
            "FROM chat_pairs ORDER BY upload_id ASC, msg_order ASC"
        ).fetchall()
    finally:
        conn.close()

    # User = 频次最高的昵称，Char = 次高；用于匿名化替换
    _, nicks = merge_messages()
    user_nick = nicks[0] if len(nicks) >= 1 else ""
    char_nick = nicks[1] if len(nicks) >= 2 else ""

    def anon(text: str) -> str:
        """昵称替换 + 深度清洗（PII 正则 → 自定义敏感词），仅作用于导出副本。"""
        t = text or ""
        if user_nick:
            t = t.replace(user_nick, "我")
        if char_nick:
            t = t.replace(char_nick, "她")
        return deep_clean(t, exclude_words)

    uploads_export = []
    for r in ups:
        uploads_export.append({
            "id": r["id"],
            "upload_type": r["upload_type"],
            "file_name": r["file_name"],
            "raw_content": anon(r["raw_content"]),
            "parsed_nicks": json.loads(r["parsed_nicks"] or "[]"),
            "message_count": r["message_count"],
            "created_at": r["created_at"],
        })

    pairs_export = []
    for r in pairs:
        pairs_export.append({
            "id": r["id"],
            "upload_id": r["upload_id"],
            "speaker": anon(r["speaker"]),
            "message": anon(r["message"]),
            "timestamp": r["timestamp"],
            "msg_order": r["msg_order"],
        })

    latest = load_latest_card()
    card_export = latest["card"] if latest and latest["card"] else None

    meta = {
        "tool_version": TOOL_VERSION,
        "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "record_count": len(uploads_export),
        "message_count": len(pairs_export),
        "card_count": 1 if card_export else 0,
        "card_id": latest["id"] if latest else None,
        "exclude_word_count": len(exclude_words),
        "pii_rules": ["手机号", "身份证号", "邮箱", "IP 地址"],
        "privacy_note": ("导出内容已做两层清洗：①原始昵称替换为「我」「她」；②手机号、身份证号、"
                         "邮箱、IP 地址替换为 [个人信息]，自定义敏感词替换为 [隐私]。"
                         "清洗仅在导出时生效，不改动数据库原文。请注意：人格卡正文（card.json）"
                         "未做敏感词清洗，且聊天正文中的其他真实信息（如称谓、事件细节）无法自动识别，"
                         "分享前请自行确认已获得数据所有人授权。"),
    }

    readme = (
        "人格卡生成工具 · 项目归档\n"
        "========================\n\n"
        "本压缩包由「人格卡生成工具」导出，可用于迁移或分享。\n\n"
        "文件说明：\n"
        "  · project_meta.json        元信息（工具版本、记录数、消息数、清洗规则）\n"
        "  · anonymized_records.json  上传记录与对话消息（已深度匿名化）\n"
        "  · card.json                最新一次生成的人格卡（若存在）\n\n"
        "导出时的清洗规则：\n"
        "  1. 昵称 →「我」/「她」\n"
        "  2. 手机号 / 身份证号 / 邮箱 / IP 地址 → [个人信息]\n"
        f"  3. 自定义敏感词（本次 {len(exclude_words)} 个）→ [隐私]\n\n"
        "注意：清洗仅在导出时生效，不改动数据库原文；人格卡正文未做敏感词清洗；\n"
        "对话中其他真实信息（称谓、事件细节等）无法自动识别，分享前请自行确认。\n\n"
        "导入方式：在工具内点击「导入项目」，选择本文件即可还原数据与人格卡。\n"
        "注意：导入会清空当前所有数据，请提前备份。\n"
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = os.path.join(BASE_DIR, "exports", ts)
    os.makedirs(export_dir, exist_ok=True)
    meta_path = os.path.join(export_dir, "project_meta.json")
    records_path = os.path.join(export_dir, "anonymized_records.json")
    card_path = os.path.join(export_dir, "card.json")
    readme_path = os.path.join(export_dir, "README_import.txt")

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    with open(records_path, "w", encoding="utf-8") as f:
        json.dump({"upload_history": uploads_export, "chat_pairs": pairs_export}, f, ensure_ascii=False, indent=2)
    if card_export:
        with open(card_path, "w", encoding="utf-8") as f:
            json.dump(card_export, f, ensure_ascii=False, indent=2)
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme)

    zip_name = f"project_{ts}.zip"
    zip_path = os.path.join(BASE_DIR, "exports", zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(meta_path, "project_meta.json")
        zf.write(records_path, "anonymized_records.json")
        if card_export:
            zf.write(card_path, "card.json")
        zf.write(readme_path, "README_import.txt")

    zip_bytes = open(zip_path, "rb").read() if os.path.exists(zip_path) else b""
    logger.info(
        "项目文件打包完成 zip=%s size=%d字节 记录=%d 消息=%d 卡片=%d client_ip=%s",
        zip_name, len(zip_bytes), meta["record_count"], meta["message_count"],
        meta["card_count"], client_ip(),
    )

    resp = send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name,
    )

    @resp.call_on_close
    def _cleanup():
        # 请求结束后再清理临时文件
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            if os.path.isdir(export_dir):
                shutil.rmtree(export_dir)
        except OSError:
            pass

    return resp


@app.route("/import_project", methods=["POST"])
@rate_limit(config.RATE_LIMIT_PROJECT_IO)
def import_project():
    """导入项目：清空当前数据后，从 ZIP 还原上传记录、消息与人格卡。"""
    f = request.files.get("file")
    if not f:
        logger.warning("导入项目：未接收到文件 client_ip=%s", client_ip())
        return jsonify({"success": False, "error": "未接收到文件"}), 400

    zip_size = 0
    try:
        f.stream.seek(0, os.SEEK_END)
        zip_size = f.stream.tell()
        f.stream.seek(0)
    except Exception:
        pass
    logger.info(
        "项目文件开始导入 file_name=%s size=%s字节 client_ip=%s",
        f.filename, zip_size or "-", client_ip(),
    )

    # 校验并解析 ZIP
    try:
        with zipfile.ZipFile(f.stream) as zf:
            names = set(zf.namelist())
            if "project_meta.json" not in names or "anonymized_records.json" not in names:
                return jsonify({"success": False, "error": "无效的项目文件：缺少必要文件"}), 400
            meta = json.loads(zf.read("project_meta.json").decode("utf-8"))
            records = json.loads(zf.read("anonymized_records.json").decode("utf-8"))
            card = None
            if "card.json" in names:
                try:
                    card = json.loads(zf.read("card.json").decode("utf-8"))
                except Exception:
                    card = None
    except zipfile.BadZipFile:
        logger.error("项目导入失败：不是合法 ZIP file_name=%s", f.filename)
        return jsonify({"success": False, "error": "文件不是合法的 ZIP 压缩包"}), 400
    except Exception as exc:
        logger.exception("项目导入失败：读取异常 file_name=%s", f.filename)
        return jsonify({"success": False, "error": f"读取失败：{exc}"}), 400

    # 版本兼容性（仅温和提示，不阻断）
    warn = ""
    try:
        mv = int(str(meta.get("tool_version", "1.0")).split(".")[0])
        cv = int(TOOL_VERSION.split(".")[0])
        if mv > cv:
            warn = f"项目文件版本（{meta.get('tool_version')}）高于当前工具（{TOOL_VERSION}），可能存在不兼容"
    except Exception:
        pass

    ups = records.get("upload_history") or []
    pairs = records.get("chat_pairs") or []

    conn = get_db()
    try:
        conn.execute("DELETE FROM chat_pairs")
        conn.execute("DELETE FROM upload_history")
        conn.executemany(
            """
            INSERT INTO upload_history
                (id, upload_type, file_name, raw_content, parsed_nicks, message_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    u.get("id"),
                    u.get("upload_type", "paste"),
                    u.get("file_name", "粘贴内容"),
                    u.get("raw_content", ""),
                    json.dumps(u.get("parsed_nicks") or [], ensure_ascii=False),
                    u.get("message_count", 0),
                    u.get("created_at"),
                )
                for u in ups
            ],
        )
        conn.executemany(
            """
            INSERT INTO chat_pairs (id, upload_id, speaker, message, timestamp, msg_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p.get("id"),
                    p.get("upload_id"),
                    p.get("speaker", ""),
                    p.get("message", ""),
                    p.get("timestamp"),
                    p.get("msg_order", 0),
                )
                for p in pairs
            ],
        )
        conn.commit()
    except sqlite3.Error:
        logger.exception("项目导入失败：写入数据库异常（当前数据可能已被清空）")
        raise
    finally:
        conn.close()

    # 导入的人格卡同样落库，保证「最新卡片」在重启后仍可导出；
    # 导入包不含上传批次信息，upload_session_id 留空表示独立生成
    card_id = None
    if isinstance(card, dict) and card:
        data = card.get("data") if isinstance(card.get("data"), dict) else {}
        card_id = save_card(card, mode="", card_name=(data.get("name") or "").strip())

    global LATEST_CARD
    LATEST_CARD = card or {}

    logger.info(
        "项目导入成功 file_name=%s 记录=%d 消息=%d 卡片=%s",
        f.filename, len(ups), len(pairs), card_id or "无",
    )
    if warn:
        logger.warning("项目导入版本告警：%s", warn)

    return jsonify({
        "success": True,
        "records": len(ups),
        "messages": len(pairs),
        "cards": 1 if card else 0,
        "card_id": card_id,
        "warning": warn,
        "card": card,
    })


def _scheduled_cleanup():
    """定时任务入口：异常必须自己吞掉，否则 APScheduler 会把 job 停掉。"""
    try:
        cleanup_old_data(trigger="scheduler")
    except Exception:
        logger.exception("定时清理任务执行失败")


def start_scheduler() -> None:
    """启动后台调度器，按间隔自动清理历史数据。"""
    global scheduler
    if not config.CLEANUP_ENABLED:
        logger.info("定时清理已关闭（CLEANUP_ENABLED=0），仅保留手动清理")
        return
    if not HAS_APSCHEDULER:
        logger.warning(
            "未安装 APScheduler，定时清理不可用（手动清理按钮仍可用）。"
            "执行 pip install APScheduler 后重启即可启用。"
        )
        return
    if scheduler is not None and getattr(scheduler, "running", False):
        return

    scheduler = BackgroundScheduler()
    job = scheduler.add_job(
        _scheduled_cleanup,
        trigger="interval",
        days=config.CLEANUP_INTERVAL_DAYS,
        id="cleanup_old_data",
        name="清理过期聊天记录",
        replace_existing=True,
        max_instances=1,     # 并发防线：上一轮没跑完就不开新一轮
        coalesce=True,       # 错过的执行合并为一次
    )
    scheduler.start()
    atexit.register(shutdown_scheduler)
    logger.info(
        "定时清理已启动 间隔=%d天 保留=%d天 下次执行=%s",
        config.CLEANUP_INTERVAL_DAYS, config.CLEANUP_RETENTION_DAYS, job.next_run_time,
    )


def shutdown_scheduler() -> None:
    global scheduler
    if scheduler is not None and getattr(scheduler, "running", False):
        scheduler.shutdown(wait=False)
        logger.info("定时清理调度器已停止")
    scheduler = None


def _should_run_maintenance() -> bool:
    """判断当前进程是否该跑启动清理与调度器。

    正常情况下（python app.py）只有一个进程，直接返回 True。
    设 CARDTOOL_SKIP_MAINTENANCE=1 可整体跳过（自测导入 app 时使用，避免误清真实数据）。
    仅当有人用 reloader 环境跑（FLASK_DEBUG=1 等）时，靠 WERKZEUG_RUN_MAIN
    区分父子进程，只有真正提供服务的子进程才执行，避免两个调度器同时跑。
    """
    if os.environ.get("CARDTOOL_SKIP_MAINTENANCE") == "1":
        return False
    flag = os.environ.get("WERKZEUG_RUN_MAIN")
    if flag is not None:
        return flag == "true"
    return True


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

def serve_app():
    """生产环境 WSGI 入口（gunicorn / waitress 等外部服务器使用）。

    gunicorn / waitress 通过「导入 app 模块」的方式加载应用，不会执行
    __main__ 分支，因此在这里补跑启动清理与定时调度器，再返回 Flask 实例。

    用法：
        gunicorn  -w 1 -b 0.0.0.0:5000 'app:serve_app()'     # Linux
        waitress-serve --port=5000 'app:serve_app()'         # Windows

    注意：保持单 worker（-w 1）。调度器是进程内线程，多 worker 会各起一份；
    SQLite 也不适合多进程并发写。生产环境请勿再经过 app.run(debug=True)。
    """
    if _should_run_maintenance():
        if config.CLEANUP_ON_START:
            try:
                cleanup_old_data(trigger="startup")
            except Exception:
                logger.exception("启动时清理失败（不影响服务启动）")
        start_scheduler()
    return app


setup_logging()    # 必须最先执行，后续建表/迁移都要打日志
logger.info("=" * 60)
logger.info("人格卡生成工具启动 版本=%s 数据库=%s", TOOL_VERSION, DB_PATH)
logger.info("日志级别=%s 日志目录=%s 保留天数=%d",
            config.LOG_LEVEL, config.LOG_DIR, config.LOG_RETENTION_DAYS)
logger.info("=" * 60)

init_db()          # 建表与迁移本身会打日志，故放在日志初始化之后


if __name__ == "__main__":
    # 启动清理与调度器只在真正提供服务的进程里跑一次（见 _should_run_maintenance）
    if _should_run_maintenance():
        if config.CLEANUP_ON_START:
            try:
                cleanup_old_data(trigger="startup")
            except Exception:
                logger.exception("启动时清理失败（不影响服务启动）")
        start_scheduler()

    print("=" * 56)
    print("  人格卡生成工具已启动")
    print(f"  访问地址: http://127.0.0.1:5000")
    print(f"  数据库:   {DB_PATH}")
    print(f"  日志目录: {config.LOG_DIR}（级别 {config.LOG_LEVEL}，保留 {config.LOG_RETENTION_DAYS} 天）")
    print(f"  自动清理: {'开启' if config.CLEANUP_ENABLED else '关闭'}"
          f"（保留 {config.CLEANUP_RETENTION_DAYS} 天，间隔 {config.CLEANUP_INTERVAL_DAYS} 天）")
    print("=" * 56)
    # 关闭热重载（use_reloader=False）：单进程运行，彻底避免
    # 「文件写到一半被 reloader 读到导致崩溃」和双调度器问题；
    # 代价是改代码后需要手动重启服务。
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
