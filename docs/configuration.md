# 配置说明

bot 是打包安装的应用(控制台命令 `energy-bot`),配置文件默认从平台用户配置目录读取,与当前工作目录无关;也可用 `--config` 参数显式指定路径:

| 平台 | 默认路径 |
| --- | --- |
| Linux | `~/.config/energy-bot/config.yaml` |
| macOS | `~/Library/Application Support/energy-bot/config.yaml` |

仅支持 macOS / Linux(uvloop 为无条件依赖,Windows 无法安装)。

```bash
energy-bot                          # 使用默认路径
energy-bot --config /etc/energy-bot/config.yaml   # 指定路径(生产部署常用)
```

配置文件包含 bot token,**config.yaml 已被 gitignore,不要提交到仓库**(模板见仓库根目录的 `config.example.yaml`)。

## 字段一览

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `bot_token` | 是 | — | 从 @BotFather 获取的 bot token |
| `webhook.base_url` | 是 | — | 公网 HTTPS 基地址,Telegram 把更新 POST 到 `{base_url}{path}` |
| `webhook.host` | 否 | `0.0.0.0` | 本地监听地址,仅本机访问可改为 `127.0.0.1` |
| `webhook.port` | 否 | `8080` | 本地监听端口,取值 1–65535 |
| `webhook.path` | 否 | `/webhook` | webhook 路径,不以 `/` 开头会自动补上;建议用随机串 |
| `webhook.secret_token` | 否 | 每次启动随机生成 | 请求校验密钥,详见下文 |

## 校验行为

加载失败时进程会以 `SystemExit` 退出并给出中文提示,包括以下情况:

- 配置文件不存在(提示复制 `config.example.yaml`);
- YAML 语法错误(附带解析器报错详情);
- `bot_token` 缺失或为空;
- `webhook.base_url` 缺失或不是 `https://` 开头(Telegram 强制要求 HTTPS);
- `webhook.port` 不是 1–65535 的整数。

另外:`base_url` 尾部的 `/` 会被自动去掉;未识别的字段会被忽略,不会报错。

## secret_token

Telegram 每次请求 webhook 都会在 `X-Telegram-Bot-Api-Secret-Token` 请求头携带此密钥,服务端校验失败直接返回 401,因此伪造的更新无法进入 bot。

- **留空(默认)**:每次启动自动生成随机密钥并打进日志,仅本次启动有效,无需任何配置即安全;
- **固定填入**:适合多实例部署等需要密钥跨重启稳定的场景,生成方式:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`

## 示例

见 [`config.example.yaml`](../config.example.yaml)。
