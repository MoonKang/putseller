"""Evaluate one put against the rules.

Each rule returns pass, fail or na. 'na' means the snapshot lacked the data;
it does not block the trade but is recorded so gaps stay visible.

Rules split into two groups:
  selection rules (delta, dte) pick which contract on a chain is a candidate;
  filter rules (everything else) decide whether that candidate may be sold.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from . import bs
from .config import Rules
from .models import PutQuote, Snapshot, TickerSnap

SELECTION_RULES = ("delta", "dte")
FILTER_RULES = (
    "watchlist", "cash_secured", "ticker_concentration", "sector_concentration",
    "total_collateral", "earnings", "spread", "open_interest",
    "annualized_return", "iv_rank", "trend", "stress",
    "iv_vs_realized", "vix_level", "vix_rank",
)


@dataclass(frozen=True)
class Check:
    rule: str
    status: str  # pass | fail | na
    detail: str


@dataclass(frozen=True)
class Evaluation:
    ticker: str
    put: PutQuote
    underlying: float
    dte: int
    delta: float | None          # absolute
    iv: float | None
    annualized_return_pct: float | None
    checks: tuple[Check, ...]

    @property
    def failed(self) -> list[str]:
        return [c.rule for c in self.checks if c.status == "fail"]

    @property
    def selected(self) -> bool:
        """True if the contract sits inside the delta and expiry window."""
        return all(c.status == "pass" for c in self.checks if c.rule in SELECTION_RULES)

    @property
    def verdict(self) -> str:
        n = len(self.failed)
        return "pass" if n == 0 else "near_miss" if n == 1 else "fail"

    @property
    def score(self) -> float:
        return self.annualized_return_pct or 0.0


def greeks(put: PutQuote, price: float, dte: int, r: float) -> tuple[float | None, float | None]:
    """(absolute delta, iv), preferring values supplied in the snapshot."""
    iv = put.iv
    t = dte / 365
    if iv is None and dte > 0:
        iv = bs.implied_vol(put.mid, price, put.strike, t, r)
    if put.delta is not None:
        return abs(put.delta), iv
    if iv is None or dte <= 0:
        return None, iv
    return abs(bs.put_delta(price, put.strike, t, r, iv)), iv


def atm_iv(t: TickerSnap, today: date, r: float) -> float | None:
    """At-the-money IV in the expiry closest to 30 days out.

    Interpolates by strike between the nearest put at or below the price and
    the nearest put above it. Falls back to the single nearest put when one
    side is missing.
    """
    expiries = {p.expiry for p in t.puts if (p.expiry - today).days >= 7}
    if not expiries:
        return None
    expiry = min(expiries, key=lambda e: abs((e - today).days - 30))
    dte = (expiry - today).days
    ivs = []
    for p in t.puts:
        if p.expiry == expiry and p.bid > 0:
            _, iv = greeks(p, t.price, dte, r)
            if iv is not None:
                ivs.append((p.strike, iv))
    if not ivs:
        return None
    below = [s for s in ivs if s[0] <= t.price]
    above = [s for s in ivs if s[0] > t.price]
    if below and above:
        (k1, v1), (k2, v2) = max(below), min(above)
        return v1 + (v2 - v1) * (t.price - k1) / (k2 - k1)
    return min(ivs, key=lambda s: abs(s[0] - t.price))[1]


def realized_vol(closes: tuple[float, ...], days: int = 20) -> float | None:
    """Annualized standard deviation of the last `days` daily log returns."""
    closes = closes[-(days + 1):]
    if len(closes) < days + 1 or min(closes) <= 0:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var * 252)


def vix_rank(vix: float | None, history: tuple[float, ...]) -> float | None:
    """Where today's VIX sits in its range over the supplied history, 0-100."""
    if vix is None or len(history) < 20:
        return None
    lo, hi = min(history + (vix,)), max(history + (vix,))
    return None if hi <= lo else 100 * (vix - lo) / (hi - lo)


def _timing(snap: Snapshot, t: TickerSnap, atm: float | None, rules: Rules) -> list[Check]:
    timing = rules.raw.get("timing", {})
    checks = []

    if "min_iv_rv_ratio" not in timing:
        checks.append(_check("iv_vs_realized", True, "rule disabled"))
    else:
        rv = realized_vol(t.closes)
        if atm is None or rv is None or rv <= 0:
            checks.append(_check("iv_vs_realized", None, "IV or 20-day price history unknown"))
        else:
            ratio = atm / rv
            checks.append(_check(
                "iv_vs_realized", ratio >= timing["min_iv_rv_ratio"],
                f"IV {atm:.0%} / realized {rv:.0%} = {ratio:.2f}, min {timing['min_iv_rv_ratio']}",
            ))

    if "max_vix" not in timing:
        checks.append(_check("vix_level", True, "rule disabled"))
    elif snap.vix is None:
        checks.append(_check("vix_level", None, "VIX unknown"))
    else:
        checks.append(_check(
            "vix_level", snap.vix <= timing["max_vix"],
            f"VIX {snap.vix:.2f}, max {timing['max_vix']}",
        ))

    if "min_vix_rank" not in timing:
        checks.append(_check("vix_rank", True, "rule disabled"))
    else:
        rank = vix_rank(snap.vix, snap.vix_closes)
        checks.append(_check(
            "vix_rank", None if rank is None else rank >= timing["min_vix_rank"],
            "VIX history unknown" if rank is None
            else f"VIX rank {rank:.0f} over the past year, min {timing['min_vix_rank']}",
        ))
    return checks


def _check(rule: str, ok: bool | None, detail: str) -> Check:
    return Check(rule, "na" if ok is None else "pass" if ok else "fail", detail)


def evaluate(
    snap: Snapshot,
    t: TickerSnap,
    put: PutQuote,
    rules: Rules,
    iv_rank: float | None = None,
    atm: float | None = None,
) -> Evaluation:
    entry, sizing, liq, risk = rules["entry"], rules["sizing"], rules["liquidity"], rules["risk"]
    dte = (put.expiry - snap.date).days
    delta, iv = greeks(put, t.price, dte, rules.risk_free_rate)
    collateral = put.strike * 100
    pv = snap.portfolio_value
    checks: list[Check] = []

    # --- selection ---
    checks.append(_check(
        "delta",
        None if delta is None else entry["delta_min"] <= delta <= entry["delta_max"],
        "no delta or IV available" if delta is None
        else f"delta {delta:.2f}, allowed {entry['delta_min']}-{entry['delta_max']}",
    ))
    checks.append(_check(
        "dte", entry["dte_min"] <= dte <= entry["dte_max"],
        f"{dte} days to expiry, allowed {entry['dte_min']}-{entry['dte_max']}",
    ))

    # --- filters ---
    checks.append(_check(
        "watchlist", t.ticker in rules.universe,
        "on watchlist" if t.ticker in rules.universe else "not on watchlist",
    ))
    checks.append(_check(
        "cash_secured", collateral <= snap.cash,
        f"needs ${collateral:,.0f}, cash ${snap.cash:,.0f}",
    ))

    if pv <= 0:
        for rule in ("ticker_concentration", "sector_concentration", "total_collateral"):
            checks.append(_check(rule, False, f"portfolio value is ${pv:,.0f}"))
    else:
        ticker_exposure = collateral
        ticker_exposure += sum(h.market_value for h in snap.holdings if h.ticker == t.ticker)
        ticker_exposure += sum(p.collateral for p in snap.open_puts if p.ticker == t.ticker)
        pct = 100 * ticker_exposure / pv
        checks.append(_check(
            "ticker_concentration", pct <= sizing["max_pct_per_ticker"],
            f"{pct:.1f}% of portfolio if assigned, max {sizing['max_pct_per_ticker']}%",
        ))

        sector = t.sector
        if sector is None:
            checks.append(_check("sector_concentration", None, "sector unknown"))
        else:
            sector_exposure = collateral
            sector_exposure += sum(
                h.market_value for h in snap.holdings if snap.sector_of(h.ticker) == sector)
            sector_exposure += sum(
                p.collateral for p in snap.open_puts if snap.sector_of(p.ticker) == sector)
            pct = 100 * sector_exposure / pv
            checks.append(_check(
                "sector_concentration", pct <= sizing["max_pct_per_sector"],
                f"{sector} {pct:.1f}% of portfolio if assigned, "
                f"max {sizing['max_pct_per_sector']}%",
            ))

        total = collateral + sum(p.collateral for p in snap.open_puts)
        pct = 100 * total / pv
        checks.append(_check(
            "total_collateral", pct <= sizing["max_pct_collateral"],
            f"put collateral {pct:.1f}% of portfolio, max {sizing['max_pct_collateral']}%",
        ))

    if not entry["exclude_earnings_in_window"]:
        checks.append(_check("earnings", True, "rule disabled"))
    elif t.next_earnings is None:
        checks.append(_check("earnings", None, "earnings date unknown"))
    else:
        inside = snap.date <= t.next_earnings <= put.expiry
        checks.append(_check(
            "earnings", not inside,
            f"earnings {t.next_earnings}, expiry {put.expiry}",
        ))

    mid = put.mid
    if put.bid <= 0 or mid <= 0:
        checks.append(_check("spread", False, "no bid"))
    else:
        spread = 100 * (put.ask - put.bid) / mid
        checks.append(_check(
            "spread", spread <= liq["max_spread_pct_of_mid"],
            f"spread {spread:.1f}% of mid, max {liq['max_spread_pct_of_mid']}%",
        ))

    checks.append(_check(
        "open_interest",
        None if put.open_interest is None else put.open_interest >= liq["min_open_interest"],
        "open interest unknown" if put.open_interest is None
        else f"open interest {put.open_interest}, min {liq['min_open_interest']}",
    ))

    # Return uses the bid: what a seller could actually collect right now.
    annualized = None
    if dte > 0 and put.strike > 0:
        annualized = 100 * (put.bid / put.strike) * (365 / dte)
    checks.append(_check(
        "annualized_return",
        None if annualized is None else annualized >= entry["min_annualized_return_pct"],
        "not computable" if annualized is None
        else f"{annualized:.1f}% annualized, min {entry['min_annualized_return_pct']}%",
    ))

    rank = t.iv_rank if t.iv_rank is not None else iv_rank
    checks.append(_check(
        "iv_rank",
        None if rank is None else rank >= entry["min_iv_rank"],
        "not enough IV history yet" if rank is None
        else f"IV rank {rank:.0f}, min {entry['min_iv_rank']}",
    ))

    if not entry["require_above_200dma"]:
        checks.append(_check("trend", True, "rule disabled"))
    elif t.sma200 is None:
        checks.append(_check("trend", None, "200-day average unknown"))
    else:
        checks.append(_check(
            "trend", t.price >= t.sma200,
            f"price {t.price:.2f}, 200-day average {t.sma200:.2f}",
        ))

    checks.append(_stress(snap, t, put, risk))
    if atm is None:
        atm = atm_iv(t, snap.date, rules.risk_free_rate)
    checks.extend(_timing(snap, t, atm, rules))

    return Evaluation(
        ticker=t.ticker, put=put, underlying=t.price, dte=dte, delta=delta, iv=iv,
        annualized_return_pct=annualized, checks=tuple(checks),
    )


def _stress(snap: Snapshot, t: TickerSnap, put: PutQuote, risk: dict) -> Check:
    """Loss at expiry if every underlying drops by stress_drop_pct (premiums ignored)."""
    keep = 1 - risk["stress_drop_pct"] / 100
    loss = max(put.strike - t.price * keep, 0) * 100
    for p in snap.open_puts:
        price = p.underlying_price or snap.price_of(p.ticker)
        if price is None:
            return _check("stress", None, f"no price for open put on {p.ticker}")
        loss += max(p.strike - price * keep, 0) * 100 * p.contracts
    if snap.portfolio_value <= 0:
        return _check("stress", False, f"portfolio value is ${snap.portfolio_value:,.0f}")
    pct = 100 * loss / snap.portfolio_value
    return _check(
        "stress", pct <= risk["max_stress_loss_pct"],
        f"{risk['stress_drop_pct']}% drop loses {pct:.1f}% of portfolio, "
        f"max {risk['max_stress_loss_pct']}%",
    )
