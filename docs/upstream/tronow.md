# TRONow 上游对接

接入要求参考以下材料;错误与限流语义以最新[错误码快照](tronow-errors.zh-CN.md)为准:[`.agents/tronow-connect/SKILL.md`](../../.agents/tronow-connect/SKILL.md)(接入要求,含 webhook)、[OpenAPI 1.2.0 快照](tronow-openapi.yaml)(字段契约)、[client.mjs](tronow-client.mjs)(官方签名参考实现)。**OpenAPI 快照未覆盖 webhook,该节内容来自 SKILL。**

## 概览

| 项 | 值 |
| --- | --- |
| Base URL | `https://api.tronow.io/openapi/v1` |
| 产品 | `ENERGY`,租期代码当前仅 `1h` |
| 金额单位 | SUN 整数十进制字符串(`1 TRX = 1,000,000 SUN`),**全程禁用浮点**,用 `int` / `Decimal` |
| 商户门槛 | 注册后分配专属充值地址,累计确认充值 **≥ 20 TRX** 才解锁 API(`INITIAL_DEPOSIT_REQUIRED` = 未达标) |
| IP 白名单 | 凭据可配置出口 IP 白名单(`IP_NOT_ALLOWED` = 不在名单) |
| 限流 | 商户级滚动 1 秒窗:默认 50 认证请求/秒、10 下单/秒,跨 API key 与实例共享;重试与幂等重放也计数 |

## 鉴权与签名

每个请求带 `X-API-Key`、`X-Timestamp`(Unix 毫秒)、`X-Nonce`(商户级唯一,每次 HTTP 尝试新生成,16–128 位 URL-safe)、`X-Signature`(小写 hex);写操作另需 `Idempotency-Key`(稳定 8–128 位可见 ASCII,同一业务请求重试时**保持不变**)。

签名为 HMAC-SHA256(secret 原始字节),对 **7 行 LF 拼接(无结尾换行)** 的规范串:

```
1. 大写 HTTP method
2. 完整转义路径(含 /openapi/v1 前缀)
3. 规范查询串(等价 Go url.Values.Encode:键按字节排序、RFC 3986 严格转义、空格转 +)
4. 时间戳
5. nonce
6. trim 后的幂等键;读操作为空行
7. 所发 body 精确字节的 SHA-256 小写 hex(无 body 为空字符串的哈希)
```

要点:JSON **只序列化一次**,签名字节 = 发送字节;重试换新的时间戳/nonce/签名,业务标识与 body 与幂等键原样保留。Python 参考实现落位 `services/upstream/tronow.py`,编码细节对照 `docs/upstream/tronow-client.mjs` 的 `canonicalQuery`/`sign`。

## 接口与流程

1. **报价**(可选):`GET /prices/quote?resource_type=ENERGY&resource_amount=65000&duration=1h` → `price_sun`/`priced_at`。只读预览,**不产生 quote ID、不锁价**;
2. **下单**:`POST /orders`,body 仅含 `client_order_id`(商户唯一业务单号,1–64 可见 ASCII)、`resource_type`、`receiver_address`、`resource_amount`、`duration`;**不要传 quote_id 或客户端价格**。受理即锁定商户价(`reserved_amount_sun`,此后不变),201 = 新受理、200 = 幂等重放,均返回 `order_id`;
3. **查单**:`GET /orders/{order_id}`(平台单号)或 `GET /orders?client_order_id=...`(对账/超时恢复优先用后者);
4. **地址激活**:`POST /address-activations`(`client_activation_id` + `address`),状态模型与订单相同;
5. **余额**:`GET /account/balance` → `available/reserved/total_balance_sun`。

响应统一信封 `{code, message, request_id, data}`;分支只看 HTTP 状态与 `code`,**绝不解析 `message`**。

## 订单状态

| 状态 | 含义 | 资金 |
| --- | --- | --- |
| `PROCESSING` | 已受理 | 已锁定预留 |
| `CONFIRMING` | 已有供应商交易哈希,链上确认中 | 保持预留 |
| `SUCCESS` | 哈希上链 | 扣款 |
| `FAILED` | 处理终止(如 `CHAIN_TRANSACTION_NOT_FOUND`:确认窗口内哈希未上链) | **已释放** |
| `REVIEWING` | 自动化无法证明安全结果 | 保持预留,转人工 |

不变量:`amount_sun` 为受理时锁定的不可变金额(查询与 webhook 同);锁价不因供应商故障转移而变;**`CONFIRMING` / `REVIEWING` 绝不另开新付费单**;无公开的 `fund_status`,以上表为准。

## Webhook(OpenAPI 快照未含,依 SKILL)

订单终态回调,**仅** `order.succeeded` 与 `order.failed` 两种事件;地址激活无 webhook。

**请求头**:`X-Lease-Timestamp`、`X-Lease-Delivery`(投递 ID)、`X-Lease-Event`(事件名)、`X-Lease-Signature`。

**签名公式**(对原始请求字节,解析 JSON 之前验签):

```
v1=hex(HMAC-SHA256(webhook_secret, timestamp + "." + delivery_id + "." + event_id + "." + exact_raw_body_bytes))
```

`webhook_secret` 与请求签名的 API Secret 是两个独立密钥。

**接收端必须**:

1. 先读原始 body 验签(常数时间比较,`hmac.compare_digest`),再解析 JSON;
2. 时间戳窗口校验(过期拒绝);
3. body 大小上限;
4. **delivery ID 持久化去重**(回调会重复投递,需要一张去重表);
5. `2xx` 只在**业务处理与去重记录共同提交之后**返回;未知/缺字段载荷、事件与状态矛盾返回 422,不记录 delivery;未匹配订单或暂时无法确认租期返回 503,允许重投;
6. webhook 只覆盖两个终态;`CONFIRMING` / `REVIEWING` 仍需轮询兜底。

当前接收端只接受 `order.succeeded` + `SUCCESS` 或 `order.failed` + `FAILED`。
支持订单字段直接位于根对象或 `data` 对象;若根对象含 `event`,必须与签名头一致。
时间戳当前按 Unix 整数秒、±5 分钟校验;上游材料未明确单位,真实联调仍需确认。
成功通知优先使用带时区的 `lease_expires_at`,其次使用 `confirmed_at + 1h`。
两者均缺失时只读查单补全;查单失败、状态未成功或仍无时间时返回 503,不确认送达。
迟到通知的租期已结束时直接经状态机流转到 `expired`,不会从接收时间重新延长租期。

回调 body 携带订单字段(`amount_sun` 为锁定金额);**完整 payload schema 未在材料中给出**,首次联调时以真实回调为准核对并回填本文档。

## 错误处理与恢复

- 信封错误码需显式处理:`INSUFFICIENT_BALANCE`、`IDEMPOTENCY_CONFLICT`、`CLIENT_ORDER_ID_CONFLICT`、`INITIAL_DEPOSIT_REQUIRED`、`IP_NOT_ALLOWED`、鉴权类(`INVALID_CREDENTIALS`/`INVALID_SIGNATURE`/`TIMESTAMP_EXPIRED`/`NONCE_REPLAYED`)、`PRICE_UNAVAILABLE`,未知未来码走安全兜底并保留原码;
- 超时 / 5xx **不代表失败**:先按 `client_order_id` 查单,确认未受理才用原幂等键重试原请求;4xx(除限流)不自动重试;
- 网关可能返回 HTML / 空响应,归入 `HTTP_ERROR` / `INVALID_RESPONSE`(客户端侧兜底码);
- 429 `RATE_LIMITED` 与 503 `RATE_LIMIT_UNAVAILABLE` 在本次业务处理前拦截;
  这不是已受理订单的失败证据。更早请求不明确时仍先查原业务号。
- `Retry-After` 是最低等待秒数,重试增加随机抖动,保持原编号/请求体/幂等键并更新签名材料。
- HTTP 201/200 表示受理/重放;查询 HTTP 200、code=OK、data.status=FAILED 是正常查询结果,
  不能把 failure_code 或上游交付限流当作商户 HTTP 429。
- 日志记录 HTTP status、原 code 和 request_id,采购尝试持久化最近一次这些元数据。
  非标准格式的新 code 由 raw_code 保留,同时使用 HTTP_ERROR/INVALID_RESPONSE 安全兜底;
  两个兜底码是客户端错误,不属于上游业务错误码。

## 与本项目的映射

| 上游概念 | 我们的落点 |
| --- | --- |
| `provider` | `"tronow"`(Order.provider 字段) |
| `client_order_id` | 由本地订单生成(如 `eb-{order.id}`),下单前持久化 |
| `order_id` | `Order.upstream_order_id` |
| 交易哈希 | `Order.upstream_txid` |
| `PROCESSING` / `CONFIRMING` | `delegating` |
| `SUCCESS` | `active` / 已过期则 `expired`;采用上游到期时间或确认时间 + 1h |
| `FAILED` | `failed` |
| `REVIEWING` | 保持 `delegating` + 告警,转人工 |
| `amount_sun` | `Decimal` 换算,`price` 以 TRX 记 |
| 金额预留语义 | 商户余额预留在 TRONow 侧,与我们订单状态机解耦 |

代码落位:客户端 `services/upstream/tronow.py`;webhook 路由 `web/`(读原始 body 验签);delivery 去重表走 Alembic 迁移;轮询任务兜底。配置:`upstream.tronow` 段(base_url/api_key/api_secret/webhook_secret),凭据建议环境变量注入。

## 验收顺序(依 SKILL)

1. 固定向量验签(含乱序查询键、重复键、空格、UTF-8);
2. 只读 `GET /account/balance` 连通(首次真实调用);
3. 幂等重放、超时恢复、重复回调、余额不足、五种状态、未知错误码的模拟测试;
4. **真实付费订单 / 地址激活前,必须获得明确授权**。

## 当前实现边界

客户端业务单号按 1–64 位可见 ASCII 校验,可独立传入 8–128 位 `idempotency_key`。
不传时,长度 ≥ 8 的业务单号继续直接作为幂等键;短单号使用 `energy-bot:` + 业务单号的 SHA-256 小写十六进制摘要。
生成键长于 64 位,与直接使用长业务单号的默认键空间分离,避免两种生成规则碰撞。重试必须保持原业务号、载荷及幂等键。

目前已实现报价/下单/两种查单/余额客户端、终态回调,并接入余额订单的采购请求持久化、
超时恢复、定时查询与 `REVIEWING` 日志/用户通知。余额订单回调持久化唤醒原单查询,
随后以完整查询结果结算;旧订单仍按前述回调直接流转。地址激活接口尚未实现。
参见 [订单系统](../orders.md)。


## 商户共享窗口的本地实现

使用 PostgreSQL 商户行锁和最近一秒请求历史,本地默认 API 50、创建 10。
每次实际 HTTP 尝试都申请额度;POST /orders 和 POST /address-activations 包括重试和
幂等重放同时申请两类额度,GET 查单等只申请 API 额度。
同商户使用同一 PostgreSQL、account_scope 和配置;未填写 scope 时所有 API Key 进入一个保守的默认商户桶,不按 key 拆分配额。

配额头记录为观测快照,Limit 可收紧本地配置上限,Remaining 不授予名额。
429 RATE_LIMITED 的 scope=orders 仅暂停创建,仍允许查单;
scope=requests 暂停全部请求。未知范围、网关 429 和 503 限流服务故障保守暂停全部。
这些客户端保护不能替代上游限制,尤其无法预留同商户其他系统正在消耗的额度。

无单号时自动提交/恢复预算默认为 5（rental.max_submit_attempts）,包括首次提交。
每轮恢复可能只查询而不重放,所以实际 POST 数不会超过预算。
耗尽后继续只读查业务号,不再 POST,不换幂等键或供应商,也不凭次数释放冻结款。
已有单号时始终仅查原单,不受提交次数预算限制。异常订单需要人工核对,
后续查询证明已交付仍可正常结算。


HTTP 层关闭 aiohttp 的隐式连接重试,避免复用旧 nonce 或绕过本地计额。
每次恢复都由订单工作流发起新 HTTP 尝试。受理成功响应的 Retry-After 也会约束下一次查单。
新增迁移 e42447497660 保存窗口历史、配额观测及采购请求诊断;部署前执行 Alembic 升级。

升级时各实例须统一新的商户分组与配置;混用旧版按 key 分桶的消费者无法共享本地额度。
`amount_sun` 是不可变锁定金额,并不表示 FAILED 订单仍有净支出;财务汇总须结合终态,
不能仅累计失败尝试中记录的锁定金额。
