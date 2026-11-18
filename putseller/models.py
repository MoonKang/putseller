"""Snapshot schema: one JSON file per scan, written by whatever fetches the data.

See README.md for the field-by-field description.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class PutQuote:
    expiry: date
    strike: float
    bid: float
    ask: float
    open_interest: int | None = None
    volume: int | None = None
    iv: float | None = None      # annualized, as a fraction (0.25 = 25%)
    delta: float | None = None   # signed or absolute; sign is ignored

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class TickerSnap:
    ticker: str
    price: float
    sector: str | None = None
    sma200: float | None = None
    iv_rank: float | None = None
    next_earnings: date | None = None
    puts: tuple[PutQuote, ...] = ()
    closes: tuple[float, ...] = ()   # recent daily closes, oldest first, for realized vol

    def find_put(self, expiry: date, strike: float) -> PutQuote | None:
        for p in self.puts:
            if p.expiry == expiry and abs(p.strike - strike) < 1e-6:
                return p
        return None


@dataclass(frozen=True)
class Holding:
    ticker: str
    market_value: float
    sector: str | None = None


@dataclass(frozen=True)
class OpenPut:
    """A real short put already in the account."""
    ticker: str
    strike: float
    expiry: date
    contracts: int = 1
    sector: str | None = None
    underlying_price: float | None = None

    @property
    def collateral(self) -> float:
        return self.strike * 100 * self.contracts


@dataclass(frozen=True)
class Snapshot:
    taken_at: datetime
    portfolio_value: float
    cash: float
    holdings: tuple[Holding, ...] = ()
    open_puts: tuple[OpenPut, ...] = ()
    tickers: dict[str, TickerSnap] = field(default_factory=dict)
    vix: float | None = None
    vix_closes: tuple[float, ...] = ()   # about a year of daily VIX closes, for VIX rank

    @property
    def date(self) -> date:
        return self.taken_at.date()

    def sector_of(self, ticker: str) -> str | None:
        t = self.tickers.get(ticker)
        if t and t.sector:
            return t.sector
        for h in self.holdings:
            if h.ticker == ticker and h.sector:
                return h.sector
        for p in self.open_puts:
            if p.ticker == ticker and p.sector:
                return p.sector
        return None

    def price_of(self, ticker: str) -> float | None:
        t = self.tickers.get(ticker)
        return t.price if t else None


def _d(value) -> date | None:
    return date.fromisoformat(value) if value else None


def _num(obj: dict, key: str, where: str) -> float:
    if obj.get(key) is None:
        raise ValueError(f"snapshot: {where} is missing '{key}'")
    return float(obj[key])


def parse_snapshot(raw: dict) -> Snapshot:
    if "taken_at" not in raw:
        raise ValueError("snapshot: missing 'taken_at'")
    account = raw.get("account") or {}
    tickers: dict[str, TickerSnap] = {}
    for t in raw.get("tickers", []):
        name = str(t["ticker"]).upper()
        puts = tuple(
            PutQuote(
                expiry=date.fromisoformat(p["expiry"]),
                strike=_num(p, "strike", f"{name} put"),
                bid=_num(p, "bid", f"{name} put"),
                ask=_num(p, "ask", f"{name} put"),
                open_interest=p.get("open_interest"),
                volume=p.get("volume"),
                iv=p.get("iv"),
                delta=p.get("delta"),
            )
            for p in t.get("puts", [])
        )
        tickers[name] = TickerSnap(
            ticker=name,
            price=_num(t, "price", name),
            sector=t.get("sector"),
            sma200=t.get("sma200"),
            iv_rank=t.get("iv_rank"),
            next_earnings=_d(t.get("next_earnings")),
            puts=puts,
            closes=tuple(float(c) for c in t.get("closes") or ()),
        )
    market = raw.get("market") or {}
    return Snapshot(
        taken_at=datetime.fromisoformat(raw["taken_at"]),
        portfolio_value=_num(account, "portfolio_value", "account"),
        cash=_num(account, "cash", "account"),
        holdings=tuple(
            Holding(str(h["ticker"]).upper(), float(h["market_value"]), h.get("sector"))
            for h in raw.get("holdings", [])
        ),
        open_puts=tuple(
            OpenPut(
                ticker=str(p["ticker"]).upper(),
                strike=float(p["strike"]),
                expiry=date.fromisoformat(p["expiry"]),
                contracts=int(p.get("contracts", 1)),
                sector=p.get("sector"),
                underlying_price=p.get("underlying_price"),
            )
            for p in raw.get("open_puts", [])
        ),
        tickers=tickers,
        vix=market.get("vix"),
        vix_closes=tuple(float(c) for c in market.get("vix_closes_1y") or ()),
    )


def load_snapshot(path: str | Path) -> Snapshot:
    return parse_snapshot(json.loads(Path(path).read_text()))
