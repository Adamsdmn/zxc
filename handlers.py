from __future__ import annotations

import datetime as dt
import html
import logging
import re

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import kb
from db import PROTO_GROUPS, utcnow
from payments import CryptoPayError
from service import Service

log = logging.getLogger("proxydrop.handlers")
CC_RE = re.compile(r"^[a-z]{2}$")

STATUS_LABEL = {
    "pending": "⏳ ждёт оплаты",
    "paid": "💰 оплачен, ждёт остатков",
    "delivering": "📤 выдаётся",
    "delivered": "✅ выдан",
    "expired": "⌛ счёт истёк",
}


class SubscriptionMiddleware(BaseMiddleware):
    channel = "@EIiteProxy"

    async def __call__(self, handler, event, data):
        if event.from_user is None:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "sub:check":
            return await handler(event, data)

        try:
            member = await data["bot"].get_chat_member(self.channel, event.from_user.id)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось проверить подписку пользователя %s: %s", event.from_user.id, e)
            text = "Не удалось проверить подписку. Убедись, что бот добавлен администратором канала, и попробуй снова."
        else:
            if member.status in {"member", "administrator", "creator"} or getattr(member, "is_member", False):
                return await handler(event, data)
            text = "Чтобы пользоваться ботом, подпишись на официальный канал и нажми «Проверить подписку»."

        if isinstance(event, CallbackQuery):
            await event.answer("Подпишись на канал и проверь подписку.", show_alert=True)
            await data["bot"].send_message(event.from_user.id, text, reply_markup=kb.subscription_menu())
        else:
            await event.answer(text, reply_markup=kb.subscription_menu())


def build_router(svc: Service) -> Router:
    r = Router()
    cfg, store = svc.cfg, svc.store
    subscription = SubscriptionMiddleware()
    r.message.outer_middleware(subscription)
    r.callback_query.outer_middleware(subscription)

    def welcome() -> str:
        return (
            "<b>ProxyDrop</b> — недорогие проверенные прокси для просмотра сайтов "
            "и проверки доступности контента\n\n"
            f"Цена: <b>{svc.price_label(1)}</b> за прокси, оплата в @CryptoBot. "
            "Каждый прокси проверяется реальным запросом прямо перед выдачей.\n\n"
            "⚠️ Прокси недорогие, поэтому скорость и стабильность могут меняться; "
            "не используй их для паролей, платежей и других чувствительных данных. "
            f"Если часть умерла, в течение {cfg.replace_minutes} мин можно бесплатно заменить нерабочие.\n\n"
        )

    async def show(cb: CallbackQuery, text: str, markup=None) -> None:
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await cb.message.answer(text, reply_markup=markup)

    async def home_markup(uid: int):
        return kb.main_menu(cfg.sample_size, await store.sample_used(uid))

    # ---------- меню ----------

    @r.message(CommandStart())
    async def start(m: Message):
        await store.ensure_user(m.from_user.id, m.from_user.username)
        await m.answer(welcome(), reply_markup=await home_markup(m.from_user.id))

    @r.callback_query(F.data == "sub:check")
    async def check_subscription(cb: CallbackQuery):
        try:
            member = await cb.bot.get_chat_member(SubscriptionMiddleware.channel, cb.from_user.id)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось проверить подписку пользователя %s: %s", cb.from_user.id, e)
            return await cb.answer("Не удалось проверить подписку. Попробуй позже.", show_alert=True)
        if member.status not in {"member", "administrator", "creator"} and not getattr(member, "is_member", False):
            return await cb.answer("Подписка пока не найдена. Подпишись и нажми ещё раз.", show_alert=True)
        await store.ensure_user(cb.from_user.id, cb.from_user.username)
        await cb.answer("Подписка подтверждена")
        await show(cb, welcome(), await home_markup(cb.from_user.id))

    @r.callback_query(F.data == "m:home")
    async def home(cb: CallbackQuery):
        await cb.answer()
        await show(cb, welcome(), await home_markup(cb.from_user.id))

    @r.callback_query(F.data == "m:stock")
    async def stock(cb: CallbackQuery):
        await cb.answer()
        counts = await store.stock_counts(svc.cutoff)
        http = counts.get("http", 0) + counts.get("https", 0)
        text = (
            f"📊 <b>Сейчас в наличии</b> (проверены за последние {cfg.fresh_minutes} мин)\n\n"
            f"HTTP/HTTPS: {http}\nSOCKS5: {counts.get('socks5', 0)}\nSOCKS4: {counts.get('socks4', 0)}\n\n"
            "Перед выдачей прокси проверяются ещё раз."
        )
        await show(cb, text, kb.home_only())

    @r.callback_query(F.data == "m:sample")
    async def sample(cb: CallbackQuery):
        await cb.answer("Проверяю прокси…")
        res = await svc.deliver_sample(cb.from_user.id)
        if res == "ok":
            return
        text = (
            "Пример ты уже получал 🙂" if res == "used"
            else "Сейчас нет свежих прокси для примера, попробуй через пару минут."
        )
        await show(cb, text, await home_markup(cb.from_user.id))

    @r.callback_query(F.data == "m:orders")
    async def orders(cb: CallbackQuery):
        await cb.answer()
        items = await store.user_orders(cb.from_user.id)
        if not items:
            return await show(cb, "Заказов пока нет.", kb.home_only())
        lines = [
            f"#{o.id} · {o.qty} шт · {kb.PROTO_LABEL.get(o.proto, o.proto)} · "
            f"{STATUS_LABEL.get(o.status, o.status)} · {o.created_at:%d.%m %H:%M} UTC"
            for o in items
        ]
        await show(cb, "📜 <b>Последние заказы</b>\n\n" + "\n".join(lines), kb.orders_menu(items))

    # ---------- покупка ----------

    @r.callback_query(F.data == "m:buy")
    async def buy(cb: CallbackQuery):
        await cb.answer()
        await show(
            cb,
            "Выбери тип прокси:\n\n"
            "• <b>SOCKS5</b> — универсальный, лучший выбор по умолчанию\n"
            "• <b>HTTP/HTTPS</b> — для браузеров и парсеров\n"
            "• <b>SOCKS4</b> — старый, без авторизации и UDP",
            kb.proto_menu(),
        )

    @r.callback_query(F.data.startswith("p:"))
    async def pick_proto(cb: CallbackQuery):
        proto = cb.data.split(":")[1]
        if proto not in PROTO_GROUPS:
            return await cb.answer()
        await cb.answer()
        countries = await store.countries(PROTO_GROUPS[proto], svc.cutoff)
        await show(
            cb,
            f"Тип: <b>{kb.PROTO_LABEL[proto]}</b>\nВыбери страну (число — сколько свежих прокси сейчас):",
            kb.country_menu(proto, countries),
        )

    @r.callback_query(F.data.startswith("c:"))
    async def pick_country(cb: CallbackQuery):
        _, proto, cc = cb.data.split(":")
        if proto not in PROTO_GROUPS or not (cc == "xx" or CC_RE.match(cc)):
            return await cb.answer()
        await cb.answer()
        cc_f = "" if cc == "xx" else cc.upper()
        avail = await store.count_available(cb.from_user.id, PROTO_GROUPS[proto], cc_f, svc.cutoff)
        where = kb.flag(cc_f) + " " + cc_f if cc_f else "любая страна"
        await show(
            cb,
            f"Тип: <b>{kb.PROTO_LABEL[proto]}</b> · Страна: <b>{where}</b>\n"
            f"Свежих доступно тебе: {avail}\n\nСколько прокси нужно?",
            kb.qty_menu(proto, cc, cfg.pack_sizes, svc.price_label),
        )

    @r.callback_query(F.data.startswith("o:"))
    async def make_order(cb: CallbackQuery):
        _, proto, cc, qty_s = cb.data.split(":")
        try:
            qty = int(qty_s)
        except ValueError:
            return await cb.answer()
        if proto not in PROTO_GROUPS or qty not in cfg.pack_sizes or not (cc == "xx" or CC_RE.match(cc)):
            return await cb.answer()
        await cb.answer()
        if not svc.pay.enabled:
            return await show(cb, "Оплата временно недоступна. Загляни позже.", kb.home_only())

        uid = cb.from_user.id
        cc_f = "" if cc == "xx" else cc.upper()
        avail = await store.count_available(uid, PROTO_GROUPS[proto], cc_f, svc.cutoff)
        if avail < qty:
            return await show(
                cb,
                f"Сейчас свежих прокси под этот фильтр только {avail}. Выбери меньшее количество, другую страну или тип.",
                kb.home_only(),
            )

        since = utcnow() - dt.timedelta(minutes=55)
        existing = await store.find_pending(uid, proto, cc_f, qty, since)
        if existing:
            return await show(cb, pay_text(existing), kb.pay_menu(existing.pay_url, existing.id))
        if await store.count_pending(uid, since) >= 5:
            return await show(cb, "У тебя слишком много неоплаченных счетов. Оплати один из них или подожди час.", kb.home_only())

        amount = svc.total_for(qty)
        oid = await store.create_order(uid, proto, cc_f, qty, amount)
        try:
            inv = await svc.pay.create_invoice(
                cfg.asset, amount,
                f"ProxyDrop: {qty} proxy ({kb.PROTO_LABEL[proto]}, {cc_f or 'any'})",
                f"order:{oid}",
            )
        except CryptoPayError as e:
            log.error("createInvoice: %s", e)
            await store.set_status(oid, "failed")
            return await show(cb, "Не получилось создать счёт. Попробуй ещё раз через минуту.", kb.home_only())
        url = inv.get("bot_invoice_url") or inv.get("pay_url") or ""
        await store.set_invoice(oid, int(inv["invoice_id"]), url)
        o = await store.get_order(oid)
        await show(cb, pay_text(o), kb.pay_menu(url, oid))

    def pay_text(o) -> str:
        return (
            f"🧾 <b>Заказ #{o.id}</b>\n\n"
            f"{o.qty} шт · {kb.PROTO_LABEL.get(o.proto, o.proto)} · "
            f"{(kb.flag(o.cc) + ' ' + o.cc) if o.cc else 'любая страна'}\n"
            f"К оплате: <b>{o.amount} {cfg.asset}</b>\n\n"
            "Оплати счёт в CryptoBot — прокси придут сюда автоматически через несколько секунд. "
            "Счёт действует 1 час. Прокси проверяются в момент выдачи, не в момент оплаты."
        )

    @r.callback_query(F.data.startswith("k:"))
    async def check_payment(cb: CallbackQuery):
        o = await store.get_order(int(cb.data.split(":")[1]))
        if o is None or o.tg_id != cb.from_user.id:
            return await cb.answer()
        await cb.answer("Проверяю оплату…")
        try:
            status = await svc.sync_order(o.id)
        except Exception:  # noqa: BLE001
            log.exception("check_payment")
            return await cb.message.answer("Не получилось проверить оплату, попробуй ещё раз через минуту.")
        if status == "pending":
            await cb.message.answer("Оплата пока не видна. Если только что заплатил — подожди 10–20 секунд и нажми снова.")
        elif status == "expired":
            await cb.message.answer("Счёт истёк. Создай новый заказ.", reply_markup=kb.home_only())
        elif status in ("paid", "delivering"):
            await cb.message.answer("Оплата получена, готовлю прокси…")

    @r.callback_query(F.data.startswith("v:"))
    async def view_order(cb: CallbackQuery):
        o = await store.get_order(int(cb.data.split(":")[1]))
        if o is None or o.tg_id != cb.from_user.id or o.status != "delivered":
            return await cb.answer()
        await cb.answer()
        rows = await store.order_proxies(o.id)
        can = (
            o.amount != "0" and not o.replaced and o.delivered_at
            and utcnow() - o.delivered_at <= dt.timedelta(minutes=cfg.replace_minutes)
        )
        await svc.send_pack(o.tg_id, o.id, rows, f"📦 <b>Заказ #{o.id}</b> (как были выданы):", replace_button=bool(can))

    @r.callback_query(F.data.startswith("r:"))
    async def replace(cb: CallbackQuery):
        o = await store.get_order(int(cb.data.split(":")[1]))
        if o is None or o.tg_id != cb.from_user.id:
            return await cb.answer()
        if o.status != "delivered" or o.amount == "0":
            return await cb.answer("Для этого заказа замена недоступна", show_alert=True)
        if o.replaced:
            return await cb.answer("Замена по этому заказу уже была", show_alert=True)
        if not o.delivered_at or utcnow() - o.delivered_at > dt.timedelta(minutes=cfg.replace_minutes):
            return await cb.answer(f"Замена доступна только {cfg.replace_minutes} мин после выдачи", show_alert=True)
        await cb.answer("Проверяю выданные прокси…")
        status, dead, new = await svc.replace_dead(o.id)
        if status == "all_ok":
            await cb.message.answer("Все выданные прокси сейчас отвечают ✅ Менять нечего.")
        elif status == "already":
            await cb.message.answer("Замена по этому заказу уже была.")
        elif new < dead:
            await cb.message.answer(
                f"Нерабочих: {dead}, заменил: {new}. Свежих прокси под твой фильтр больше нет — попробуй позже или напиши админу."
            )

    # ---------- админ ----------

    def is_admin(m: Message) -> bool:
        return m.from_user.id in cfg.admin_ids

    @r.message(Command("stats"))
    async def stats(m: Message):
        if not is_admin(m):
            return
        st = await store.stats()
        counts = await store.stock_counts(svc.cutoff)
        total = await store.total_proxies()
        await m.answer(
            "<b>Статистика</b>\n"
            f"Пользователей: {st['users']}\n"
            f"Заказы: {html.escape(str(st['orders']))}\n"
            f"Выручка (по оплаченным): {st['revenue']:g} {cfg.asset}\n\n"
            f"Прокси в базе: {total}\nСвежих: {html.escape(str(counts))}\n"
            f"Последняя загрузка: {svc.last_fetch_info}"
        )

    @r.message(Command("gift"))
    async def gift(m: Message):
        if not is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) != 3 or any(not re.fullmatch(r"[0-9]{1,19}", value) for value in parts[1:]):
            return await m.answer("Формат: <code>/gift ID количество</code>")
        tg_id, qty = int(parts[1]), int(parts[2])
        if tg_id <= 0 or tg_id > 2**63 - 1 or qty <= 0:
            return await m.answer("ID и количество должны быть положительными числами.")

        await m.answer("Проверяю остатки и готовлю подарок…")
        status, order_id = await svc.deliver_gift(tg_id, qty)
        if status == "no_user":
            return await m.answer("Получатель ещё не запускал бота. Попроси его отправить /start, затем повтори команду.")
        if status == "no_stock":
            return await m.answer("Недостаточно свежих прокси для подарка. Попробуй позже или уменьши количество.")
        if status == "send_failed":
            return await m.answer(
                f"Прокси сохранены в заказе #{order_id}, но бот не смог написать получателю. "
                "Когда он откроет бота, подарок будет доступен в «Мои заказы»."
            )
        await m.answer(f"🎁 Выдал {qty} прокси пользователю <code>{tg_id}</code> (заказ #{order_id}).")

    @r.message(Command("refresh"))
    async def refresh(m: Message):
        if not is_admin(m):
            return
        await m.answer("Запускаю загрузку и проверку…")
        await m.answer(await svc.run_cycle(force_fetch=True))

    return r
