from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from config import Settings, load_settings
from db import Store
from handlers import build_router
from payments import CryptoPay
from service import Service

log = logging.getLogger("proxydrop")


def build(cfg: Settings):
    store = Store(cfg.database_url)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pay = CryptoPay(cfg.cryptopay_token, cfg.cryptopay_testnet)
    svc = Service(cfg, store, pay, bot)
    dp = Dispatcher()
    dp.include_router(build_router(svc))
    return store, bot, svc, dp


async def run_polling(cfg: Settings) -> None:
    store, bot, svc, dp = build(cfg)
    await store.init()
    await svc.start()
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("запущен в режиме polling")
    try:
        await dp.start_polling(bot)
    finally:
        await svc.stop()
        await bot.session.close()


def run_webhook(cfg: Settings) -> None:
    store, bot, svc, dp = build(cfg)
    url = f"{cfg.webhook_base_url}/webhook"

    async def on_startup(app: web.Application) -> None:
        await store.init()
        await svc.start()
        await bot.set_webhook(url, secret_token=cfg.webhook_secret, allowed_updates=dp.resolve_used_update_types())
        log.info("запущен в режиме webhook: %s", url)

    async def on_cleanup(app: web.Application) -> None:
        await svc.stop()
        await bot.session.close()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=cfg.webhook_secret).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=cfg.port)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_settings()
    if cfg.webhook_base_url:
        run_webhook(cfg)
    else:
        asyncio.run(run_polling(cfg))


if __name__ == "__main__":
    main()
