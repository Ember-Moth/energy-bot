# energy-bot

基于 [aiogram 3](https://aiogram.dev/) 的 Telegram bot:全异步,非 Windows 平台使用 [uvloop](https://github.com/MagicStack/uvloop) 事件循环,webhook 模式接收更新。

## 快速开始

```bash
uv sync                             # 安装依赖
cp config.example.yaml config.yaml  # 填入 token 与 webhook 地址
uv run energy-bot                   # 启动(webhook 模式,需公网 HTTPS)
```

本地调试需要公网隧道,详见[部署指南](docs/deployment.md)。

## 文档

- [配置说明](docs/configuration.md) — `config.yaml` 全部字段、默认值与校验行为
- [部署指南](docs/deployment.md) — 本地隧道调试、反向代理、systemd
- [开发指南](docs/development.md) — 项目结构、添加 handler、工具链
- [提交规范](docs/commit-convention.md) — Conventional Commits(英文提交信息)

## 项目结构

```
src/energy_bot/
├── main.py        # 入口:webhook 服务(aiohttp)+ uvloop
├── config.py      # 读取 config.yaml
├── handlers/      # 业务路由(start、echo 示例)
├── middlewares/   # 中间件(更新日志)
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
