"""收款网关协议层(epusdt GMPay)。

只做协议:签名规范化、下单、状态查询;入账、去重、回调处理
分别在 services/deposit.py 与 web/gmpay.py。接入方案见 docs/gmpay-integration.md。
"""
