# TRONow — 错误码与处理

本表仅适用于 HMAC 商户 API（/openapi/v1）。后台登录、运营管理使用独立鉴权与错误约定。商户身份由 API 凭据决定，请求不能指定其他商户。

一套约定，三个层次：HTTP 状态表示请求结果，code 用于程序分支，message 用于人工阅读。

API 错误返回 {code, message, request_id}，同时使用真正的 HTTP 4xx/5xx 状态。X-Request-ID 与 request_id 一致。错误响应不返回业务 data。message 是安全的英文说明，可能调整，不能用来判断业务分支。

限流按商户共享，涵盖所有 API Key 和服务实例，采用滚动 1 秒窗口。默认每秒 50 次已鉴权 API 请求、10 次下单请求，配额由平台配置。POST /orders 与 POST /address-activations（含重试和幂等重放）同时占用两种配额；查单等读请求仅占 API 配额。X-RateLimit-Limit/Remaining、X-OrderRateLimit-Limit/Remaining 返回当前配额快照，不是预留名额。HTTP 429 RATE_LIMITED 同时返回 Retry-After（秒）与 X-RateLimit-Scope（requests 或 orders）。HTTP 503 RATE_LIMIT_UNAVAILABLE 在业务处理前拦截。已受理订单继续异步处理，上游限流不会被混同为商户 HTTP 429。

POST 结果不明？先用 client_order_id / client_activation_id 查单。超时或 5xx 不代表交易未执行。重试同一请求时保留原编号、请求体和 Idempotency-Key，更新时间戳、nonce 与签名，不能盲目创建另一笔付费订单。

网关与网络可能返回 429/502/504、HTML 或没有响应，不能假设所有失败都是 JSON。遵守 Retry-After，有限退避并记录 request_id。参考客户端以 HTTP_ERROR / INVALID_RESPONSE 兜底异常响应；这两个是客户端代码，不属于 API 业务错误码。收到未识别的新 code 时保留安全兜底，并记录原 HTTP 状态与 code。

```http
HTTP/1.1 422 Unprocessable Entity
X-Request-ID: req_example
Content-Type: application/json

{
  "code": "INSUFFICIENT_BALANCE",
  "message": "Available merchant balance is insufficient.",
  "request_id": "req_example"
}
```

## 请求错误

| 错误码 | HTTP | 含义说明 | 处理建议 |
| --- | --- | --- | --- |
| INVALID_ARGUMENT | 400 | 必填参数缺失或不合法。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_JSON | 400 | 请求体必须是单个 JSON 对象，且不能包含未知字段。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_REQUEST_BODY | 400 | 请求体读取不完整或传输中断。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_IDEMPOTENCY_KEY | 400 | Idempotency-Key 须为 8–128 个可见 ASCII 字符，不含空格。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_CLIENT_ORDER_ID | 400 | client_order_id 须为 1–64 个可见 ASCII 字符，不含空格。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_CLIENT_ACTIVATION_ID | 400 | client_activation_id 须为 1–64 个可见 ASCII 字符，不含空格。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_ORDER_ID | 400 | order_id 不是有效的平台订单编号。 | 使用同一商户接口返回的编号，区分自定义业务编号与平台编号。 |
| INVALID_ACTIVATION_ID | 400 | activation_id 不是有效的平台激活单编号。 | 使用同一商户接口返回的编号，区分自定义业务编号与平台编号。 |
| INVALID_RESOURCE_TYPE | 400 | resource_type 不合法。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_RESOURCE_AMOUNT | 400 | resource_amount 须为支持范围内的正整数。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_DURATION | 400 | duration 不合法。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_RECEIVER_ADDRESS | 400 | receiver_address 须为有效的 TRON 主网地址。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_ACTIVATION_ADDRESS | 400 | address 须为有效的 TRON 主网地址。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| REQUEST_TOO_LARGE | 413 | 请求体超过 1 MiB。 | 按接口约束修正参数后再提交，不要循环重试 4xx。 |
| INVALID_CREDENTIALS | 401 | 鉴权头缺失、格式错误，或 API 凭据无效、停用、过期。 | 检查 API Key、Secret、必要请求头及有效期；凭据仅保存在服务端。 |
| INVALID_SIGNATURE | 401 | HMAC 签名校验失败。 | 核对签名的 HTTP 方法、路径、规范化查询、原始请求体字节和 Secret。 |
| TIMESTAMP_EXPIRED | 401 | X-Timestamp 格式不正确或超出允许的时间窗口。 | 同步服务器时钟，重新生成毫秒时间戳、nonce 和签名。 |
| NONCE_REPLAYED | 401 | 该商户已使用过本次 nonce。 | 若上次下单结果不明，先查单。每次请求使用新 nonce 和签名；重试同一笔业务保留原业务编号、请求体及幂等键。 |
| IP_NOT_ALLOWED | 403 | 请求 IP 不在该 API 凭据的白名单内。 | 将业务服务器真实公网出口 IP 加入白名单。 |
| INITIAL_DEPOSIT_REQUIRED | 403 | 尚未达到 API 服务所需的已确认充值门槛。 | 累计已确认充值达到 20 TRX、API 服务开通后再试。 |
| ORDER_NOT_FOUND | 404 | 当前鉴权商户下未找到该订单。 | 使用同一商户接口返回的编号，区分自定义业务编号与平台编号。 |
| ACTIVATION_NOT_FOUND | 404 | 当前鉴权商户下未找到该激活单。 | 使用同一商户接口返回的编号，区分自定义业务编号与平台编号。 |
| ENDPOINT_NOT_FOUND | 404 | 请求的 API 路径不存在。 | 核对 Base URL 和接口参考中的 /openapi/v1 路径。 |
| METHOD_NOT_ALLOWED | 405 | 该路径不支持当前 HTTP 方法。 | 按文档使用 HTTP 方法；Allow 响应头列出可用方法。 |
| IDEMPOTENCY_CONFLICT | 409 | Idempotency-Key 已被用于不同的请求内容。 | 用原业务编号查单。不要为了绕过冲突或重试结果不明的扣款而更换编号。 |
| CLIENT_ORDER_ID_CONFLICT | 409 | client_order_id 已被用于不同的订单。 | 用原业务编号查单。不要为了绕过冲突或重试结果不明的扣款而更换编号。 |
| CLIENT_ACTIVATION_ID_CONFLICT | 409 | client_activation_id 已被用于不同的激活请求。 | 用原业务编号查单。不要为了绕过冲突或重试结果不明的扣款而更换编号。 |
| INSUFFICIENT_BALANCE | 422 | 商户可用余额不足。 | 充值补足可用余额并核对冻结资金后再提交。 |
| UNSUPPORTED_PRODUCT | 422 | 当前商户暂不支持所请求的产品。 | 核对 resource_type、resource_amount、duration 与账号已开通产品。 |
| PRICE_UNAVAILABLE | 503 | 实时报价暂时不可用。 | 遵守 Retry-After，有限次数退避重试。POST 结果不明先按业务编号查单；重试保留原请求体和幂等键。 |
| ADDRESS_ACTIVATION_UNAVAILABLE | 503 | 地址激活服务暂时不可用。 | 遵守 Retry-After，有限次数退避重试。POST 结果不明先按业务编号查单；重试保留原请求体和幂等键。 |
| AUTH_UNAVAILABLE | 503 | 鉴权服务暂时不可用，并不代表您的密钥无效。 | 遵守 Retry-After，有限次数退避重试。POST 结果不明先按业务编号查单；重试保留原请求体和幂等键。 |
| BALANCE_OVERFLOW | 500 | 余额暂时无法读取。 | 携带 request_id 联系客服；读取失败不代表余额为零。 |
| INTERNAL_ERROR | 500 | 服务器内部错误。 | 遵守 Retry-After，有限次数退避重试。POST 结果不明先按业务编号查单；重试保留原请求体和幂等键。 |
| RATE_LIMITED | 429 | 超过该商户的 API 请求或下单频率限制；本次请求未进入业务处理。 | 至少等待 Retry-After 指定秒数，加入随机抖动并限制重试次数。重试更新 timestamp、nonce、签名，保留业务编号、请求体和幂等键；更早请求若结果不明，先查单。 |
| RATE_LIMIT_UNAVAILABLE | 503 | 商户限流服务暂不可用；本次请求未进入业务处理。 | 至少等待 Retry-After 指定秒数，加入随机抖动并限制重试次数。重试更新 timestamp、nonce、签名，保留业务编号、请求体和幂等键；更早请求若结果不明，先查单。 |

## 异步交付结果

HTTP 201 表示已受理，并不代表交付完成；幂等重放返回 200。查单成功时，即使 data.status=FAILED 也返回 HTTP 200、code=OK。公共状态只有 PROCESSING、CONFIRMING、SUCCESS、FAILED、REVIEWING；SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。failure_code 是稳定原因，txid 用于链上查询。

| 错误码 | 适用业务 | 含义说明 | 处理建议 |
| --- | --- | --- | --- |
| NO_AVAILABLE_ROUTE | 能量订单 | 暂无可用的交付资源。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
| DELIVERY_REJECTED | 能量订单 | 本次交付请求未被受理。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
| CHAIN_TRANSACTION_NOT_FOUND | 能量订单 | 在确认窗口内未能在 TRON 主网查询到对应交易，冻结款已释放。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
| DELIVERY_FAILED | 能量订单 | 订单交付失败。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
| DELIVERY_REVIEW_REQUIRED | 能量订单 | 交付结果需要人工核验。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
| ON_CHAIN_FAILED | 地址激活 | 地址激活交易在链上执行失败。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
| BROADCAST_REJECTED | 地址激活 | 地址激活交易被拒绝。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
| ACTIVATION_FAILED | 地址激活 | 地址激活失败。 | 以 status 为准：SUCCESS 已扣款，FAILED 已释放冻结款，REVIEWING 仍保持冻结。结果未明确前持续查询原单，不要重复发起付费请求。 |
