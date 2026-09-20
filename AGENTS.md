# AGENTS.md

面向 AI 编码代理的项目约定。修改代码前先读完本文件。

## 项目概况

- aiogram 3 Telegram bot(TRON 链能量租赁,对接外部上游供应商),Python ≥ 3.14,uv 管理(src 布局,包名 `energy_bot`)
- **打包安装的应用,三层等价入口**:console script `energy-bot`(pyproject `[project.scripts]` → `energy_bot:main`,生产用)、`python -m energy_bot`(`__main__.py`,开发用)、`main()` 本体在 `__init__.py`(解析参数 → 装 uvloop → 驱动 `app.amain`);加独立命令在 `[project.scripts]` 加行指向包内函数;不要引入对工作目录的依赖
- **webhook 模式**接收更新(aiohttp `SimpleRequestHandler` 承载),不使用 long polling
- 全异步;事件循环固定使用 uvloop(仅支持 macOS / Linux,不考虑 Windows)
- **配置系统**(pydantic-settings):YAML 为主,`ENERGY_BOT_` 前缀环境变量可覆盖(嵌套键用 `__`,如 `ENERGY_BOT_WEBHOOK__PORT`);路径解析:`--config` 参数 > `ENERGY_BOT_CONFIG` 环境变量 > `platformdirs` 平台默认目录;不用 .env 文件;**数据库 DSN 只允许环境变量**(`ENERGY_BOT_DATABASE__DSN`),配置文件用 address/username/password/database 离散字段,出现 `database.dsn` 直接拒绝;密码建议环境变量注入
- **时区统一东八区**(`Asia/Shanghai`,常量在 `config.py` 的 `TIMEZONE`):数据库连接 `server_settings` 固定时区,日志时间戳同源;时间列一律 `TIMESTAMPTZ`
- **日志系统**(`logging_config.py`):stdout 彩色开发格式 / JSON 生产格式(`logging.json_logs`),配置 `logging.log_dir` 则按天轮转保留 30 天(文件固定 JSON);JSON 字段为 `ts/level/logger/msg`
- **优雅停机**:`app.py` 的 `amain()` 用 `AsyncExitStack` 管理资源,注册 SIGTERM 处理器触发停机,退出栈按 LIFO 清理(delete_webhook → aiohttp 下线 → bot 会话关闭)

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
├── app.py         # amain():装配 Bot/Dispatcher/web 服务,AsyncExitStack + SIGTERM 优雅停机
├── config.py      # pydantic-settings 模型与 load_settings(),校验失败以 SystemExit 提示
├── models/        # ORM 模型包:base.py(Base/TimestampMixin)、user.py、order.py;新模型加模块并在 __init__.py 导出
├── db.py          # async engine 与会话工厂,DSN 统一 postgresql+asyncpg
├── repositories/  # 薄数据访问:模块级 async 函数,首参 AsyncSession;无业务规则
├── services/      # 业务工作流:rental.py 订单状态机与流转(status 只准在这里改)
├── logging_config.py # 日志:彩色开发格式 / JSON 生产格式,按天轮转
├── handlers/      # 每个 Router 一个模块,在 __init__.py 的 routers 元组按优先级注册
├── middlewares/   # aiogram 中间件(更新日志、每更新一个 DB 会话)
├── web/           # HTTP 路由:telegram.py(更新接收,密钥校验)、health.py(/healthz)
└── keyboards/     # 键盘定义
tests/             # pytest;pytest-asyncio auto 模式,async 测试直接写
alembic/           # Alembic 迁移(env.py 读应用配置获取 DSN)
docs/              # 配置 / 部署 / 开发 / 提交规范文档
```

新增功能:在 `handlers/` 建模块 → 定义 `router = Router(name="...")` → 加入 `routers` 元组(排前面的先匹配)。

## 硬性约定

- handler 内禁止阻塞调用;同步 SDK、重 IO 用 `asyncio.to_thread` 包裹;
- 数据库:schema 一律经 Alembic——改 `models/` 下对应模块 → `alembic revision --autogenerate` → 人工检查生成脚本 → `upgrade head`;禁止手写 DDL 或启动时自动建表;查询走 ORM,handler 里用 `DbSessionMiddleware` 注入的 `session: AsyncSession`,不要自建连接;
- 分层:handler 解析输入 → 调 `services` → 格式化输出;订单 `status` 只准经 `services/rental.py` 的流转函数修改,并发路径(支付回调/后台任务)先 `get_order_for_update` 行锁;查询第二次复用才下沉 `repositories/`,新查询先写在 service 里;
- 提交信息遵循 Conventional Commits,**英文**,由 `.githooks/commit-msg`(cz check)强制校验;
- 新增配置项时同步更新四处:`config.py` 解析校验、`config.example.yaml`、`docs/configuration.md`、相关测试;
- 注释与文档用中文,可放心使用全角标点(ruff RUF001–003 已忽略);
- 行宽 100,target py314;pyright `standard` 模式必须零错误;
- `config.yaml` 含 bot token,已被 gitignore,严禁提交,也不得把真实 token 写进代码或测试;
- 详细文档在 `docs/`,修改对应行为时同步更新对应文档。
