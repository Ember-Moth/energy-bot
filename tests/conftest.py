import os

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离环境变量,避免 ENERGY_BOT_* 覆盖影响其他用例(测试 DSN 除外)。"""
    for key in list(os.environ):
        if key.startswith("ENERGY_BOT_") and key != "ENERGY_BOT_TEST_DSN":
            monkeypatch.delenv(key)
