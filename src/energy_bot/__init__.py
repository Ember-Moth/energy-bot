"""energy-bot:基于 aiogram 3 的 Telegram bot(webhook 模式)。

入口形态(三层等价):
1. console script `energy-bot`(pyproject [project.scripts],生产用);
2. `python -m energy_bot`(__main__.py,开发用);
3. `main()` 本体在此(装 uvloop → 驱动 app.amain)。

仅支持 macOS / Linux 部署,事件循环固定使用 uvloop。
"""

import argparse
import asyncio
from pathlib import Path

import uvloop

from energy_bot.app import amain
from energy_bot.config import default_config_path

__version__ = "0.1.0"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="energy-bot",
        description="基于 aiogram 3 的 Telegram bot(webhook 模式)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"配置文件路径(默认:{default_config_path()})",
    )
    args = parser.parse_args()
    asyncio.run(amain(args.config), loop_factory=uvloop.new_event_loop)
