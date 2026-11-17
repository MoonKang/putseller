"""Evaluate one put against the rules.

Each rule returns pass, fail or na. 'na' means the snapshot lacked the data;
it does not block the trade but is recorded so gaps stay visible.

Rules split into two groups:
  selection rules (delta, dte) pick which contract on a chain is a candidate;
  filter rules (everything else) decide whether that candidate may be sold.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import bs
from .config import Rules
from .models import PutQuote, Snapshot, TickerSnap

SELECTION_RULES = ("delta", "dte")
FILTER_RULES = (
    "watchlist", "cash_secured", "ticker_concentration", "sector_concentration",
    "total_collateral", "earnings", "spread", "open_interest",
    "annualized_return", "iv_rank", "trend", "stress",
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


def _check(rule: str, ok: bool | None, detail: str) -> Check:
    return Check(rule, "na" if ok is None else "pass" if ok else "fail", detail)


def evaluate(
    snap: Snapshot,
    t: TickerSnap,
    put: PutQuote,
    rules: Rules,
    iv_rank: float | None = None,
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
            f"{sector} {pct:.1f}% of portfolio if assigned, max {sizing['max_pct_per_sector']}%",
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
    pct = 100 * loss / snap.portfolio_value
    return _check(
        "stress", pct <= risk["max_stress_loss_pct"],
        f"{risk['stress_drop_pct']}% drop loses {pct:.1f}% of portfolio, "
        f"max {risk['max_stress_loss_pct']}%",
    )
