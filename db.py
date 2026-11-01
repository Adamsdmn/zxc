from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    and_,
    case,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Группы протоколов в меню покупки
PROTO_GROUPS: dict[str, tuple[str, ...]] = {
    "http": ("http", "https"),
    "socks5": ("socks5",),
    "socks4": ("socks4",),
    "any": ("http", "https", "socks4", "socks5"),
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Proxy(Base):
    __tablename__ = "proxies"
    __table_args__ = (UniqueConstraint("ip", "port", "protocol", name="uq_proxy"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(String(8), index=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    anonymity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fails: Mapped[int] = mapped_column(Integer, default=0)
    sold_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime)
    last_checked: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_ok: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    sample_used: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    proto: Mapped[str] = mapped_column(String(8))
    cc: Mapped[str] = mapped_column(String(2), default="")
    qty: Mapped[int] = mapped_column(Integer)
    amount: Mapped[str] = mapped_column(String(16))
    # pending -> paid -> delivering -> delivered | expired | failed
    status: Mapped[str] = mapped_column(String(12), index=True)
    invoice_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pay_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    replaced: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class OrderProxy(Base):
    __tablename__ = "order_proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    proxy_id: Mapped[int] = mapped_column(ForeignKey("proxies.id"), index=True)
    replacement: Mapped[bool] = mapped_column(Boolean, default=False)


@dataclass(frozen=True)
class PRow:
    id: int
    ip: str
    port: int
    protocol: str
    country: str | None
    latency_ms: int | None

    @property
    def url(self) -> str:
        scheme = "http" if self.protocol in ("http", "https") else self.protocol
        return f"{scheme}://{self.ip}:{self.port}"


def _prow(p: Proxy) -> PRow:
    return PRow(p.id, p.ip, p.port, p.protocol, p.country, p.latency_ms)


class Store:
    def __init__(self, url: str):
        if url.startswith("sqlite"):
            path = url.split("///", 1)[1]
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sm = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # ---------- прокси ----------

    async def upsert_proxies(self, recs) -> int:
        if not recs:
            return 0
        async with self.sm() as s:
            rows = (await s.execute(select(Proxy.ip, Proxy.port, Proxy.protocol))).all()
            known = {(r[0], r[1], r[2]) for r in rows}
            now = utcnow()
            new: list[Proxy] = []
            for r in recs:
                key = (r.ip, r.port, r.protocol)
                if key in known:
                    continue
                known.add(key)
                new.append(
                    Proxy(
                        ip=r.ip, port=r.port, protocol=r.protocol, country=r.country,
                        anonymity=r.anonymity, latency_ms=r.latency_ms, first_seen=now,
                    )
                )
            s.add_all(new)
            await s.commit()
            return len(new)

    async def candidates_for_check(self, limit: int, recheck_alive_min: int, retry_dead_min: int):
        now = utcnow()
        stale_alive = and_(
            Proxy.alive.is_(True),
            Proxy.last_checked < now - dt.timedelta(minutes=recheck_alive_min),
        )
        fresh_new = Proxy.last_checked.is_(None)
        retry = and_(
            Proxy.alive.is_(False),
            Proxy.fails < 5,
            Proxy.last_checked < now - dt.timedelta(minutes=retry_dead_min),
        )
        stmt = (
            select(Proxy.id, Proxy.ip, Proxy.port, Proxy.protocol)
            .where(or_(stale_alive, fresh_new, retry))
            .order_by(case((stale_alive, 0), (fresh_new, 1), else_=2), Proxy.id)
            .limit(limit)
        )
        async with self.sm() as s:
            return [tuple(r) for r in (await s.execute(stmt)).all()]

    async def apply_checks(self, results) -> None:
        if not results:
            return
        now = utcnow()
        async with self.sm() as s:
            for pid, ok, lat in results:
                if ok:
                    vals = dict(alive=True, last_checked=now, last_ok=now, fails=0, latency_ms=lat)
                else:
                    vals = dict(alive=False, last_checked=now, fails=Proxy.fails + 1)
                await s.execute(update(Proxy).where(Proxy.id == pid).values(**vals))
            await s.commit()

    async def cleanup(self) -> int:
        sold = select(OrderProxy.proxy_id)
        async with self.sm() as s:
            res = await s.execute(
                delete(Proxy).where(Proxy.alive.is_(False), Proxy.fails >= 8, Proxy.id.not_in(sold))
            )
            await s.commit()
            return res.rowcount or 0

    async def stock_counts(self, cutoff: dt.datetime) -> dict[str, int]:
        stmt = (
            select(Proxy.protocol, func.count())
            .where(Proxy.alive.is_(True), Proxy.last_ok >= cutoff)
            .group_by(Proxy.protocol)
        )
        async with self.sm() as s:
            return {p: n for p, n in (await s.execute(stmt)).all()}

    async def total_proxies(self) -> int:
        async with self.sm() as s:
            return (await s.execute(select(func.count()).select_from(Proxy))).scalar_one()

    async def countries(self, protos, cutoff, limit: int = 8) -> list[tuple[str, int]]:
        stmt = (
            select(Proxy.country, func.count().label("n"))
            .where(
                Proxy.alive.is_(True), Proxy.last_ok >= cutoff,
                Proxy.protocol.in_(protos), Proxy.country.is_not(None),
            )
            .group_by(Proxy.country)
            .order_by(func.count().desc())
            .limit(limit)
        )
        async with self.sm() as s:
            return [(c, n) for c, n in (await s.execute(stmt)).all()]

    def _avail_conds(self, tg_id, protos, cc, cutoff):
        sold = (
            select(OrderProxy.proxy_id)
            .join(Order, Order.id == OrderProxy.order_id)
            .where(Order.tg_id == tg_id)
        )
        conds = [
            Proxy.alive.is_(True), Proxy.last_ok >= cutoff,
            Proxy.protocol.in_(protos), Proxy.id.not_in(sold),
        ]
        if cc:
            conds.append(Proxy.country == cc)
        return conds

    async def count_available(self, tg_id, protos, cc, cutoff) -> int:
        stmt = select(func.count()).select_from(Proxy).where(*self._avail_conds(tg_id, protos, cc, cutoff))
        async with self.sm() as s:
            return (await s.execute(stmt)).scalar_one()

    async def pick_candidates(self, tg_id, protos, cc, cutoff, limit, exclude=()) -> list[PRow]:
        conds = self._avail_conds(tg_id, protos, cc, cutoff)
        if exclude:
            conds.append(Proxy.id.not_in(list(exclude)))
        stmt = (
            select(Proxy)
            .where(*conds)
            .order_by(
                case((Proxy.anonymity == "transparent", 1), else_=0),
                Proxy.sold_count.asc(),
                Proxy.latency_ms.asc(),
            )
            .limit(limit)
        )
        async with self.sm() as s:
            return [_prow(p) for p in (await s.execute(stmt)).scalars().all()]

    # ---------- пользователи ----------

    async def ensure_user(self, tg_id: int, username: str | None) -> None:
        async with self.sm() as s:
            u = await s.get(User, tg_id)
            if u is None:
                s.add(User(tg_id=tg_id, username=username, created_at=utcnow(), sample_used=False))
            else:
                u.username = username
            await s.commit()

    async def has_user(self, tg_id: int) -> bool:
        async with self.sm() as s:
            return await s.get(User, tg_id) is not None

    async def take_sample(self, tg_id: int) -> bool:
        """Атомарно помечает, что пример взят. True — можно выдавать."""
        async with self.sm() as s:
            res = await s.execute(
                update(User).where(User.tg_id == tg_id, User.sample_used.is_(False)).values(sample_used=True)
            )
            await s.commit()
            return (res.rowcount or 0) == 1

    async def give_back_sample(self, tg_id: int) -> None:
        async with self.sm() as s:
            await s.execute(update(User).where(User.tg_id == tg_id).values(sample_used=False))
            await s.commit()

    async def sample_used(self, tg_id: int) -> bool:
        async with self.sm() as s:
            u = await s.get(User, tg_id)
            return bool(u and u.sample_used)

    # ---------- заказы ----------

    async def create_order(self, tg_id, proto, cc, qty, amount, status="pending") -> int:
        async with self.sm() as s:
            o = Order(
                tg_id=tg_id, proto=proto, cc=cc, qty=qty, amount=amount,
                status=status, created_at=utcnow(),
            )
            s.add(o)
            await s.commit()
            return o.id

    async def get_order(self, order_id: int) -> Order | None:
        async with self.sm() as s:
            return await s.get(Order, order_id)

    async def set_invoice(self, order_id: int, invoice_id: int, pay_url: str) -> None:
        async with self.sm() as s:
            await s.execute(
                update(Order).where(Order.id == order_id).values(invoice_id=invoice_id, pay_url=pay_url)
            )
            await s.commit()

    async def set_status(self, order_id: int, status: str) -> None:
        async with self.sm() as s:
            await s.execute(update(Order).where(Order.id == order_id).values(status=status))
            await s.commit()

    async def _transition(self, order_id: int, src: str, dst: str, **extra) -> bool:
        async with self.sm() as s:
            res = await s.execute(
                update(Order).where(Order.id == order_id, Order.status == src).values(status=dst, **extra)
            )
            await s.commit()
            return (res.rowcount or 0) == 1

    async def mark_paid(self, order_id: int) -> bool:
        return await self._transition(order_id, "pending", "paid", paid_at=utcnow())

    async def claim_for_delivery(self, order_id: int) -> bool:
        return await self._transition(order_id, "paid", "delivering")

    async def release(self, order_id: int) -> None:
        await self._transition(order_id, "delivering", "paid")

    async def finish_delivery(self, order_id: int) -> None:
        await self._transition(order_id, "delivering", "delivered", delivered_at=utcnow())

    async def mark_notified(self, order_id: int) -> bool:
        async with self.sm() as s:
            res = await s.execute(
                update(Order).where(Order.id == order_id, Order.notified.is_(False)).values(notified=True)
            )
            await s.commit()
            return (res.rowcount or 0) == 1

    async def claim_replace(self, order_id: int) -> bool:
        async with self.sm() as s:
            res = await s.execute(
                update(Order)
                .where(Order.id == order_id, Order.replaced.is_(False), Order.status == "delivered")
                .values(replaced=True)
            )
            await s.commit()
            return (res.rowcount or 0) == 1

    async def find_pending(self, tg_id, proto, cc, qty, since) -> Order | None:
        stmt = (
            select(Order)
            .where(
                Order.tg_id == tg_id, Order.proto == proto, Order.cc == cc, Order.qty == qty,
                Order.status == "pending", Order.created_at >= since, Order.pay_url.is_not(None),
            )
            .order_by(Order.id.desc())
            .limit(1)
        )
        async with self.sm() as s:
            return (await s.execute(stmt)).scalars().first()

    async def count_pending(self, tg_id: int, since) -> int:
        stmt = select(func.count()).select_from(Order).where(
            Order.tg_id == tg_id, Order.status == "pending", Order.created_at >= since
        )
        async with self.sm() as s:
            return (await s.execute(stmt)).scalar_one()

    async def pending_orders(self, since) -> list[Order]:
        stmt = select(Order).where(
            Order.status == "pending", Order.created_at >= since, Order.invoice_id.is_not(None)
        )
        async with self.sm() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def paid_waiting(self) -> list[Order]:
        async with self.sm() as s:
            return list((await s.execute(select(Order).where(Order.status == "paid"))).scalars().all())

    async def user_orders(self, tg_id: int, limit: int = 8) -> list[Order]:
        stmt = (
            select(Order)
            .where(Order.tg_id == tg_id, Order.status != "failed")
            .order_by(Order.id.desc())
            .limit(limit)
        )
        async with self.sm() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def record_sale(self, order_id: int, proxy_ids: list[int], replacement: bool = False) -> None:
        if not proxy_ids:
            return
        async with self.sm() as s:
            s.add_all([OrderProxy(order_id=order_id, proxy_id=i, replacement=replacement) for i in proxy_ids])
            await s.execute(
                update(Proxy).where(Proxy.id.in_(proxy_ids)).values(sold_count=Proxy.sold_count + 1)
            )
            await s.commit()

    async def order_proxies(self, order_id: int) -> list[PRow]:
        stmt = (
            select(Proxy)
            .join(OrderProxy, OrderProxy.proxy_id == Proxy.id)
            .where(OrderProxy.order_id == order_id)
            .order_by(OrderProxy.id)
        )
        async with self.sm() as s:
            return [_prow(p) for p in (await s.execute(stmt)).scalars().all()]

    async def stats(self) -> dict:
        async with self.sm() as s:
            users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
            by_status = dict((await s.execute(select(Order.status, func.count()).group_by(Order.status))).all())
            paid = (
                await s.execute(
                    select(Order.amount).where(Order.status.in_(("paid", "delivering", "delivered")), Order.amount != "0")
                )
            ).scalars().all()
        revenue = sum(float(a) for a in paid)
        return {"users": users, "orders": by_status, "revenue": revenue}
