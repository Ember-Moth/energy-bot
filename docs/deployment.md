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
uv run energy-bot
```

验证:服务日志出现 `webhook 已设置`,`curl http://127.0.0.1:8080/healthz` 返回 `ok`。

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
ExecStart=/opt/energy-bot/.venv/bin/energy-bot --config /etc/energy-bot/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

应用是打包安装的,不依赖工作目录;配置统一放 `/etc/energy-bot/config.yaml` 并用 `--config` 指定。

## 注意事项

- **健康检查**:`GET /healthz` 返回 `ok`,可用于负载均衡探活;
- **滚动重启**:退出时会 `delete_webhook`,重启间隙消息会短暂中断;如需零停机部署,注释掉 `app.py` 中 `on_shutdown` 里的 `delete_webhook` 调用;
- **`webhook.path`**:Telegram 允许任意路径,改成随机串可以在密钥校验之外多一层防护;
- **set_webhook 覆盖**:同一个 bot 只有一个 webhook,重复启动新实例会覆盖旧地址。
