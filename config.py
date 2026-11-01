from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name("config.env"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _db_url(raw: str) -> str:
    if not raw:
        return "sqlite+aiosqlite:///./data/proxydrop.db"
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://"):]
    return raw


@dataclass(frozen=True)
class Settings:
    bot_token: str
    cryptopay_token: str
    cryptopay_testnet: bool
    admin_ids: tuple[int, ...]
    database_url: str
    webhook_base_url: str
    webhook_secret: str
    port: int
    # продажа
    price: str                      # цена ЗА ОДИН прокси, в asset
    asset: str
    pack_sizes: tuple[int, ...]     # какие количества можно купить
    sample_size: int                # бесплатный пример (0 = выключено)
    replace_minutes: int            # сколько минут можно заменить нерабочие
    # парсер / чекер
    fetch_limit: int
    fetch_interval: int             # сек между загрузками списка
    cycle_seconds: int              # сек между циклами проверки
    check_batch: int
    check_concurrency: int
    check_timeout: int
    fresh_minutes: int              # "свежий" = проверен не позже N минут назад
    extra_sources: tuple[str, ...]  # "protocol|https://url.txt"


def load_settings() -> Settings:
    token = _env("BOT_TOKEN")
    if not token:
        raise SystemExit("Переменная BOT_TOKEN не задана")

    base = _env("WEBHOOK_BASE_URL") or _env("RENDER_EXTERNAL_URL")
    secret = _env("WEBHOOK_SECRET") or hashlib.sha256(token.encode()).hexdigest()[:32]

    admins = tuple(int(x) for x in _env("ADMIN_IDS").replace(" ", "").split(",") if x)
    sizes = tuple(int(x) for x in (_env("PACK_SIZES") or "1,5,10").replace(" ", "").split(",") if x)
    extra = tuple(x.strip() for x in _env("EXTRA_SOURCES").split(",") if x.strip())

    return Settings(
        bot_token=token,
        cryptopay_token=_env("CRYPTOPAY_TOKEN"),
        cryptopay_testnet=_env("CRYPTOPAY_TESTNET").lower() in {"1", "true", "yes"},
        admin_ids=admins,
        database_url=_db_url(_env("DATABASE_URL")),
        webhook_base_url=base.rstrip("/"),
        webhook_secret=secret,
        port=_int("PORT", 10000),
        price=_env("PRICE_PER_PROXY") or "1",
        asset=_env("ASSET") or "USDT",
        pack_sizes=sizes,
        sample_size=_int("SAMPLE_SIZE", 1),
        replace_minutes=_int("REPLACE_MINUTES", 60),
        fetch_limit=_int("FETCH_LIMIT", 3000),
        fetch_interval=_int("FETCH_INTERVAL", 600),
        cycle_seconds=_int("CYCLE_SECONDS", 60),
        check_batch=_int("CHECK_BATCH", 1500),
        check_concurrency=_int("CHECK_CONCURRENCY", 150),
        check_timeout=_int("CHECK_TIMEOUT", 8),
        fresh_minutes=_int("FRESH_MINUTES", 20),
        extra_sources=extra,
    )
