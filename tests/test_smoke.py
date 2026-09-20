import json
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from energy_bot.config import (
    TIMEZONE,
    DatabaseSettings,
    Settings,
    WebhookSettings,
    default_config_path,
    load_settings,
    resolve_config_path,
)
from energy_bot.handlers import routers
from energy_bot.logging_config import setup_logging
from energy_bot.middlewares.logging import LoggingMiddleware
from energy_bot.models import Order, OrderStatus, User


def _write_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(body, encoding="utf-8")
    return config


def test_routers_registered() -> None:
    names = {router.name for router in routers}
    assert {"start", "echo"} <= names


async def test_logging_middleware_passes_event_through() -> None:
    seen: list[tuple[object, dict[str, int]]] = []

    async def handler(event: object, data: dict[str, int]) -> None:
        seen.append((event, data))

    await LoggingMiddleware()(handler, "event", {"k": 1})

    assert seen == [("event", {"k": 1})]


def test_settings_direct_construction() -> None:
    settings = Settings(
        bot_token="abc",
        webhook=WebhookSettings(base_url="https://example.com"),
    )
    assert settings.bot_token == "abc"


def test_default_config_path_in_user_config_dir() -> None:
    path = default_config_path()
    assert path.name == "config.yaml"
    assert "energy-bot" in path.parts


def test_resolve_config_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.yaml"
    assert resolve_config_path(explicit) == explicit  # --config 参数最优先
    monkeypatch.setenv("ENERGY_BOT_CONFIG", "/from/env.yaml")
    assert resolve_config_path(explicit) == explicit  # 参数仍优先于环境变量
    assert resolve_config_path() == Path("/from/env.yaml")  # 环境变量优先于平台默认
    monkeypatch.delenv("ENERGY_BOT_CONFIG")
    assert resolve_config_path() == default_config_path()


def test_load_settings_defaults(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "bot_token: abc\nwebhook:\n  base_url: https://example.com\n")
    settings = load_settings(config)
    assert settings.bot_token == "abc"
    assert settings.webhook.host == "127.0.0.1"
    assert settings.webhook.port == 8080
    assert settings.webhook.path == "/webhook"
    assert settings.webhook.secret_token == ""
    assert settings.logging.level == "INFO"
    assert settings.logging.log_dir == ""
    assert settings.logging.json_logs is False


def test_load_settings_normalizes_values(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "bot_token: abc\n"
        "webhook:\n"
        "  base_url: https://example.com/\n"
        "  path: hook\n"
        "logging:\n"
        "  level: debug\n"
        "  json_logs: true\n",
    )
    settings = load_settings(config)
    assert settings.webhook.base_url == "https://example.com"  # 去掉尾部斜杠
    assert settings.webhook.path == "/hook"  # 自动补 /
    assert settings.logging.level == "DEBUG"  # 统一为大写
    assert settings.logging.json_logs is True


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(tmp_path, "bot_token: abc\nwebhook:\n  base_url: https://example.com\n")
    monkeypatch.setenv("ENERGY_BOT_WEBHOOK__PORT", "9443")
    monkeypatch.setenv("ENERGY_BOT_LOGGING__LEVEL", "warning")
    settings = load_settings(config)
    assert settings.webhook.port == 9443
    assert settings.logging.level == "WARNING"
    assert settings.webhook.base_url == "https://example.com"  # 未覆盖字段保持 YAML 值


def test_load_settings_database_discrete_fields(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "bot_token: abc\n"
        "webhook:\n"
        "  base_url: https://example.com\n"
        "database:\n"
        "  address: localhost\n"
        "  username: u\n"
        "  password: p@ss:word\n"
        "  database: energy_bot\n"
        "  max_overflow: 20\n",
    )
    database = load_settings(config).database
    # 特殊字符密码会被转义,不会破坏连接串
    assert database.effective_dsn() == "postgresql://u:p%40ss%3Aword@localhost:5432/energy_bot"
    assert database.pool_size == 5  # 未配置用默认
    assert database.max_overflow == 20


def test_env_dsn_takes_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(
        tmp_path,
        "database:\n  address: localhost\n  username: u\n  database: db\n",
    )
    monkeypatch.setenv("ENERGY_BOT_DATABASE__DSN", "postgres://from-env:5432/db")
    assert load_settings(config).database.effective_dsn() == "postgres://from-env:5432/db"


def test_dsn_in_yaml_rejected(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "database:\n  dsn: postgresql://u@h/db\n")
    with pytest.raises(SystemExit):
        load_settings(config)


def test_invalid_env_dsn_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(
        tmp_path, "database:\n  address: localhost\n  username: u\n  database: db\n"
    )
    monkeypatch.setenv("ENERGY_BOT_DATABASE__DSN", "mysql://bad")
    with pytest.raises(SystemExit):
        load_settings(config)


def test_incomplete_database_effective_dsn_raises() -> None:
    with pytest.raises(ValueError, match="address/username/database"):
        DatabaseSettings().effective_dsn()


def test_project_timezone_is_utc_plus_8() -> None:
    # 东八区无夏令时,任意时刻偏移恒为 +8
    assert TIMEZONE.utcoffset(datetime(2026, 1, 1, tzinfo=TIMEZONE)) == timedelta(hours=8)
    assert TIMEZONE.utcoffset(datetime(2026, 7, 1, tzinfo=TIMEZONE)) == timedelta(hours=8)


def test_missing_config_file_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_settings(tmp_path / "config.yaml")


def test_invalid_yaml_exits(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "bot_token: [unclosed\n")
    with pytest.raises(SystemExit):
        load_settings(config)


def test_invalid_base_url_exits(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "bot_token: abc\nwebhook:\n  base_url: http://example.com\n")
    with pytest.raises(SystemExit):
        load_settings(config)


def test_invalid_port_exits(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "bot_token: abc\nwebhook:\n  base_url: https://example.com\n  port: 99999\n",
    )
    with pytest.raises(SystemExit):
        load_settings(config)


def test_invalid_log_level_exits(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "bot_token: abc\nwebhook:\n  base_url: https://example.com\nlogging:\n  level: VERBOSE\n",
    )
    with pytest.raises(SystemExit):
        load_settings(config)


@pytest.mark.skipif(
    not os.environ.get("ENERGY_BOT_TEST_DSN"),
    reason="需要 ENERGY_BOT_TEST_DSN 指向可用的 PostgreSQL(测试库,数据会被清空)",
)
async def test_models_roundtrip(db_factory) -> None:
    factory = db_factory
    async with factory() as session:
        tz = (await session.execute(text("SELECT current_setting('TimeZone')"))).scalar_one()
        assert tz == "Asia/Shanghai"
    async with factory() as session:
        session.add(User(id=1, first_name="测试", language_code="zh"))
        await session.flush()
        session.add(
            Order(
                user_id=1,
                recipient_address="TBase1ExampleAddressDoNotUseXxx",
                energy_amount=65000,
                duration_hours=1,
                price=Decimal("1.5"),
                status=OrderStatus.DRAFT,
            )
        )
        await session.commit()
    async with factory() as session:
        stmt = select(Order).options(selectinload(Order.user))  # async 下禁止懒加载,显式预加载
        order = (await session.execute(stmt)).scalar_one()
        assert order.user.first_name == "测试"
        assert order.price == Decimal("1.5")
        assert order.status is OrderStatus.DRAFT


def test_setup_logging_json_to_stdout_and_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    old_level, old_handlers = root.level, root.handlers[:]
    log_dir = tmp_path / "logs"  # 父目录不存在,应自动创建
    try:
        setup_logging(level="INFO", log_dir=str(log_dir), json_logs=True)
        logging.getLogger("t").info("你好")
        for handler in root.handlers:
            handler.flush()
        record = json.loads((log_dir / "energy-bot.log").read_text(encoding="utf-8").strip())
        assert record["level"] == "INFO"
        assert record["logger"] == "t"
        assert record["msg"] == "你好"
        assert record["ts"]
    finally:
        for handler in root.handlers:
            handler.close()
        root.setLevel(old_level)
        root.handlers[:] = old_handlers
