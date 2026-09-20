# 开发指南

## 环境要求

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/)

```bash
uv sync                          # 安装全部依赖(含 dev 组)
cp config.example.yaml config.yaml  # 填入 token 与 webhook 地址
uv run energy-bot                # 启动
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
tests/                    # pytest 测试
```

## 添加新功能

1. 在 `handlers/` 下新建模块,定义 `router = Router(name="...")` 并编写 handler;
2. 在 `handlers/__init__.py` 的 `routers` 中按优先级注册(排前面的先匹配);
3. handler 里的阻塞调用(同步 SDK、重计算)必须用 `asyncio.to_thread` 包裹,不要阻塞事件循环。

## 工具链

```bash
uv run ruff check .        # lint
uv run ruff format .       # 格式化
uv run pyright             # 类型检查
uv run pytest              # 运行测试
uv run cz commit           # 按 Conventional Commits 交互式提交
```

配置都在 `pyproject.toml`。ruff 的 lint 已启用 `E/W/F/I/UP/B/SIM/ASYNC/RUF`,其中 RUF001–003(中英文易混淆标点)已关闭,注释和文案可放心使用全角标点;pyright 为 `standard` 级别。

## 测试

pytest 已启用 `asyncio_mode = "auto"`,直接写 `async def test_...` 即可,无需装饰器。配置加载相关测试通过 `tmp_path` 写临时 YAML,不依赖真实配置文件。
