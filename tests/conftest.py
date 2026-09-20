import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete

from energy_bot.db import create_engine_from_dsn, create_session_factory
from energy_bot.models import Order, UpstreamDelivery, User

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离环境变量,避免 ENERGY_BOT_* 覆盖影响其他用例(测试 DSN 除外)。"""
    for key in list(os.environ):
        if key.startswith("ENERGY_BOT_") and key != "ENERGY_BOT_TEST_DSN":
            monkeypatch.delenv(key)


@pytest.fixture
async def db_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_env):
    """仅使用显式测试库;经真实 Alembic 升级,逐例清空业务表。"""
    dsn = os.environ.get("ENERGY_BOT_TEST_DSN")
    if not dsn:
        pytest.skip("需要 ENERGY_BOT_TEST_DSN 指向专用 PostgreSQL 测试库")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ENERGY_BOT_CONFIG", str(config_path))
    monkeypatch.setenv("ENERGY_BOT_DATABASE__DSN", dsn)
    config = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_engine_from_dsn(dsn)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            for model in (UpstreamDelivery, Order, User):
                await session.execute(delete(model))
            await session.commit()
        yield factory
    finally:
        await engine.dispose()
