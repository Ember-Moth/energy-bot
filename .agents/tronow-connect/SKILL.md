---
name: tronow-connect
description: Integrate TRONow merchant OpenAPI v1 into a server-side application, including HMAC authentication, Energy orders, status tracking and signed webhooks.
---

# TRONow merchant integration

请在我们现有服务端项目中实现 TRONow 商户 OpenAPI 接入，沿用当前语言和架构。交付客户端、环境变量模板、报价/下单/查单流程、Webhook 验签及模拟测试。真实付费验收前需要获得明确授权。

Base URL: https://api.tronow.io/openapi/v1
Documentation language: zh-CN

Integrate with the merchant's existing server framework. Read the accompanying OpenAPI YAML as the canonical field contract. Keep `OPENAPI_BASE_URL`, `LEASE_API_KEY` and `LEASE_API_SECRET` in server-side environment configuration. Never expose credentials in browser code, source control, logs, screenshots or prompts.

## Access, units and products

A dedicated deposit address is assigned automatically after registration. Confirmed deposits must total at least 20 TRX before API service is unlocked; `INITIAL_DEPOSIT_REQUIRED` means onboarding is incomplete. All `*_sun` values are decimal integer strings: 1 TRX = 1,000,000 SUN. Use integers or BigInt, never floating-point money. The current product is `ENERGY` for `1h`; quantities remain subject to each merchant's enabled product limits.

## Request authentication

Every request requires `X-API-Key`, Unix-millisecond `X-Timestamp`, a fresh merchant-wide `X-Nonce`, and lowercase-hex `X-Signature`. Creation also requires a stable 8–128 character `Idempotency-Key`.

Sign exactly seven LF-separated lines, with no trailing LF:

1. uppercase method;
2. full escaped path, including `/openapi/v1`;
3. canonical query equivalent to Go `url.Values.Encode()`;
4. timestamp;
5. nonce;
6. trimmed idempotency key, or an empty line for reads;
7. lowercase hex SHA-256 of the exact transmitted body bytes.

Use HMAC-SHA256 with the Secret's raw string bytes. Serialize JSON once, sign those bytes, and send those exact bytes. A retry keeps the same business ID, body and idempotency key, but generates a new timestamp, nonce and signature.

## Minimal order flow

1. Optionally call `GET /prices/quote?resource_type=ENERGY&resource_amount=65000&duration=1h`. It is a read-only current-price preview and returns `resource_type`, `resource_amount`, `duration`, `price_sun`, `currency` and `priced_at`. It creates no quote ID and does not lock a price.
2. Call `POST /orders` with `client_order_id`, `resource_type`, `receiver_address`, `resource_amount` and `duration`. Do not send `quote_id` or a client price.
3. The platform recalculates and locks the authoritative merchant price when the POST is accepted. The response returns `order_id`, `client_order_id`, `status`, `reserved_amount_sun`, `currency` and `created_at`.
4. Persist the platform order ID and query `GET /orders/{order_id}`. After a timeout, recover with `GET /orders?client_order_id=...` before retrying the exact POST.

The locked merchant amount never changes during provider fallback. Internal `allow_loss` policy only decides whether TRONow may use a backup provider whose cost exceeds the locked revenue; it is not a public field and never changes the merchant charge.

## Public order states

- `PROCESSING`: accepted; the locked amount is reserved.
- `CONFIRMING`: a provider transaction hash is available and is being checked on TRON; funds remain reserved.
- `SUCCESS`: the hash is found on TRON and the locked amount is captured.
- `FAILED`: processing ended and the reserved amount was released. `CHAIN_TRANSACTION_NOT_FOUND` means the provider hash was still absent from TRON after the bounded confirmation window.
- `REVIEWING`: automation cannot prove a safe result; funds remain reserved for manual review.

The public contract deliberately has no `fund_status`. Use the status invariants above. `amount_sun` in order queries and webhooks is the immutable amount locked at acceptance. Never create a replacement paid order for `CONFIRMING` or `REVIEWING`.

## Address activation and balance

- `POST /address-activations` accepts `client_activation_id` and `address`; track it with either activation query route. The same five public states apply.
- `GET /account/balance` returns `available_balance_sun`, `reserved_balance_sun`, `total_balance_sun`, `currency` and `updated_at`.

## HTTP and retry behavior

Creation returns 201 for a new acceptance and 200 for an idempotent replay. Both mean accepted, not delivered. Persist the client identifier, idempotency key, exact body and platform identifier. A timeout or 5xx does not prove failure. First query by client identifier, then retry the original request if it was not accepted. Do not automatically retry all 4xx responses.

All API responses use the stable `code`, human `message`, `request_id` and success `data` envelope. Branch on HTTP status and `code`; never parse `message`. Respect `Retry-After` and the merchant-wide rate-limit headers. Handle `INSUFFICIENT_BALANCE`, `IDEMPOTENCY_CONFLICT`, `CLIENT_ORDER_ID_CONFLICT`, `INITIAL_DEPOSIT_REQUIRED`, `IP_NOT_ALLOWED`, authentication errors, `PRICE_UNAVAILABLE` and unknown future codes explicitly.

## Webhooks

Order callbacks use `order.succeeded` and `order.failed`. Verify `X-Lease-Timestamp`, `X-Lease-Delivery`, `X-Lease-Event` and `X-Lease-Signature` against the exact raw body before parsing JSON. The signature is:

`v1=hex(HMAC-SHA256(webhook_secret, timestamp + "." + delivery_id + "." + event_id + "." + exact_body_bytes))`

Apply a timestamp window, constant-time comparison, body-size limit and durable delivery-ID deduplication. Return 2xx only after durable acceptance. Callbacks can repeat. Poll for `CONFIRMING` and `REVIEWING`; address activation has no webhook.

## Verification checklist

- Validate signatures with fixed vectors, reordered query keys, repeated keys, spaces and UTF-8.
- Test idempotent replay, timeout recovery, duplicate callbacks, insufficient balance, all five public states and unknown error codes.
- Assert that the GET price preview writes no quote row.
- Assert that POST locks one immutable amount and backup-provider routing never changes it.
- Assert that a hash found on TRON captures funds, clean not-found at the retry boundary releases funds, and RPC errors enter `REVIEWING` without releasing funds.

Use the downloadable `client.mjs` as a Node.js standard-library signing reference. Begin real verification with the read-only balance endpoint. Obtain explicit authorization before any paid production order or address activation.
