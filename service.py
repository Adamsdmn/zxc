from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import Counter
from decimal import Decimal

import aiohttp
from aiogram import Bot
from aiogram.types import BufferedInputFile

import kb
from checker import check_many, get_real_ip
from config import Settings
from db import PROTO_GROUPS, Order, PRow, Store, utcnow
from payments import CryptoPay
from sources import fetch_all

log = logging.getLogger("proxydrop")


def _rows(rows: list[PRow]):
    return [(r.id, r.ip, r.port, r.protocol) for r in rows]


class Service:
    def __init__(self, cfg: Settings, store: Store, pay: CryptoPay, bot: Bot):
        self.cfg, self.store, self.pay, self.bot = cfg, store, pay, bot
        self.http: aiohttp.ClientSession | None = None
        self._tasks: list[asyncio.Task] = []
        self._fulfil_lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
        self._last_fetch = 0.0
        self.last_fetch_info = "ещё не запускалось"
        self._real_ip: str | None = None
        self._real_ip_ts = 0.0

    # ---------- жизненный цикл ----------

    async def start(self) -> None:
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), headers={"User-Agent": "ProxyDrop/1.0"}
        )
        self._tasks = [
            asyncio.create_task(self._guard(self._proxy_loop), name="proxy_loop"),
            asyncio.create_task(self._guard(self._payment_loop), name="payment_loop"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.http:
            await self.http.close()
        await self.pay.close()

    async def _guard(self, fn) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("фоновая задача %s упала, перезапуск через 30с", fn.__name__)
                await asyncio.sleep(30)

    # ---------- утилиты ----------

    @property
    def cutoff(self) -> dt.datetime:
        return utcnow() - dt.timedelta(minutes=self.cfg.fresh_minutes)

    def total_for(self, qty: int) -> str:
        return format((Decimal(self.cfg.price) * qty).normalize(), "f")

    def price_label(self, qty: int) -> str:
        return f"{self.total_for(qty)} {self.cfg.asset}"

    async def real_ip(self) -> str | None:
        if self._real_ip and time.time() - self._real_ip_ts < 1800:
            return self._real_ip
        ip = await get_real_ip(self.http)
        if ip:
            self._real_ip, self._real_ip_ts = ip, time.time()
        return ip or self._real_ip

    async def safe_send(self, chat_id: int, text: str, **kw) -> bool:
        try:
            await self.bot.send_message(chat_id, text, **kw)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("не смог отправить сообщение %s: %s", chat_id, e)
            return False

    async def notify_admins(self, text: str) -> None:
        for aid in self.cfg.admin_ids:
            await self.safe_send(aid, text)

    # ---------- парсинг и проверка ----------

    async def run_cycle(self, force_fetch: bool = False) -> str:
        async with self._cycle_lock:
            parts: list[str] = []
            if force_fetch or time.time() - self._last_fetch >= self.cfg.fetch_interval:
                recs = await fetch_all(self.http, self.cfg.fetch_limit, self.cfg.extra_sources)
                new = await self.store.upsert_proxies(recs)
                self._last_fetch = time.time()
                self.last_fetch_info = f"{utcnow():%H:%M:%S} UTC: получено {len(recs)}, новых {new}"
                parts.append(f"загружено {len(recs)}, новых {new}")
                removed = await self.store.cleanup()
                if removed:
                    parts.append(f"удалено мёртвых {removed}")

            cands = await self.store.candidates_for_check(
                self.cfg.check_batch,
                recheck_alive_min=max(3, self.cfg.fresh_minutes * 2 // 5),
                retry_dead_min=30,
            )
            if cands:
                res = await check_many(
                    cands, concurrency=self.cfg.check_concurrency,
                    timeout=self.cfg.check_timeout, real_ip=await self.real_ip(),
                )
                await self.store.apply_checks(res)
                alive = sum(1 for _, ok, _ in res if ok)
                parts.append(f"проверено {len(res)}, живых {alive}")
            msg = "; ".join(parts) or "проверять нечего"
            log.info("цикл: %s", msg)
            return msg

    async def _proxy_loop(self) -> None:
        while True:
            await self.run_cycle()
            await asyncio.sleep(self.cfg.cycle_seconds)

    async def pick_checked(self, tg_id: int, proto: str, cc: str, qty: int) -> list[PRow]:
        """Берёт кандидатов из базы и перепроверяет прямо перед выдачей."""
        protos = PROTO_GROUPS[proto]
        picked: list[PRow] = []
        tried: set[int] = set()
        real_ip = await self.real_ip()
        for _ in range(3):
            need = qty - len(picked)
            if need <= 0:
                break
            cands = await self.store.pick_candidates(tg_id, protos, cc, self.cutoff, need * 3, exclude=tried)
            if not cands:
                break
            tried.update(c.id for c in cands)
            res = await check_many(_rows(cands), concurrency=100, timeout=6, real_ip=real_ip)
            await self.store.apply_checks(res)
            ok_ids = {pid for pid, ok, _ in res if ok}
            lat = {pid: l for pid, ok, l in res if ok}
            for c in cands:
                if c.id in ok_ids and len(picked) < qty:
                    picked.append(PRow(c.id, c.ip, c.port, c.protocol, c.country, lat.get(c.id) or c.latency_ms))
        return picked

    # ---------- оплата и выдача ----------

    async def sync_order(self, order_id: int) -> str:
        """Проверяет счёт заказа в CryptoBot. Возвращает актуальный статус заказа."""
        o = await self.store.get_order(order_id)
        if o is None:
            return "missing"
        if o.status == "paid":
            await self.fulfil(order_id)
            return (await self.store.get_order(order_id)).status
        if o.status != "pending" or not o.invoice_id:
            return o.status
        inv = await self.pay.get_invoice(o.invoice_id)
        if inv is None:
            return o.status
        st = inv.get("status")
        if st == "paid":
            try:
                ok = (
                    str(inv.get("asset")) == self.cfg.asset
                    and Decimal(str(inv.get("amount"))) >= Decimal(o.amount)
                )
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                log.error("счёт %s оплачен, но не совпал с заказом #%s: %s", o.invoice_id, o.id, inv)
                await self.notify_admins(f"⚠️ Счёт заказа #{o.id} оплачен, но сумма/валюта не совпала. Проверь вручную.")
                return o.status
            if await self.store.mark_paid(o.id):
                await self.notify_admins(f"💰 Оплачен заказ #{o.id}: {o.qty} шт, {o.amount} {self.cfg.asset}")
                await self.fulfil(o.id)
        elif st == "expired":
            await self.store.set_status(o.id, "expired")
        return (await self.store.get_order(order_id)).status

    async def _payment_loop(self) -> None:
        while True:
            since = utcnow() - dt.timedelta(minutes=90)
            for o in await self.store.pending_orders(since):
                try:
                    await self.sync_order(o.id)
                except Exception:  # noqa: BLE001
                    log.exception("sync_order #%s", o.id)
            for o in await self.store.paid_waiting():
                try:
                    await self.fulfil(o.id)
                except Exception:  # noqa: BLE001
                    log.exception("fulfil #%s", o.id)
            await asyncio.sleep(20)

    async def fulfil(self, order_id: int) -> str:
        async with self._fulfil_lock:
            if not await self.store.claim_for_delivery(order_id):
                return "skip"
            o = await self.store.get_order(order_id)
            try:
                rows = await self.pick_checked(o.tg_id, o.proto, o.cc, o.qty)
            except Exception:
                await self.store.release(order_id)
                raise
            if len(rows) < o.qty:
                await self.store.release(order_id)
                if await self.store.mark_notified(order_id):
                    await self.safe_send(
                        o.tg_id,
                        f"⏳ Заказ #{o.id} оплачен, но свежих прокси под твой фильтр сейчас не хватает. "
                        "Выдам автоматически, как только появятся. Если не хочешь ждать — напиши админу, вернём оплату.",
                    )
                    await self.notify_admins(f"⚠️ Заказ #{o.id} оплачен, но нет остатков ({o.proto}/{o.cc or 'any'} × {o.qty}).")
                return "no_stock"
            await self.store.record_sale(order_id, [r.id for r in rows])
            await self.store.finish_delivery(order_id)
            await self.send_pack(o.tg_id, o.id, rows, "✅ <b>Оплата получена. Твои прокси:</b>")
            return "delivered"

    async def deliver_sample(self, tg_id: int) -> str:
        """Бесплатный пример. Возвращает 'ok' | 'used' | 'no_stock'."""
        if not self.cfg.sample_size:
            return "used"
        if not await self.store.take_sample(tg_id):
            return "used"
        rows = await self.pick_checked(tg_id, "any", "", self.cfg.sample_size)
        if not rows:
            await self.store.give_back_sample(tg_id)  # ничего не выдали — право на пример возвращаем
            return "no_stock"
        oid = await self.store.create_order(tg_id, "any", "", len(rows), "0", status="delivering")
        await self.store.record_sale(oid, [r.id for r in rows])
        await self.store.finish_delivery(oid)
        await self.send_pack(tg_id, oid, rows, "🎁 <b>Твой бесплатный пример:</b>", replace_button=False)
        return "ok"

    async def deliver_gift(self, tg_id: int, qty: int) -> tuple[str, int | None]:
        """Выдаёт подарок: ('ok' | 'no_user' | 'no_stock' | 'send_failed', order_id)."""
        async with self._fulfil_lock:
            if not await self.store.has_user(tg_id):
                return "no_user", None
            available = await self.store.count_available(tg_id, PROTO_GROUPS["any"], "", self.cutoff)
            if available < qty:
                return "no_stock", None
            rows = await self.pick_checked(tg_id, "any", "", qty)
            if len(rows) < qty:
                return "no_stock", None

            order_id = await self.store.create_order(tg_id, "any", "", qty, "0", status="delivering")
            await self.store.record_sale(order_id, [row.id for row in rows])
            await self.store.finish_delivery(order_id)
            try:
                await self.send_pack(
                    tg_id, order_id, rows, "🎁 <b>Тебе подарили прокси:</b>", replace_button=False,
                )
            except Exception:
                log.exception("не удалось отправить подарок пользователю %s (заказ #%s)", tg_id, order_id)
                return "send_failed", order_id
            return "ok", order_id

    async def replace_dead(self, order_id: int) -> tuple[str, int, int]:
        """Проверяет выданные прокси и меняет нерабочие. -> (статус, мёртвых, заменено)"""
        o = await self.store.get_order(order_id)
        rows = await self.store.order_proxies(order_id)
        res = await check_many(_rows(rows), concurrency=50, timeout=8, real_ip=await self.real_ip())
        await self.store.apply_checks(res)
        dead = [pid for pid, ok, _ in res if not ok]
        if not dead:
            return "all_ok", 0, 0
        if not await self.store.claim_replace(order_id):
            return "already", len(dead), 0
        new = await self.pick_checked(o.tg_id, o.proto, o.cc, len(dead))
        if new:
            await self.store.record_sale(order_id, [r.id for r in new], replacement=True)
            await self.send_pack(
                o.tg_id, o.id, new,
                f"🔄 <b>Замена ({len(new)} из {len(dead)} нерабочих):</b>", replace_button=False,
            )
        return "replaced", len(dead), len(new)

    async def send_pack(self, tg_id: int, order_id: int, rows: list[PRow], title: str, replace_button: bool = True) -> None:
        lines = "\n".join(r.url for r in rows)
        protos = ", ".join(sorted({"HTTP/HTTPS" if r.protocol in ("http", "https") else r.protocol.upper() for r in rows}))
        lats = [r.latency_ms for r in rows if r.latency_ms]
        avg = f"{sum(lats) // len(lats)} мс" if lats else "н/д"
        ccs = " ".join(sorted({kb.flag(r.country) for r in rows if r.country}))
        text = (
            f"{title}\n\n<pre>{lines}</pre>\n"
            f"Тип: {protos} · средняя задержка при проверке: {avg} {ccs}\n\n"
            "Проверены прямо перед выдачей. Скорость и доступность могут измениться в любой момент. "
            f"Если часть не работает, в течение {self.cfg.replace_minutes} мин можно заменить их одной кнопкой. "
            "Не пропускай через них пароли и платёжные данные. Не используй для массовой регистрации."
        )
        await self.bot.send_message(tg_id, text, reply_markup=kb.after_pack(order_id, replace_button))
        await self.bot.send_document(
            tg_id, BufferedInputFile(lines.encode(), filename=f"proxydrop_{order_id}.txt")
        )
