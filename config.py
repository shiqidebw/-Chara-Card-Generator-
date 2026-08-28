# -*- coding: utf-8 -*-
"""
配置中心。

优先级：环境变量 > 项目根目录 .env 文件 > 代码默认值。

把 .env.example 复制为 .env 后按需修改即可，无需改动代码。
.env 不纳入版本控制（已在 .gitignore 中排除）。
"""

from __future__ import annotations

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

# 默认值（环境变量 / .env 未配置时生效）
DEFAULTS = {
    # 日志级别：DEBUG / INFO / WARNING / ERROR / CRITICAL
    "LOG_LEVEL": "INFO",
    # 日志目录，相对于本文件所在目录；运行时自动创建
    "LOG_DIR": "logs",
    # 按天滚动的日志文件保留天数
    "LOG_RETENTION_DAYS": "30",
    # 是否在日志中记录请求耗时（毫秒）
    "LOG_TIMING": "1",
    # 是否开启定时自动清理历史数据
    "CLEANUP_ENABLED": "1",
    # 保留天数：早于 now - N 天的 upload_history 会被清理（chat_pairs 外键级联删除）
    "CLEANUP_RETENTION_DAYS": "7",
    # 自动清理的执行间隔（天）
    "CLEANUP_INTERVAL_DAYS": "1",
    # 启动时是否先跑一次清理；设为 0 可关闭（调试/回溯数据时有用）
    "CLEANUP_ON_START": "1",
    # 总消息数超过该阈值时，前端提示建议清理
    "MESSAGE_WARN_THRESHOLD": "10000",
    # ---------- 限流 ----------
    # 是否开启限流（本地单机使用且嫌麻烦时可设 0）
    "RATE_LIMIT_ENABLED": "1",
    # 全局兜底（可填多条，逗号分隔）
    # 说明：/chat、/history 等交互路由都走这里，200/时约合每分钟 3.3 次，
    #       足够单人正常使用，又能挡住脚本刷接口
    "RATE_LIMIT_DEFAULT": "200 per hour",
    # 高消耗路由单独限流
    "RATE_LIMIT_GENERATE": "5 per minute",     # 调 DeepSeek 生成人格卡
    "RATE_LIMIT_EVALUATE": "10 per minute",    # 保真度评估
    "RATE_LIMIT_UPLOAD": "20 per minute",      # 上传聊天记录
    "RATE_LIMIT_PROJECT_IO": "30 per hour",    # 项目导出 / 导入
    # 存储后端，默认内存；分布式部署改成 redis://host:port
    "RATE_LIMIT_STORAGE_URI": "memory://",
}


def _parse_env_file(path: str) -> dict:
    """极简 .env 解析：支持 KEY=VALUE、# 注释、引号包裹，不引入额外依赖。"""
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                values[key] = val
    return values


_FILE_VALUES = _parse_env_file(ENV_PATH)


def get(key: str) -> str:
    """读取配置项：环境变量优先，其次 .env，最后默认值。"""
    if key in os.environ and os.environ[key].strip():
        return os.environ[key].strip()
    if key in _FILE_VALUES and _FILE_VALUES[key].strip():
        return _FILE_VALUES[key].strip()
    return DEFAULTS.get(key, "")


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return fallback


def get_bool(key: str, fallback: bool = False) -> bool:
    v = get(key).lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return fallback


# 日志
LOG_LEVEL = get("LOG_LEVEL").upper()
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
if LOG_LEVEL not in VALID_LEVELS:
    LOG_LEVEL = "INFO"

LOG_DIR = get("LOG_DIR") or "logs"
if not os.path.isabs(LOG_DIR):
    LOG_DIR = os.path.join(BASE_DIR, LOG_DIR)

LOG_RETENTION_DAYS = max(1, get_int("LOG_RETENTION_DAYS", 30))
LOG_TIMING = get_bool("LOG_TIMING", True)

# 数据清理
CLEANUP_ENABLED = get_bool("CLEANUP_ENABLED", True)
CLEANUP_RETENTION_DAYS = max(1, get_int("CLEANUP_RETENTION_DAYS", 7))
CLEANUP_INTERVAL_DAYS = max(1, get_int("CLEANUP_INTERVAL_DAYS", 1))
CLEANUP_ON_START = get_bool("CLEANUP_ON_START", True)
MESSAGE_WARN_THRESHOLD = max(1, get_int("MESSAGE_WARN_THRESHOLD", 10000))

# 限流
RATE_LIMIT_ENABLED = get_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_DEFAULT = [
    s.strip() for s in get("RATE_LIMIT_DEFAULT").split(",") if s.strip()
] or ["200 per hour"]
RATE_LIMIT_GENERATE = get("RATE_LIMIT_GENERATE") or "5 per minute"
RATE_LIMIT_EVALUATE = get("RATE_LIMIT_EVALUATE") or "10 per minute"
RATE_LIMIT_UPLOAD = get("RATE_LIMIT_UPLOAD") or "20 per minute"
RATE_LIMIT_PROJECT_IO = get("RATE_LIMIT_PROJECT_IO") or "30 per hour"
RATE_LIMIT_STORAGE_URI = get("RATE_LIMIT_STORAGE_URI") or "memory://"
