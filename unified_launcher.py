"""Run KIWI and the Cedar Cove verification API behind one public port."""

import asyncio
import logging
import os
from multidict import CIMultiDict

from aiohttp import ClientSession, ClientTimeout, web
import uvicorn

BOT_INTERNAL_PORT = int(os.getenv("BOT_INTERNAL_PORT", "18081"))
VERIFY_INTERNAL_PORT = int(os.getenv("VERIFY_INTERNAL_PORT", "18082"))
PUBLIC_PORT = int(os.getenv("PORT", "8080"))

# The dashboard's server-side session check should use the private API listener.
os.environ["AUTH_API_URL"] = f"http://127.0.0.1:{VERIFY_INTERNAL_PORT}"
os.environ.setdefault("CLIENT_IP_HEADER", "x-forwarded-for")

import mega_bot as kiwi  # noqa: E402
import verification_api  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kiwi_unified")

BOT_URL = f"http://127.0.0.1:{BOT_INTERNAL_PORT}"
VERIFY_URL = f"http://127.0.0.1:{VERIFY_INTERNAL_PORT}"

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length", "proxy-connection",
}


async def create_session(app: web.Application):
    app["upstream"] = ClientSession(
        timeout=ClientTimeout(total=120),
        auto_decompress=False,
        headers={"User-Agent": "KIWI-Unified-Proxy/1.0"},
    )


async def close_session(app: web.Application):
    session = app.get("upstream")
    if session and not session.closed:
        await session.close()


async def proxy_request(request: web.Request) -> web.StreamResponse:
    path = request.path
    if path.startswith("/api/auth/") or path.startswith("/api/internal/staff/"):
        upstream = VERIFY_URL
    else:
        upstream = BOT_URL

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_HEADERS
    }
    forwarded_for = request.headers.get("X-Forwarded-For")
    if not forwarded_for and request.remote:
        forwarded_for = request.remote
    if forwarded_for:
        headers["X-Forwarded-For"] = forwarded_for
    headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto", "https")

    body = await request.read() if request.can_read_body else None
    target = f"{upstream}{request.rel_url}"

    try:
        async with request.app["upstream"].request(
            request.method,
            target,
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as response:
            payload = await response.read()
            response_headers = CIMultiDict(
                (key, value)
                for key, value in response.headers.items()
                if key.lower() not in HOP_HEADERS
            )
            return web.Response(
                body=payload,
                status=response.status,
                headers=response_headers,
            )
    except Exception:
        logger.exception("Upstream request failed for %s", path)
        return web.json_response(
            {"error": "The requested service is temporarily unavailable."},
            status=502,
        )


async def combined_health(request: web.Request) -> web.Response:
    session = request.app["upstream"]
    bot_data = {"status": "unavailable"}
    verification_data = {"status": "unavailable"}
    bot_status = 503
    verification_status = 503

    try:
        async with session.get(BOT_URL + "/health") as response:
            bot_status = response.status
            bot_data = await response.json(content_type=None)
    except Exception:
        logger.exception("Could not read KIWI health status")

    try:
        async with session.get(VERIFY_URL + "/health") as response:
            verification_status = response.status
            verification_data = await response.json(content_type=None)
    except Exception:
        logger.exception("Could not read verification health status")

    healthy = bot_status == 200 and verification_status == 200
    return web.json_response(
        {
            "status": "ok" if healthy else "degraded",
            "kiwi": bot_data,
            "verification": verification_data,
        },
        status=200 if healthy else 503,
    )


async def start_internal_bot_server() -> web.AppRunner:
    app = web.Application(client_max_size=30 * 1024 * 1024)
    app.router.add_get("/", kiwi.root_handler)
    app.router.add_get("/health", kiwi.health_handler)
    kiwi.register_staff_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", BOT_INTERNAL_PORT)
    await site.start()
    logger.info("KIWI internal HTTP server listening on 127.0.0.1:%s", BOT_INTERNAL_PORT)
    return runner


async def start_public_proxy() -> web.AppRunner:
    app = web.Application(client_max_size=30 * 1024 * 1024)
    app.on_startup.append(create_session)
    app.on_cleanup.append(close_session)
    app.router.add_get("/health", combined_health)
    app.router.add_route("*", "/{tail:.*}", proxy_request)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PUBLIC_PORT)
    await site.start()
    logger.info("Unified public HTTPS service listening on port %s", PUBLIC_PORT)
    return runner


async def main():
    if not kiwi.DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    if not os.getenv("MONGODB_URI"):
        raise RuntimeError("MONGODB_URI is missing.")

    bot_runner = await start_internal_bot_server()
    proxy_runner = await start_public_proxy()

    api_config = uvicorn.Config(
        verification_api.app,
        host="127.0.0.1",
        port=VERIFY_INTERNAL_PORT,
        log_level="info",
        proxy_headers=False,
        access_log=False,
    )
    api_server = uvicorn.Server(api_config)
    api_task = asyncio.create_task(api_server.serve(), name="verification-api")

    bot_task = None
    try:
        for _ in range(200):
            if api_server.started:
                break
            if api_task.done():
                await api_task
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Verification API did not start.")

        logger.info("Starting KIWI and verification API in one service.")
        bot_task = asyncio.create_task(
            kiwi.bot.start(kiwi.DISCORD_TOKEN),
            name="kiwi-discord-bot",
        )
        await asyncio.gather(bot_task, api_task)
    finally:
        api_server.should_exit = True
        if bot_task and not bot_task.done():
            bot_task.cancel()
        if not api_task.done():
            api_task.cancel()
        await asyncio.gather(
            *(task for task in (bot_task, api_task) if task is not None),
            return_exceptions=True,
        )
        try:
            await kiwi.bot.close()
        except Exception:
            logger.exception("Error closing KIWI")
        await proxy_runner.cleanup()
        await bot_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
