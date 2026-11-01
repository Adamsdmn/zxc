from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PROTO_LABEL = {"http": "HTTP/HTTPS", "socks5": "SOCKS5", "socks4": "SOCKS4", "any": "Любой"}


def flag(cc: str | None) -> str:
    if not cc or len(cc) != 2 or not cc.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper())


def main_menu(sample_size: int, sample_used: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Купить прокси", callback_data="m:buy")
    b.button(text="📊 В наличии", callback_data="m:stock")
    if sample_size and not sample_used:
        b.button(text=f"🎁 Бесплатный пример ({sample_size} шт)", callback_data="m:sample")
    b.button(text="📜 Мои заказы", callback_data="m:orders")
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def subscription_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Подписаться на канал", url="https://t.me/EIiteProxy")
    b.button(text="Проверить подписку", callback_data="sub:check")
    b.adjust(1)
    return b.as_markup()


def proto_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in ("socks5", "http", "socks4", "any"):
        b.button(text=PROTO_LABEL[key], callback_data=f"p:{key}")
    b.button(text="← Назад", callback_data="m:home")
    b.adjust(2, 2, 1)
    return b.as_markup()


def country_menu(proto: str, countries: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cc, n in countries:
        b.button(text=f"{flag(cc)} {cc} · {n}", callback_data=f"c:{proto}:{cc.lower()}")
    b.button(text="🌍 Любая страна", callback_data=f"c:{proto}:xx")
    b.button(text="← Назад", callback_data="m:buy")
    n = len(countries)
    b.adjust(*([2] * (n // 2)), *([1] if n % 2 else []), 1, 1)
    return b.as_markup()


def qty_menu(proto: str, cc: str, sizes: tuple[int, ...], price_of) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for q in sizes:
        b.button(text=f"{q} шт — {price_of(q)}", callback_data=f"o:{proto}:{cc}:{q}")
    b.button(text="← Назад", callback_data=f"p:{proto}")
    b.adjust(*([1] * len(sizes)), 1)
    return b.as_markup()


def pay_menu(url: str, order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплатить в CryptoBot", url=url)
    b.button(text="✅ Я оплатил — проверить", callback_data=f"k:{order_id}")
    b.button(text="🏠 Меню", callback_data="m:home")
    b.adjust(1, 1, 1)
    return b.as_markup()


def after_pack(order_id: int, replace_button: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if replace_button:
        b.button(text="🔄 Заменить нерабочие", callback_data=f"r:{order_id}")
    b.button(text="🛒 Купить ещё", callback_data="m:buy")
    b.button(text="🏠 Меню", callback_data="m:home")
    b.adjust(1, 2) if replace_button else b.adjust(2)
    return b.as_markup()


def orders_menu(orders) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for o in orders:
        if o.status == "delivered":
            b.button(text=f"📦 Показать заказ #{o.id}", callback_data=f"v:{o.id}")
        elif o.status == "pending" and o.pay_url:
            b.button(text=f"💳 Оплатить заказ #{o.id}", callback_data=f"k:{o.id}")
    b.button(text="🏠 Меню", callback_data="m:home")
    b.adjust(1)
    return b.as_markup()


def home_only() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Меню", callback_data="m:home")
    return b.as_markup()
