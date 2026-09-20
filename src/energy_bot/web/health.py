"""健康检查端点,只返回存活状态,不输出路径或错误详情。"""

from aiohttp import web


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"status": "alive"}, headers={"Cache-Control": "no-store"})


def register_health_routes(app: web.Application) -> None:
    app.router.add_get("/healthz", healthz)
