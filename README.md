# energy-bot

基于 [aiogram 3](https://aiogram.dev/) 的 Telegram bot,全异步,非 Windows 平台使用 [uvloop](https://github.com/MagicStack/uvloop) 事件循环。

## 快速开始

```bash
# 安装依赖
uv sync

# 配置 token(从 @BotFather 获取)
cp config.example.yaml config.yaml

# 启动(webhook 模式,需公网 HTTPS)
uv run energy-bot
# 或
uv run python -m energy_bot
```

## 项目结构

```
src/energy_bot/
├── main.py               # 入口:webhook 模式(aiohttp 接收更新),uvloop 事件循环
├── config.py             # 从 config.yaml 读取配置(默认工作目录下查找)
├── handlers/             # 业务路由,新增功能就在这里加模块
│   ├── start.py          # /start、/help
│   └── echo.py           # 示例:回显文本消息
├── middlewares/          # 中间件(当前:更新日志)
└── keyboards/            # 键盘定义
```

## 添加新功能

1. 在 `handlers/` 下新建模块,定义 `router = Router(name="...")` 并编写 handler;
2. 在 `handlers/__init__.py` 的 `routers` 中按优先级注册。

## 开发工具链

```bash
uv run ruff check .        # lint
uv run ruff format .       # 格式化
uv run pyright             # 类型检查
uv run pytest              # 运行测试(tests/)
```

配置均位于 `pyproject.toml`(`[tool.ruff]`、`[tool.pyright]`、`[tool.pytest.ini_options]`)。
pytest 已启用 `asyncio_mode = "auto"`,直接写 `async def test_...` 即可测异步代码。
