"""Проверка источника без запуска бота:  python selftest.py

Показывает, сколько прокси отдал источник, как распарсился формат и сколько
из первых 40 реально работают. Запусти один раз перед деплоем.
"""
import asyncio
from collections import Counter

import aiohttp

from checker import check_many, get_real_ip
from sources import fetch_all


async def main() -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        recs = await fetch_all(http, 1000)
        print(f"Получено записей: {len(recs)}")
        print("По протоколам:", dict(Counter(r.protocol for r in recs)))
        print("Со страной:", sum(1 for r in recs if r.country), "из", len(recs))
        if not recs:
            print("Источник ничего не вернул — проверь доступ к hproxy.com")
            return
        sample = recs[:40]
        real = await get_real_ip(http)
        print("Мой IP:", real)
        res = await check_many(
            [(i, r.ip, r.port, r.protocol) for i, r in enumerate(sample)],
            concurrency=40, timeout=8, real_ip=real,
        )
        ok = [(sample[i], lat) for i, alive, lat in res if alive]
        print(f"Рабочих из {len(sample)}: {len(ok)}")
        for r, lat in ok[:10]:
            print(f"  {r.protocol}://{r.ip}:{r.port}  {lat} мс  {r.country or '-'}")


if __name__ == "__main__":
    asyncio.run(main())
