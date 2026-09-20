# AGENTS.md

面向 AI 编码代理的项目约定。修改代码前先读完本文件。

## 项目概况

- aiogram 3 Telegram bot,Python ≥ 3.14,uv 管理(src 布局,包名 `energy_bot`)
- **打包安装的应用,三层等价入口**:console script `energy-bot`(pyproject `[project.scripts]` → `energy_bot:main`,生产用)、`python -m energy_bot`(`__main__.py`,开发用)、`main()` 本体在 `__init__.py`(解析参数 → 装 uvloop → 驱动 `app.amain`);加独立命令在 `[project.scripts]` 加行指向包内函数;不要引入对工作目录的依赖
- **webhook 模式**接收更新(aiohttp `SimpleRequestHandler` 承载),不使用 long polling
- 全异步;事件循环固定使用 uvloop(仅支持 macOS / Linux,不考虑 Windows)
- 配置默认从平台用户配置目录读取(`platformdirs`,见 `config.py` 的 `default_config_path`),`--config` 参数可指定;不使用环境变量 / .env

## 常用命令

```bash
uv sync                            # 安装依赖
uv run energy-bot --config config.yaml  # 启动(开发时指向仓库内配置)
uv run ruff check .                # lint
uv run ruff format .               # 格式化
uv run pyright                     # 类型检查
uv run pytest                      # 测试
uv run cz commit                   # 按 Conventional Commits 交互式提交
```

任何代码改动完成前必须依次通过:`ruff check`、`ruff format --check`、`pyright`、`pytest`。

## 代码结构

```
src/energy_bot/
├── __init__.py    # main() 入口本体(解析参数 → 装 uvloop → 驱动 app.amain)
├── __main__.py    # 支持 python -m energy_bot
├── app.py         # amain():webhook 注册、aiohttp 服务与生命周期(on_startup/on_shutdown)
├── config.py      # load_settings() 解析并校验 config.yaml,失败以 SystemExit 提示
├── handlers/      # 每个 Router 一个模块,在 __init__.py 的 routers 元组按优先级注册
├── middlewares/   # aiogram 中间件
└── keyboards/     # 键盘定义
tests/             # pytest;pytest-asyncio auto 模式,async 测试直接写
docs/              # 配置 / 部署 / 开发 / 提交规范文档
```

新增功能:在 `handlers/` 建模块 → 定义 `router = Router(name="...")` → 加入 `routers` 元组(排前面的先匹配)。

## 硬性约定

- handler 内禁止阻塞调用;同步 SDK、重 IO 用 `asyncio.to_thread` 包裹;
- 提交信息遵循 Conventional Commits,**英文**,由 `.githooks/commit-msg`(cz check)强制校验;
- 新增配置项时同步更新四处:`config.py` 解析校验、`config.example.yaml`、`docs/configuration.md`、相关测试;
- 注释与文档用中文,可放心使用全角标点(ruff RUF001–003 已忽略);
- 行宽 100,target py314;pyright `standard` 模式必须零错误;
- `config.yaml` 含 bot token,已被 gitignore,严禁提交,也不得把真实 token 写进代码或测试;
- 详细文档在 `docs/`,修改对应行为时同步更新对应文档。
