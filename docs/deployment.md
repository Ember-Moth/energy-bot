# 部署指南

bot 以 webhook 模式运行:aiohttp 在本地监听 `webhook.host:webhook.port`,Telegram 把更新 POST 到 `{base_url}{path}`。启动时自动调用 `set_webhook`,进程退出时自动 `delete_webhook`。

## 本地调试

Telegram 要求公网 HTTPS 地址,本地调试需要临时隧道:

```bash
# cloudflared(无需注册)
cloudflared tunnel --url http://localhost:8080
# 或 ngrok
ngrok http 8080
```

把得到的 https 地址填入 `webhook.base_url` 后启动:

```bash
uv run energy-bot --config config.yaml
```

验证:服务日志出现 `webhook 已注册`,`curl http://127.0.0.1:8080/healthz` 返回 `{"status":"alive"}`。

## 生产部署

典型拓扑:**Nginx/Caddy 终止 TLS → 转发到本机 8080**。

### Caddy

```caddy
bot.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### systemd

`/etc/systemd/system/energy-bot.service`:

```ini
[Unit]
Description=energy-bot Telegram bot
After=network-online.target

[Service]
User=energy-bot
# 密钥用环境变量注入,不落盘
Environment=ENERGY_BOT_CONFIG=/etc/energy-bot/config.yaml
Environment=ENERGY_BOT_DATABASE__PASSWORD=CHANGE_ME
ExecStartPre=/opt/energy-bot/.venv/bin/alembic -c /opt/energy-bot/alembic.ini upgrade head
ExecStart=/opt/energy-bot/.venv/bin/energy-bot --config /etc/energy-bot/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

应用是打包安装的,不依赖工作目录;配置统一放 `/etc/energy-bot/config.yaml` 并用 `--config` 指定,也可用 `ENERGY_BOT_CONFIG` 环境变量指定路径。

`systemctl stop` 发送 SIGTERM,应用注册了信号处理器优雅停机:注销 webhook → aiohttp 下线 → 关闭 bot 会话,之后才退出。

## 注意事项

- **健康检查**:`GET /healthz` 返回 `{"status":"alive"}`,可用于负载均衡探活;
- **日志**:默认输出到 stdout,systemd 自动收入 journald(`journalctl -u energy-bot`);需要落盘文件时配置 `logging.log_dir`,按天轮转保留 30 天,文件固定 JSON 格式,详见[配置说明](configuration.md);
- **滚动重启**:停机时会 `delete_webhook`,启动时保留 Telegram 待处理更新(`drop_pending_updates=False`),恢复后继续接收。当前按单实例部署;仅去掉 `delete_webhook` 不能保证零停机,随机密钥切换、并发实例及更新持久化还需另外设计;
- **`webhook.path`**:Telegram 允许任意路径,改成随机串可以在密钥校验之外多一层防护;
- **set_webhook 覆盖**:同一个 bot 只有一个 webhook,重复启动新实例会覆盖旧地址。

## 数据库升级注意事项

`ENERGY_BOT_CONFIG` 同时供 Alembic 和应用使用,`ExecStart` 的 `--config` 不会传给
`ExecStartPre`。用户名及密码可包含空格、`@`、`%` 等字符,迁移直接使用 SQLAlchemy URL
对象,不会将密码脱敏成 `***` 或经过 ConfigParser 插值。

迁移 `5578d0593605` 为 `(provider, upstream_order_id)` 添加唯一约束。
已有相同供应商、相同上游单号的重复记录时升级会失败并回滚,需要先人工核对绑定关系;
迁移不会自动删除或合并订单。不同供应商可以使用相同单号。
