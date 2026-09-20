# GMPay(epusdt)收款接入方案

状态:待评审(v3,单币种 TRX)。协议依据:epusdt 仓库 wiki/API.md + 本地源码 `/Users/tschen/epusdt`(已核实,见"源码确证"一节)。

## 目标与边界

为余额订单系统补上**用户充值入口**:用户在 bot 内发起充值(TRX)→ 拿到收款地址与应付 TRX 金额 → 链上转账 → GMPay 回调 → 钱包入账 → 可下单。

**TRX 是唯一币种**:充值、账本、套餐定价、冻结/扣减全以 TRX 记,无任何折算环节。

明确不做:收银台 H5 页面(用户只在 Telegram 内交互)、EPay 兼容模式、OkPay 通道、USDT 及其他链上资产(只接 tron 网络 TRX 原生转账)、汇率相关逻辑。

## 源码确证(文档未覆盖、已核实)

| 问题 | 结论 | 出处 |
| --- | --- | --- |
| 少付/多付会怎样 | 不存在部分支付:链上监听按 `地址+币种+金额` 精确匹配交易锁,金额不符不匹配、订单静默过期;**回调一定意味着足额到账** | `order_data.go:90,460`;`listen_chain_common` |
| TRX 原生转账 | 与 TRC20 同等支持,独立监听路径,token 传 `"trx"`、network 传 `"tron"` 即可 | `task_service.go:128 TryProcessTronTRXTransfer` |
| 回调防重放 | 回调体无时间戳/nonce,只能靠商户侧永久去重 | `worker.go:223` |
| 重试行为 | `callback_num <= maxRetry` 控制调度,未 ack 持续重发,另有管理端手动重发入口;去重必须永久有效 | `order_data.go:209`,`admin/order_controller.go:188` |
| 同一 txid | 网关侧 `block_transaction_id` 已存在即拒绝,链上重放不重复确认 | `order_service.go:262` |
| 签名数字格式 | float64 用 `FormatFloat('f', -1, 64)` 规范化(**验签必须复刻**,最大坑点) | `sign.go:76` |
| 汇率换算 | 网关按商户配置汇率把法币金额折算成币量(`GetRateForCoin`);**TRX 支付侧实收随网关汇率漂移** | `config/rate.go:76-101` |

**关键推论(两种部署形态)**:

**形态 A(推荐):网关 currency 直接设为 `trx`**。`currency` 字段无枚举校验,
而 `GetRateForCoin(coin, base)` 有 **coin == base → 汇率 1** 的短路(`rate.go:82`)。
下单 `currency="trx"` + `token="trx"` 时,`amount` 直接就是 TRX 数量,
actual_amount 精确等于下单金额——**汇率环节彻底消失,金额语义端到端闭环**。

**形态 B:法币币种(usd)+ 汇率配置**。`usd_per_trx` 把用户想充的 TRX 数换成
下单法币金额,网关再按它自己的汇率折算回 TRX;两侧汇率若不严格一致,
实收偏离预期,只能以网关 actual_amount 为准兜底。

默认按形态 A 设计(运维要求:网关侧允许任意 currency 值,经实测确认);
形态 B 仅作为网关侧受限时的退路,代码上只是配置值差异。

## 分层设计(复用 TRONow 模式)

### 1. 配置(`config.py`)

```yaml
payment:
  gmpay:
    base_url: "https://pay.example.com"   # 自部署 epusdt 实例
    pid: "1000"
    secret_key: ""                        # 环境变量注入:ENERGY_BOT_PAYMENT__GMPAY__SECRET_KEY
    currency: "trx"                       # 下单币种:trx = 金额即 TRX 数量(网关 coin==base 短路,汇率 1)
    timeout_seconds: 10
```

`currency="trx"` 时下单 `amount` 直接是 TRX 数量,actual_amount 与其精确相等;
若网关侧限制 currency 取值(退路形态 B),改配 `usd` 并增加 `usd_per_trx` 估算汇率。
新增配置项按硬性约定同步四处:config.py、config.example.yaml、docs/configuration.md、测试。

### 2. 协议层客户端 `services/payment/`(新包,与 services/upstream/ 平级)

只做协议,不含业务决策:

- `create_transaction(order_id, amount, notify_url, token="trx", network="tron")` → 返回 `trade_id / receive_address / actual_amount / payment_url / expiration_time`;
- `check_status(trade_id)` → 状态轮询兜底(无需签名);
- `sign_params(params, secret)` / `verify_callback(payload, secret)` —— 规范化实现**严格复刻** `sign.go`:参数名 ASCII 升序、`key=value` 以 `&` 拼接、剔除空值与 `signature`、float64 按 `FormatFloat('f', -1, 64)` 等价格式化(Python 侧:JSON 数字先转 Decimal 再规范化,拒绝科学计数法与多余尾零,需固定向量测试);
- 错误模型沿用 upstream 客户端:`GmpayApiError(code, status, ...)`,HTTP_ERROR / INVALID_RESPONSE 兜底;不自动重试。

### 3. 数据模型(Alembic 迁移)

**deposit_orders 表**(充值单,与 wallet 流水解耦):

| 列 | 说明 |
| --- | --- |
| `id` | 本地主键 |
| `user_id` | FK users.id |
| `order_id` | 商户单号(≤32 字符,全平台唯一,唯一索引),如 `dep-{user_id}-{uuid8}` |
| `trade_id` | GMPay 平台单号,回调对账键(唯一索引,可空:下单失败时无) |
| `fiat_amount` | 下单法币金额(Numeric(12,6)) |
| `expected_amount` | 应付 TRX(Numeric(20,8),下单响应 actual_amount,**展示给用户的金额**) |
| `receive_address` | 收款地址 |
| `status` | created / paid / expired / failed |
| `block_transaction_id` | 链上 txid(回调带回) |
| `created_at / updated_at / paid_at` | TIMESTAMPTZ |

去重复用现有 `upstream_deliveries` 表(provider=`gmpay`,delivery_id=`gmpay:{trade_id}`),**不建新表**。

入账凭证:`reference = f"gmpay:{trade_id}"`,喂给现有 `wallet.credit()`——天然获得"同凭证重放幂等、换用户/金额拒绝"的资金护栏。

### 4. 金额口径(核心决策)

- **闭环**:形态 A 下,用户输入的 TRX 数量 = 下单 amount = 网关 actual_amount = 入账金额,全链路同一数值,无任何折算;
- **展示**:用户看到的就是他输入的 TRX 数量(以网关返回 actual_amount 复核展示,防御形态 B 退化);
- **入账**:回调 `actual_amount` 必须等于订单 `expected_amount`(epusdt 精确匹配保证,双侧校验防御),相等则**按 `expected_amount` 入账**;不一致 → 告警 + 503 转人工,**不入账**。

### 5. Webhook 接收端 `web/gmpay.py`(复刻 web/tronow.py 的纪律)

`POST /payment/gmpay/notify`:

1. 未配 secret_key → 503 fail-closed;
2. Content-Length 预检 + 64 KiB body 上限;
3. 解析 JSON(epusdt 签名覆盖参数字典而非原始字节,故先解析后验签——与 TRONow 相反的顺序,协议设计如此);
4. 验签:按 pid 查 secret → 规范化参数 → HMAC-SHA256 → `hmac.compare_digest`;失败 401;
5. `status != 2` → 应答 `ok` 但不处理(防御,正常不会收到);
6. 按 `trade_id` 行锁取 deposit_orders;找不到 → 503 换重投(同 TRONow unmatched 语义);
7. 同事务:金额一致性校验(actual_amount == expected_amount,另校验回调 `token` 为 trx)→ `wallet.credit(user_id, expected_amount, reference=f"gmpay:{trade_id}")` → 订单 status=paid、记录 txid → 插 `upstream_deliveries` 去重行 → 提交;
8. 重复 trade_id → 去重命中,直接应答 `ok`;
9. 应答体严格为纯文本 `ok`(epusdt 只认 `ok`/`success`,且要求 HTTP 200)。

与 TRONow webhook 的三处刻意差异:无时间戳窗口(协议没有);验签在 JSON 解析后;应答体是 `ok` 而非 JSON。

### 6. 充值入口 `handlers/deposit.py`

- `/deposit` → 展示用法;
- `/deposit TRX数量`:
  - 创建 deposit_orders 记录(expected_amount = 用户输入的 TRX 数量);
  - 调 GMPay 下单(amount 同值,currency=trx),同一事务提交后回复:收款地址、**应付 TRX**(以网关返回 actual_amount 复核展示)、有效期、付款提示;
  - 回复文案强调**转账金额必须与显示完全一致**(epusdt 靠 `地址+金额` 精确匹配,多付少付都静默过期);
- `/deposit_status 单号` → 本地状态 + 主动 `check_status` 对账一次(回调延迟时的用户自助恢复)。

下单请求号:`request_key=f"tg:{chat_id}:{message_id}"` 模式沿用,重复消息幂等复用。

### 7. 恢复与兜底

- 回调丢失:轮询兜底——复用现有订单工作器模式,对 `created` 超过 N 分钟的充值单定期 `check_status`,发现已支付而本地未入账时走与回调相同的入账路径(reference 幂等保证不重复入账);
- 过期:网关状态 3 → 本地标记 expired,用户重新下单即可;资金未动,无需退款逻辑;
- 金额/币种不符的回调:不丢弃——告警日志 + 订单标 `failed`,人工核对(与 REVIEWING 同一通道)。

### 8. 测试计划

- 签名规范化固定向量(对照 `sign_test.go` 的固定向量 + 自构向量:float 尾零、整数值浮点、空值剔除、大小写敏感);
- 回环伪服务器:下单 / check-status / 验签失败的回调;
- 真实 PG 集成:回调足额入账、重复回调幂等、金额不符拒绝、unmatched 503 换重投、轮询兜底恢复;
- handler 层:金额解析、地址与金额展示、重复消息幂等。

### 9. 风险与决策点

| 项 | 决策 |
| --- | --- |
| 币种 | **仅 TRX**(tron 网络原生转账),账本与套餐天然同币种,零折算 |
| 下单币种 | `currency="trx"`,利用网关 `coin==base → 汇率 1` 短路,amount 即 TRX 数量,汇率环节消失(网关侧若限制 currency 取值,退路为 usd + `usd_per_trx` 估算配置) |
| 回调无防重放窗口 | 接受,永久去重表兜底(已论证资金安全不受影响) |
| 金额不符 | 宁停不错:拒绝入账转人工,不自动按实收入账(防止金额混淆攻击) |

## 实施顺序

1. 配置 + 协议层客户端(签名规范化是核心难点,固定向量先行);
2. 迁移 + deposit_orders 模型 + 充值 handler(下单链路);
3. webhook 接收端(回调链路);
4. 轮询兜底 + 过期处理;
5. 文档(configuration.md / orders.md / deployment.md)+ 全量门禁。

每步独立提交,协议层与 webhook 分开评审。


## 回调挂起排查记录

曾出现第一条成功应答正常、重复回调或后续成功回调等待响应头不返回的现象。
原因是接收端全局复用同一个 `web.Response(text="ok")`:首次发送后它的 EOF 标记已置位,
后续请求复用该对象时 aiohttp 不再准备响应或写出响应头。数据库处理已经结束,
因此 PG 可仅显示 idle/ROLLBACK;单次手动请求和只运行 fixture 都无法暴露此问题。

接收端已改为每次创建新 Response。HTTP 集成测试设置 5 秒总超时,
并覆盖无数据库的连续应答、跨应用实例应答、并发重复通知只入账一次和不同充值单连续入账。
这项修复只解决响应对象生命周期,不代表真实网关付款流程已完成验收。
