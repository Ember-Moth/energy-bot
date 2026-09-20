# 开发指南

## 环境要求

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/)

```bash
uv sync                             # 安装全部依赖(含 dev 组)
cp config.example.yaml config.yaml  # 填入 token、webhook 地址与数据库连接信息
uv run energy-bot --config config.yaml  # 开发时指向仓库内配置
```

需要本地 PostgreSQL,最简单的方式:

```bash
docker run -d --name energy-bot-pg -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=energy_bot -p 5432:5432 postgres:17
# 对应 DSN: postgresql://postgres:dev@localhost:5432/energy_bot
```

数据库迁移由 Alembic 管理(schema 变更见下节),启动时不会自动建表。

## 数据库迁移(Alembic)

模型集中在 `src/energy_bot/models/` 包(每类实体一个模块,公共时间列用 `TimestampMixin`),schema 变更流程:

```bash
# 1. 修改 models/ 下对应模块(新实体:建模块并在 models/__init__.py 导出)
# 2. 自动生成迁移(DSN 从应用配置读取,可用 ENERGY_BOT_CONFIG / ENERGY_BOT_DATABASE__DSN 覆盖)
uv run alembic revision --autogenerate -m "add xxx table"
# 3. 人工检查 alembic/versions/ 下生成的脚本
# 4. 应用
uv run alembic upgrade head
```

部署环境从任意目录执行:`alembic -c /opt/energy-bot/alembic.ini upgrade head`(ini 用 `%(here)s` 定位脚本目录,不依赖工作目录)。

应用本身按打包安装方式运行,默认读取平台用户配置目录(见[配置说明](configuration.md));开发时用 `--config` 指向仓库内的 `config.yaml` 最方便。

## 项目结构

```
src/energy_bot/
├── __init__.py    # main():入口本体(解析参数 → 装 uvloop → 驱动 app.amain)
├── __main__.py    # 支持 python -m energy_bot
├── app.py         # amain():装配 Bot/Dispatcher/web 服务,AsyncExitStack + SIGTERM 优雅停机
├── config.py      # pydantic-settings 模型与 load_settings(),校验失败以 SystemExit 提示
├── models/        # ORM 模型包:base.py(Base/TimestampMixin)、user.py、order.py
├── db.py          # async engine 与会话工厂(DSN 统一走 asyncpg 驱动)
├── repositories/  # 薄数据访问:模块级 async 函数,首参 AsyncSession;无业务规则
├── services/      # 业务工作流:rental.py 订单状态机与流转;upstream/ 上游客户端(tronow.py、tronbid.py)
├── handlers/      # 业务路由,新增功能就在这里加模块
│   ├── start.py   # /start、/help
│   └── echo.py    # 示例:回显文本消息
├── middlewares/   # 中间件(当前:更新日志)
├── web/           # HTTP 路由:telegram.py(更新接收,密钥校验)、health.py(/healthz)、tronow.py(上游回调验签)
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
