# energy-bot

TRON 链能量租赁 Telegram bot:基于 [aiogram 3](https://aiogram.dev/),全异步,[uvloop](https://github.com/MagicStack/uvloop) 事件循环,webhook 模式接收更新,SQLAlchemy 2.0(async)+ PostgreSQL 存储,Alembic 管理迁移。仅支持 macOS / Linux 部署。

## 快速开始

```bash
uv sync                                      # 安装依赖
cp config.example.yaml config.yaml           # 填入 token 与 webhook 地址
uv run energy-bot --config config.yaml       # 启动(webhook 模式,需公网 HTTPS)
uv run python -m energy_bot --config config.yaml  # 等价的模块方式
```

打包安装的应用:默认从平台用户配置目录读取配置,`--config` 可指定任意路径,详见[配置说明](docs/configuration.md)。

本地调试需要公网隧道,详见[部署指南](docs/deployment.md)。

## 文档

- [配置说明](docs/configuration.md) — `config.yaml` 全部字段、默认值与校验行为
- [部署指南](docs/deployment.md) — 本地隧道调试、反向代理、systemd
- [开发指南](docs/development.md) — 项目结构、添加 handler、工具链
- [提交规范](docs/commit-convention.md) — Conventional Commits(英文提交信息)
- [上游 API 规范](docs/upstream/) — TRONow / TronBid 两家能量供应商的 OpenAPI 快照

## 项目结构

```
src/energy_bot/
├── __init__.py    # main() 入口(console script 与 python -m 都指向它)
├── __main__.py    # 支持 python -m energy_bot
├── app.py         # amain():装配 Bot/Dispatcher + web 服务,AsyncExitStack 优雅停机
├── config.py      # pydantic-settings 配置(YAML + 环境变量覆盖)
├── models/        # ORM 模型包(base / user / order)
├── db.py          # async engine 与会话工厂
├── repositories/  # 薄数据访问(users / orders)
├── services/      # 业务工作流(订单状态机)
├── handlers/      # 业务路由(start、echo 示例)
├── middlewares/   # 中间件(更新日志)
├── web/           # HTTP 路由:telegram.py(更新接收)、health.py(健康检查)
└── keyboards/     # 键盘定义
tests/             # pytest 测试
docs/              # 文档
```

## 开发

```bash
uv run ruff check . && uv run ruff format .  # lint + 格式化
uv run pyright                               # 类型检查
uv run pytest                                # 测试
uv run cz commit                             # 按规范交互式提交(英文)
```

提交信息遵循 Conventional Commits,由 commit-msg 钩子强制校验,详见[提交规范](docs/commit-convention.md)。

## 协议

[MIT](LICENSE)
