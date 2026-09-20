# 开发指南

## 环境要求

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/)

```bash
uv sync                             # 安装全部依赖(含 dev 组)
cp config.example.yaml config.yaml  # 填入 token 与 webhook 地址(仓库内已 gitignore)
uv run energy-bot --config config.yaml  # 开发时指向仓库内配置
```

应用本身按打包安装方式运行,默认读取平台用户配置目录(见[配置说明](configuration.md));开发时用 `--config` 指向仓库内的 `config.yaml` 最方便。

## 项目结构

```
src/energy_bot/
├── __init__.py    # main():入口本体(解析参数 → 装 uvloop → 驱动 app.amain)
├── __main__.py    # 支持 python -m energy_bot
├── app.py         # amain():装配 Bot/Dispatcher/web 服务,AsyncExitStack + SIGTERM 优雅停机
├── config.py      # 配置解析与校验(默认平台配置目录,--config 可覆盖)
├── handlers/      # 业务路由,新增功能就在这里加模块
│   ├── start.py   # /start、/help
│   └── echo.py    # 示例:回显文本消息
├── middlewares/   # 中间件(当前:更新日志)
├── web/           # HTTP 路由:telegram.py(更新接收,密钥校验)、health.py(/healthz)
└── keyboards/     # 键盘定义
tests/             # pytest 测试
docs/              # 配置 / 部署 / 开发 / 提交规范文档
```

## 入口(三层等价)

这是打包安装的 src-layout 项目,不是单脚本项目,入口有三层:

1. **console script(生产用)**:`pyproject.toml` 的 `[project.scripts]` 声明
   `energy-bot = "energy_bot:main"`,`uv sync` 后生成 `.venv/bin/energy-bot`,
   systemd 的 `ExecStart` 跑的就是它;
2. **模块方式(开发用)**:`python -m energy_bot`(`__main__.py` 一行调用 `main()`);
3. **`main()` 本体**在 `__init__.py`:解析参数 → 装 uvloop → 驱动 `app.amain()`。

以后加独立命令(如备份工具),在 `[project.scripts]` 加一行指向包内函数即可,例如
`energy-bot-backup = "energy_bot.services.backup:main"`。

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
