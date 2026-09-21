# 上游能量供应商 API 规范

对接外部上游时以此处的规范为准;文件是原样快照,更新时整份替换并注明版本。

- [TRONow 错误码与处理](tronow-errors.zh-CN.md) — 用户提供的错误/限流规范快照（2026-09-20）
- [TRONow 对接文档](tronow.md) — 签名链、订单流程、状态机、**webhook 验签**与本项目映射的整合说明

| 文件 | 供应商 / 服务 | 版本 | Base URL | 鉴权 |
| --- | --- | --- | --- | --- |
| [tronow-openapi.yaml](tronow-openapi.yaml) | TRONow Merchant OpenAPI | 1.2.0 | `https://api.tronow.io/openapi/v1` | HMAC-SHA256 签名头(`X-API-Key` + `X-Timestamp`/`X-Nonce`/`X-Signature`),写操作需 `Idempotency-Key` |
| [quick-rent-v2.json](quick-rent-v2.json) | TronBid Quick Rent API | 2.0.0 | `https://tronbid.com/api/v2/quick-rent` | `Authorization: Bearer <API_KEY>`,下单带 `idempotency_key` |
| [tronow-client.mjs](tronow-client.mjs) | TRONow 官方签名参考实现(Node 标准库) | — | — | 签名链/规范查询编码/幂等重试的对照基准 |

两家都是「报价 → 幂等下单 → 轮询查单」的异步模型。注意:**TRONow 订单有 webhook 回调**(`order.succeeded` / `order.failed`,`X-Lease-*` 签名头,`v1=HMAC-SHA256(webhook_secret, timestamp "." delivery_id "." event_id "." 原始字节)`,需时间窗 + 常数时间比较 + delivery 去重;OpenAPI YAML 快照未覆盖该部分,以 `.agents/tronow-connect/SKILL.md` 为准);但 `CONFIRMING` / `REVIEWING` 仍需轮询,地址激活无 webhook。TronBid 无 webhook,纯轮询。差异要点:

- **TRONow** 按整单金额(SUN 整数字符串,1 TRX = 10⁶ SUN,禁用浮点)计价,租期代码目前仅 `1h`;下单即锁定商户价格且此后不变,支持按 `client_order_id` 对账;商户级滚动限流(默认 50 请求/秒、10 下单/秒,429 带 `Retry-After`);注册后有专属充值地址,累计确认充值 ≥ 20 TRX 才解锁 API。
- **TronBid** 按 `energy_amount` + `duration_minutes` 计价(TRX 字符串),支持 `onchain` / `balance` 两种支付模式,订单状态含 `pending_payment`(链上待付),支持取消与设置付款地址。

实现上游客户端时放在 `src/energy_bot/services/upstream/`,订单状态映射到我们的 `OrderStatus`(见 `services/rental.py`);TRONow 接入的完整要求(签名细节、恢复策略、验收清单)见 `.agents/tronow-connect/SKILL.md`。

## 当前覆盖范围

TRONow 已有报价、下单、查单、余额与终态回调;TronBid 已有报价、下单、查单、取消、
设置付款地址和余额。TRONow 地址激活、TronBid calculator 尚未封装。
余额订单已接入单上游直采/多上游比价采购、持久化幂等请求、原单恢复、轮询和待核对日志/通知。
订单租期统一为分钟字段(`duration_minutes`),存量小时字段已迁移收敛。两家上游与真实账户的付费联调尚未完成。
协议细节与当前运行边界见 [订单系统](../orders.md)。

错误码表中的 `DELIVERY_REJECTED` 比旧 OpenAPI 枚举更新。保留原 OpenAPI 快照,
客户端不把 failure_code 枚举作为资金结算条件,仍以 HTTP 结果和订单 status 分层处理。
