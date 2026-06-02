"""
HTTP API для Mini App: статус заказа (nginx проксирует /api/ → bot:3005).
"""

from __future__ import annotations

import logging

from aiohttp import web

from client_db import get_client, get_orders_for_client
from config import BOT_TOKEN
from order_status import miniapp_status_payload, normalize_status, status_meta
from order_store import get_active_order_by_tg, get_order_for_user, update_order_status
from posiflora import get_order_status
from telegram_auth import tg_user_id_from_init

logger = logging.getLogger(__name__)

INIT_DATA_HEADER = "X-Telegram-Init-Data"


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": f"Content-Type, {INIT_DATA_HEADER}",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
    }


def _init_data(request: web.Request) -> str:
    return request.headers.get(INIT_DATA_HEADER) or request.query.get("initData", "")


def _auth_user(request: web.Request) -> int | None:
    return tg_user_id_from_init(_init_data(request), BOT_TOKEN)


async def handle_options(_request: web.Request) -> web.Response:
    return web.Response(status=204, headers=_cors_headers())


async def _order_payload(redis, order: dict, live_status: str) -> dict:
    details = order.get("details") or {}
    status_info = miniapp_status_payload(live_status, order.get("created_at"))
    recipient = details.get("recipient", "")
    return {
        "order_id": order["order_id"],
        "created_at": order.get("created_at"),
        "details": {
            "name": details.get("name", ""),
            "phone": details.get("phone", ""),
            "recipient": recipient,
            "date": details.get("date", ""),
            "occasion": details.get("occasion", ""),
            "relation": details.get("relation", ""),
            "budget": details.get("budget", ""),
        },
        "status": status_info,
        "status_full": status_meta(live_status),
        "subtitle": f"Букет для {recipient}" if recipient else "",
    }


async def _respond_order_status(
    redis, tg_id: int, order_id: str
) -> web.Response:
    order = await get_order_for_user(redis, order_id, tg_id)
    if not order:
        return web.json_response({"error": "not_found"}, status=404, headers=_cors_headers())

    stored = normalize_status(order.get("status"))
    live = stored
    try:
        live = normalize_status(await get_order_status(order_id))
        if live != stored:
            await update_order_status(redis, order_id, live)
    except Exception:
        logger.debug("Posiflora недоступна для #%s", order_id)

    return web.json_response(
        await _order_payload(redis, order, live),
        headers=_cors_headers(),
    )


async def handle_order_status(request: web.Request) -> web.Response:
    redis = request.app["redis"]
    tg_id = _auth_user(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_cors_headers())

    order_id = request.match_info.get("order_id", "").strip()
    return await _respond_order_status(redis, tg_id, order_id)


async def handle_order_active(request: web.Request) -> web.Response:
    redis = request.app["redis"]
    tg_id = _auth_user(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_cors_headers())

    order = await get_active_order_by_tg(redis, tg_id)
    if not order:
        return web.json_response({"order": None}, headers=_cors_headers())

    order_id = order["order_id"]
    stored = normalize_status(order.get("status"))
    live = stored
    try:
        live = normalize_status(await get_order_status(order_id))
        if live != stored:
            await update_order_status(redis, order_id, live)
    except Exception:
        pass

    payload = await _order_payload(redis, order, live)
    return web.json_response({"order": payload}, headers=_cors_headers())


async def handle_client_me(request: web.Request) -> web.Response:
    tg_id = _auth_user(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_cors_headers())

    client = await get_client(tg_id)
    if not client:
        return web.json_response({"known": False}, headers=_cors_headers())

    return web.json_response(
        {
            "known": True,
            "name": client["name"],
            "phone": client["phone"],
        },
        headers=_cors_headers(),
    )


async def handle_client_orders(request: web.Request) -> web.Response:
    tg_id = _auth_user(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_cors_headers())

    limit = min(int(request.query.get("limit", "10")), 30)
    orders = await get_orders_for_client(tg_id, limit=limit)
    items = []
    for o in orders:
        meta = status_meta(o.get("status", "new"))
        items.append(
            {
                "order_id": o["posiflora_order_id"],
                "recipient": o["recipient"],
                "delivery_date": o["delivery_date"],
                "budget": o["budget"],
                "status": meta,
                "created_at": o.get("created_at"),
            }
        )
    return web.json_response({"orders": items}, headers=_cors_headers())


async def handle_api_order_legacy(request: web.Request) -> web.Response:
    """Совместимость со старым /api/order?order_id=."""
    redis = request.app["redis"]
    tg_id = _auth_user(request)
    if tg_id is None:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_cors_headers())

    order_id = request.query.get("order_id", "").strip()
    if not order_id:
        return web.json_response(
            {"error": "order_id_required"},
            status=400,
            headers=_cors_headers(),
        )
    return await _respond_order_status(redis, tg_id, order_id)


def create_api_app(redis) -> web.Application:
    app = web.Application()
    app["redis"] = redis

    app.router.add_route("OPTIONS", "/api/order-status/{order_id}", handle_options)
    app.router.add_route("OPTIONS", "/api/order/active", handle_options)
    app.router.add_route("OPTIONS", "/api/order", handle_options)
    app.router.add_route("OPTIONS", "/api/client/me", handle_options)
    app.router.add_route("OPTIONS", "/api/client/orders", handle_options)

    app.router.add_get("/api/order-status/{order_id}", handle_order_status)
    app.router.add_get("/api/order/active", handle_order_active)
    app.router.add_get("/api/order", handle_api_order_legacy)
    app.router.add_get("/api/client/me", handle_client_me)
    app.router.add_get("/api/client/orders", handle_client_orders)

    return app


async def start_webapp_server(redis, host: str, port: int) -> web.AppRunner:
    app = create_api_app(redis)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("🌐 API Mini App: http://%s:%s/api/", host, port)
    return runner
