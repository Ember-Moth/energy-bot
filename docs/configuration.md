# 配置说明

bot 是打包安装的应用(控制台命令 `energy-bot`),配置基于 pydantic-settings:

- **配置文件**:YAML 格式,路径解析优先级为 `--config` 参数 > `ENERGY_BOT_CONFIG` 环境变量 > 平台默认目录;
- **环境变量覆盖**:前缀 `ENERGY_BOT_` 的环境变量优先级高于 YAML,嵌套键用双下划线(如 `ENERGY_BOT_WEBHOOK__PORT`),方便注入密钥而不提交到代码库;未覆盖的字段保持 YAML 值。

| 平台 | 默认路径 |
| --- | --- |
| Linux | `~/.config/energy-bot/config.yaml` |
| macOS | `~/Library/Application Support/energy-bot/config.yaml` |

仅支持 macOS / Linux(uvloop 为无条件依赖,Windows 无法安装)。

## 字段一览

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `bot_token` | 是 | 空 | 从 @BotFather 获取;或设 `ENERGY_BOT_BOT_TOKEN` |
| `webhook.base_url` | 是 | 空 | 公网 HTTPS 基地址,Telegram 把更新 POST 到 `{base_url}{path}`;尾部 `/` 自动去除 |
| `webhook.host` | 否 | `127.0.0.1` | 本地监听地址;需直接对外暴露可改 `0.0.0.0`(通常前面有反向代理) |
| `webhook.port` | 否 | `8080` | 本地监听端口,取值 1–65535 |
| `webhook.path` | 否 | `/webhook` | webhook 路径,不以 `/` 开头自动补上;建议用随机串 |
| `webhook.secret_token` | 否 | 每次启动随机生成 | 请求校验密钥,详见下文 |
| `database.address` | 是 | 空 | PostgreSQL 主机 |
| `database.port` | 否 | `5432` | 端口,取值 1–65535 |
| `database.username` | 是 | 空 | 用户名 |
| `database.password` | 否 | 空 | 密码;建议用环境变量注入不落盘:`ENERGY_BOT_DATABASE__PASSWORD` |
| `database.database` | 是 | 空 | 库名 |
| `database.pool_size` | 否 | `5` | 连接池常驻连接数 |
| `database.max_overflow` | 否 | `10` | 峰值时允许的超额连接数 |
| `upstream.tronow.base_url` | 否 | TRONow 官方地址 | 须为 `https://` 且路径以 `/openapi/v1` 开头 |
| `upstream.tronow.api_key` | 是(启用时) | 空 | 商户 API Key;建议环境变量注入:`ENERGY_BOT_UPSTREAM__TRONOW__API_KEY` |
| `upstream.tronow.api_secret` | 是(启用时) | 空 | 请求签名密钥;与 webhook 密钥是两个独立密钥 |
| `upstream.tronow.webhook_secret` | 是(启用回调时) | 空 | 回调验签密钥(`X-Lease-Signature`);留空则回调端点拒绝一切请求(503) |
| `upstream.tronow.timeout_seconds` | 否 | `10.0` | 上游单请求超时,大于 0 且不超过 60 秒 |
| `upstream.tronbid.base_url` | 否 | TronBid 官方地址 | 须为 `https://` 且路径以 `/api/` 开头 |
| `upstream.tronbid.api_key` | 是(启用时) | 空 | API Key(`Authorization: Bearer`);建议环境变量注入:`ENERGY_BOT_UPSTREAM__TRONBID__API_KEY` |
| `upstream.tronbid.timeout_seconds` | 否 | `10.0` | 上游单请求超时,大于 0 且不超过 60 秒 |

**DSN 只允许环境变量**:`ENERGY_BOT_DATABASE__DSN` 提供完整连接串(设置后优先生效,适合密钥管理系统统一注入);配置文件里出现 `database.dsn` 会被直接拒绝,文件里请使用上面的离散字段。

**时区**:项目与数据库统一为东八区(`Asia/Shanghai`)——数据库连接通过 `server_settings` 固定时区(`now()` 等返回 +08 时间),日志时间戳同样使用东八区;时间列均为 `TIMESTAMPTZ`。
| `logging.level` | 否 | `INFO` | 日志级别:`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`,大小写不敏感 |
| `logging.log_dir` | 否 | 空 | 日志文件目录;空表示只输出 stdout,配了则按天轮转保留 30 天,文件固定 JSON |
| `logging.json_logs` | 否 | `false` | stdout 是否用 JSON 格式;开发用彩色文本,生产建议开 |

## 校验行为

字段约束由 pydantic 模型声明(`src/energy_bot/config.py`),加载失败时进程以 `SystemExit` 退出并附上具体的字段错误,包括:

- 配置文件不存在(提示复制 `config.example.yaml` 或用 `--config` / `ENERGY_BOT_CONFIG` 指定);
- YAML 语法错误(附带解析器报错详情);
- `webhook.base_url` 不是 `https://` 开头(Telegram 强制要求 HTTPS);
- `webhook.port` 超出 1–65535;
- `logging.level` 不是合法级别;
- `bot_token` 为空、`webhook.base_url` 为空、数据库连接信息不完整(均在启动时检查)。

## secret_token

Telegram 每次请求 webhook 都会在 `X-Telegram-Bot-Api-Secret-Token` 请求头携带此密钥,服务端校验失败直接返回 401,因此伪造的更新无法进入 bot。

- **留空(默认)**:每次启动自动生成随机密钥并打进日志,仅本次启动有效,无需任何配置即安全;
- **固定填入**:适合多实例部署等需要密钥跨重启稳定的场景,生成方式:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`

## 示例

见 [`config.example.yaml`](../config.example.yaml)。

## 订单系统 rental

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `rental.enabled` | `false` | 开启余额下单与后台采购;默认关闭 |
| `rental.products` | `[]` | 套餐列表,同数量和租期不能重复 |
| `products[].energy_amount` | 必填 | 正整数能量数量 |
| `products[].duration_minutes` | 必填 | 1–525600 分钟 |
| `products[].price_trx` | 必填 | 正数销售价,最多 6 位小数,小于 1,000,000 TRX |
| `products[].max_cost_trx` | 销售价 | 多上游询价成本上限;单上游仅用于成交后告警,不是成交价保证 |
| `rental.poll_seconds` | `5` | 未完成订单的再次查单间隔,1–300 秒 |
| `rental.lease_seconds` | `180` | 工作租约,180–3600 秒,覆盖最大请求超时 |
| `rental.batch_size` | `20` | 单轮维护/tick 的数量上限,1–100;生产消费者持续工作 |
| `rental.max_submit_attempts` | `5` | 无上游单号时的提交/恢复预算,1–100;耗尽转核对,不解冻或换单 |
| `rental.quote_retry_limit` | `3` | 多上游无可采购报价的尝试次数,1–100;耗尽解冻 |
| `rental.order_concurrency` | `8` | 每进程采购消费者数,1–64 |
| `rental.notification_concurrency` | `4` | 每进程通知消费者数,1–32,独立于采购 |
| `rental.idle_poll_seconds` | `1` | 队列为空时等待,0.05–30 秒 |
| `rental.quote_cache_seconds` | `2` | 报价缓存 TTL,0–10 秒;0 关闭 |
| `rental.balance_cache_seconds` | `1` | 上游余额缓存 TTL,0–5 秒;0 关闭 |
| `rental.upstream_concurrency` | `4` | 每进程每供应商 HTTP 并发,1–32 |
| `rental.upstream_requests_per_second` | `20` | TronBid 同商户跨进程总请求平滑速率,1–1000 |
| `rental.upstream_orders_per_second` | `5` | TronBid 同商户跨进程创建订单平滑速率,1–1000 |

示例见 `config.example.yaml`,环境变量仍使用 `ENERGY_BOT_RENTAL__...` 覆盖。
TRONow 需要 api_key 和 api_secret 才进入比价池;TronBid 需要 api_key。
两者均使用经营者的上游预存余额。用户充值渠道不在此配置中。
完整流程、异常恢复和资金语义见 [订单系统](orders.md)。
仅配置一家凭据齐全的供应商时自动直采,不调用报价或上游余额接口;
配置两家时继续比价。判断以配置数量为准,不是某次报价成功的数量。



### TRONow 商户限流

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `upstream.tronow.account_scope` | 空 | 本地商户分组,不发送上游;空时所有 TRONow key 共用默认桶 |
| `upstream.tronow.request_limit` | `50` | 本地滚动 1 秒 API 上限,1–1000 |
| `upstream.tronow.order_limit` | `10` | 本地滚动 1 秒创建上限,1–1000;订单和地址激活合计 |

TRONow 以商户共享配额,同商户所有 API Key/服务实例应使用相同 account_scope、限额和
同一 PostgreSQL。不同商户要指定不同的本地 scope,避免默认桶造成保守的共享节流。
它不改变由 API 凭据决定的商户身份。平台实际配额仍是最终限制。
已观察到的 Limit 头可保守降低本地上限;Remaining 只记录快照,不作为预留令牌。
TRONow 不使用 rental 下的平滑速率配置,这两个配置继续控制 TronBid。

`upstream.tronbid.account_scope` 默认为空,空时按 API key 隔离;
同一 TronBid 账户跨 key 的实例也应显式使用相同 scope 和速率。
缓存始终按凭据与规格隔离,限流分组不会合并不同凭据的缓存。
UNLOGGED 缓存只连接主库,不得用于用户账本、订单状态或采购幂等记录。
并发及基准说明见 [性能与调度](performance.md)。
