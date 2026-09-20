"""日志系统。

- 开发环境:彩色人类可读格式,输出到 stdout
- 生产环境:结构化 JSON;配置 log_dir 后按天轮转保留 30 天,同时输出 stdout 和文件
- JSON 字段为 ts/level/logger/msg,方便采集端统一解析
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar


class JSONFormatter(logging.Formatter):
    """把日志记录格式化成单行 JSON,方便采集和检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ColoredFormatter(logging.Formatter):
    """开发环境用的彩色格式。"""

    COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[36m",  # 青
        "INFO": "\033[32m",  # 绿
        "WARNING": "\033[33m",  # 黄
        "ERROR": "\033[31m",  # 红
        "CRITICAL": "\033[35m",  # 紫
    }
    RESET: ClassVar[str] = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(level: str = "INFO", log_dir: str | None = None, json_logs: bool = False) -> None:
    """初始化全局日志:stdout 必有;配置 log_dir 则追加按天轮转的 JSON 文件。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    # stdout 处理器
    stdout_handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        stdout_handler.setFormatter(JSONFormatter())
    else:
        stdout_handler.setFormatter(
            ColoredFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root.addHandler(stdout_handler)

    # 文件处理器(按天轮转,保留 30 天),文件固定 JSON
    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            path / "energy-bot.log",
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(JSONFormatter())
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """获取带模块名的 logger,业务代码统一用这个。"""
    return logging.getLogger(name)
