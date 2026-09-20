import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from energy_bot.config import LogSettings

_FORMAT = "%(asctime)s %(levelname)s %(name)s - %(message)s"


def setup_logging(log: LogSettings) -> None:
    """按配置初始化根日志:stderr 必有,配置了 file 则追加滚动文件。"""
    handlers: list[logging.Handler] = [
        logging.StreamHandler()
    ]  # 默认 stderr,systemd 可收入 journald
    if log.file:
        Path(log.file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log.file,
                maxBytes=log.file_max_bytes,
                backupCount=log.file_backup_count,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=log.level,
        format=_FORMAT,
        handlers=handlers,
        force=True,  # 可重复初始化(测试场景)
    )
