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

## 余额订单

已提供用户账本、按套餐冻结余额、单上游直采/多上游比价采购、原单恢复、回调/轮询对账和订单通知。
使用 `/rent`、`/balance`、`/orders`、`/order`、`/cancel_order`,默认不启用真实采购。
先配置套餐和上游余额,再启用 `rental.enabled`;TRX 充值经 GMPay 网关(`/deposit`、`/deposit_status`,
接入方案见 [GMPay 接入](docs/gmpay-integration.md))。
详见 [订单系统](docs/orders.md)。

## 文档

- [配置说明](docs/configuration.md) — `config.yaml` 全部字段、默认值与校验行为
- [部署指南](docs/deployment.md) — 本地隧道调试、反向代理、systemd
- [性能与调度](docs/performance.md) — 并发队列、UNLOGGED 缓存、限流及本地对照基准
- [开发指南](docs/development.md) — 项目结构、添加 handler、工具链
- [提交规范](docs/commit-convention.md) — Conventional Commits(英文提交信息)
- [上游 API 规范](docs/upstream/) — TRONow / TronBid 两家能量供应商的 OpenAPI 快照

## 项目结构

```
src/energy_bot/
├── __init__.py          # main() 入口:解析参数、安装 uvloop、驱动 app.amain
├── __main__.py          # 支持 python -m energy_bot
├── app.py               # 装配 Bot/Dispatcher、HTTP 服务和采购工作器,管理优雅停机
├── config.py            # pydantic-settings 配置(YAML + 环境变量覆盖)
├── logging_config.py    # stdout/JSON 日志与按天轮转
├── db.py                # SQLAlchemy async engine 与会话工厂
├── models/              # 用户、订单、钱包、充值、采购、上游缓存等 ORM 模型
├── repositories/        # users/orders 薄数据访问层
├── services/
│   ├── rental.py        # 订单状态机、余额冻结与采购结算
│   ├── procurement.py   # 持久化采购及通知工作器
│   ├── wallet.py        # 用户余额账本
│   ├── deposit.py       # TRX 充值编排
│   ├── providers.py     # 上游统一协议、直采与比价适配
│   ├── upstream/        # TRONow / TronBid 异步客户端
│   └── payment/         # GMPay(epusdt)收款客户端
├── handlers/            # /start、/rent、/balance、/orders、/deposit 等 Telegram 路由
├── middlewares/         # 更新日志与每次更新一个数据库会话
├── web/                 # Telegram、TRONow、GMPay 回调和 /healthz
└── keyboards/           # Telegram 键盘定义
alembic/                 # 数据库迁移及版本脚本
tests/                   # 状态机、并发、上游、handler 与 webhook 测试
scripts/                 # 订单处理性能基准
docs/                    # 配置、部署、订单、性能与上游接口文档
config.example.yaml      # 无敏感信息的配置示例
pyproject.toml           # 包元数据、依赖和开发工具配置
```

主要调用链为 `Webhook → Middleware → Handler → Service → Repository/ORM → PostgreSQL`。
启用租赁后,`OrderWorker` 会在后台处理询价/直采、失败恢复、轮询对账和消息通知;
仅配置一家上游时直接采购,同时配置 TRONow 与 TronBid 时按预算选择最低报价。

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
