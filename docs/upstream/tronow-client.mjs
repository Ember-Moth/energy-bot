// Node.js 20+; standard library only. Import request() in server-side code.
import { createHash, createHmac, randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";

function queryEncode(value) {
  return encodeURIComponent(value)
    .replace(
      /[!'()*]/g,
      (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase(),
    )
    .replace(/%20/g, "+");
}
export function canonicalQuery(query) {
  const values = new URLSearchParams(query);
  const keys = [...new Set(values.keys())].sort((a, b) =>
    Buffer.compare(Buffer.from(a), Buffer.from(b)),
  );
  return keys
    .flatMap((key) =>
      values
        .getAll(key)
        .map((value) => queryEncode(key) + "=" + queryEncode(value)),
    )
    .join("&");
}
export function sign(
  secret,
  method,
  path,
  query,
  timestamp,
  nonce,
  idempotencyKey,
  body = "",
) {
  const bodyHash = createHash("sha256").update(body).digest("hex");
  const canonical = [
    method.toUpperCase(),
    path,
    canonicalQuery(query),
    timestamp,
    nonce,
    idempotencyKey.trim(),
    bodyHash,
  ].join("\n");
  return createHmac("sha256", secret).update(canonical).digest("hex");
}
export async function request(method, path, data, idempotencyKey = "") {
  const base = (process.env.OPENAPI_BASE_URL || "").replace(/\/$/, "");
  const key = process.env.LEASE_API_KEY,
    secret = process.env.LEASE_API_SECRET;
  if (!base || !key || !secret)
    throw new Error("Set OPENAPI_BASE_URL, LEASE_API_KEY and LEASE_API_SECRET");
  const url = new URL(base + "/" + path);
  if (
    url.protocol !== "https:" ||
    url.origin !== new URL(base).origin ||
    !url.pathname.startsWith("/openapi/v1/") ||
    url.hash ||
    url.username ||
    url.password
  )
    throw new Error("Use an HTTPS OpenAPI v1 endpoint");
  method = method.toUpperCase();
  if (!["GET", "POST"].includes(method)) throw new Error("Unsupported method");
  if (method === "POST" && !/^[\x21-\x7e]{8,128}$/.test(idempotencyKey))
    throw new Error("A stable 8-128 character Idempotency-Key is required");
  const body = data === undefined ? "" : JSON.stringify(data);
  if (method === "GET" && body)
    throw new Error("GET requests must have an empty body");
  const timestamp = String(Date.now()),
    nonce = randomBytes(18).toString("hex");
  const signature = sign(
    secret,
    method,
    url.pathname,
    url.search,
    timestamp,
    nonce,
    idempotencyKey,
    body,
  );
  const response = await fetch(url, {
    method,
    redirect: "error",
    signal: AbortSignal.timeout(10000),
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": key,
      "X-Timestamp": timestamp,
      "X-Nonce": nonce,
      "X-Signature": signature,
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
    },
    ...(method === "POST" ? { body } : {}),
  });
  // A gateway may return HTML or an empty response. Preserve HTTP metadata
  // without exposing that response body or losing the idempotency decision.
  const envelope = await response.json().catch(() => null);
  const structured =
    envelope &&
    typeof envelope === "object" &&
    typeof envelope.code === "string" &&
    /^[A-Z][A-Z0-9_]{0,79}$/.test(envelope.code);
  const requestId =
    typeof envelope?.request_id === "string"
      ? envelope.request_id
      : response.headers.get("X-Request-ID");
  if (
    !response.ok ||
    !structured ||
    envelope.code !== "OK" ||
    !Object.hasOwn(envelope, "data")
  ) {
    const code = !response.ok
      ? structured && envelope.code !== "OK"
        ? envelope.code
        : "HTTP_ERROR"
      : "INVALID_RESPONSE";
    const error = new Error(code);
    Object.assign(error, {
      name: "TRONowAPIError",
      code,
      status: response.status,
      requestId,
      retryAfter: response.headers.get("Retry-After"),
    });
    throw error;
  }
  return {
    data: envelope.data,
    requestId,
    location: response.headers.get("Location"),
    retryAfter: response.headers.get("Retry-After"),
  };
}
// Direct invocation performs only a signed balance lookup. No paid order is sent.
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  request("GET", "account/balance")
    .then((result) => console.log(JSON.stringify(result, null, 2)))
    .catch((error) => {
      console.error(error.message, error.requestId || "");
      process.exitCode = 1;
    });
}
