"""Stock-quality screen: pick the stocks the daily scan may sell puts on.

Input is a saved scanner result (preview_scan JSON). Each stock is checked
against screen.toml's criteria; the passing stocks are ranked and capped by
list size and sector to propose a new `universe`.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# metric -> scanner column names it may arrive under (the scanner renames
# columns that match its standard ones)
COLUMNS = {
    "price": ("Last",),
    "market_cap": ("Market cap", "Mkt cap"),
    "beta": ("Beta",),
    "roe": ("Return on equity", "ROE"),
    "net_margin": ("Net profit margin", "Net margin"),
    "lt_debt_to_capital": ("LT debt/cap",),
    "fcf_per_share": ("FCF/share",),
    "option_open_interest": ("Opt OI",),
    "iv": ("Implied volatility", "IV30"),
    "iv_rank": ("IV rank",),
    "realized_vol": ("Historical volatility", "RV 1M"),
    "forward_pe": ("Forward P/E", "Fwd PE"),
    "revenue_growth": ("Rev growth",),
}
UNIVERSE_LINE = re.compile(r"^universe = \[.*\]$", re.M)
SHARE_CLASS = re.compile(r"\s+Class [A-Z]\b.*$")


def company(name: str) -> str:
    """'Alphabet Class A' and 'Alphabet Class C' are the same company."""
    return SHARE_CLASS.sub("", name).strip().lower()


@dataclass
class Stock:
    ticker: str
    name: str
    sector: str | None
    metrics: dict[str, float | None]
    failed: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failed


def _num(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def load_scan(path: str | Path) -> list[Stock]:
    raw = json.loads(Path(path).read_text())
    rows = raw.get("data", raw).get("result", raw)["results"]
    stocks = []
    for r in rows:
        cols = r["columns"]
        metrics = {}
        for metric, names in COLUMNS.items():
            metrics[metric] = next((_num(cols[n]) for n in names if n in cols), None)
        stocks.append(Stock(r["ticker"].upper(), cols.get("Name", ""),
                            cols.get("Sector") or None, metrics))
    return stocks


def check(s: Stock, c: dict) -> list[str]:
    """Names of the criteria the stock fails; missing data counts as a fail."""
    m = s.metrics
    rules = [
        ("market_cap", "min_market_cap", lambda v, t: v >= t),
        ("net_margin", "min_net_margin", lambda v, t: v >= t),
        ("roe", "min_roe", lambda v, t: v >= t),
        ("lt_debt_to_capital", "max_lt_debt_to_capital", lambda v, t: v <= t),
        ("beta", "max_beta", lambda v, t: v <= t),
        ("option_open_interest", "min_option_open_interest", lambda v, t: v >= t),
        ("price", "max_price", lambda v, t: v <= t),
    ]
    failed = []
    for metric, key, ok in rules:
        if key in c and (m[metric] is None or not ok(m[metric], c[key])):
            failed.append(metric)
    if "min_fcf_per_share" in c and s.sector not in c.get("fcf_exempt_sectors", []):
        if m["fcf_per_share"] is None or m["fcf_per_share"] <= c["min_fcf_per_share"]:
            failed.append("fcf_per_share")
    if s.sector is None:
        failed.append("sector")
    return failed


def select(stocks: list[Stock], cfg: dict, current: list[str]) -> list[Stock]:
    """Apply criteria, then fill up to max_stocks, at most max_per_sector each."""
    for s in stocks:
        s.failed = check(s, cfg["criteria"])
    passing = [s for s in stocks if s.passed]
    key = cfg.get("rank_by", "iv")
    passing.sort(key=lambda s: -(s.metrics.get(key) or 0))
    if cfg.get("keep_current", True):
        passing.sort(key=lambda s: s.ticker not in current)   # stable: incumbents first
    chosen, per_sector, companies = [], {}, set()
    for s in passing:
        if len(chosen) >= cfg["max_stocks"]:
            break
        if per_sector.get(s.sector, 0) >= cfg["max_per_sector"] or company(s.name) in companies:
            continue
        chosen.append(s)
        companies.add(company(s.name))
        per_sector[s.sector] = per_sector.get(s.sector, 0) + 1
    return chosen


def current_universe(rules_path: str | Path) -> list[str]:
    return list(tomllib.loads(Path(rules_path).read_text())["universe"])


def apply_universe(paths: list[Path], tickers: list[str]) -> None:
    line = "universe = [" + ", ".join(f'"{t}"' for t in tickers) + "]"
    for p in paths:
        text = p.read_text()
        new, n = UNIVERSE_LINE.subn(line, text, count=1)
        if n != 1:
            raise ValueError(f"{p}: no single-line `universe = [...]` to replace")
        p.write_text(new)


def report(stocks: list[Stock], chosen: list[Stock], current: list[str]) -> str:
    def pct(v):
        return "-" if v is None else f"{v:.0%}"

    picked = [s.ticker for s in chosen]
    out = [f"{sum(s.passed for s in stocks)} of {len(stocks)} stocks pass the criteria",
           "", "Proposed universe:"]
    out.append(f"  {'ticker':<7}{'sector':<24}{'price':>8}{'IV':>6}{'IV rk':>7}"
               f"{'RV':>6}{'ROE':>7}{'margin':>8}  note")
    for s in chosen:
        m = s.metrics
        note = "kept" if s.ticker in current else "new"
        out.append(f"  {s.ticker:<7}{(s.sector or '-')[:23]:<24}{m['price'] or 0:>8.2f}"
                   f"{pct(m['iv']):>6}{pct(m['iv_rank']):>7}{pct(m['realized_vol']):>6}"
                   f"{pct(m['roe']):>7}{pct(m['net_margin']):>8}  {note}")
    dropped = [t for t in current if t not in picked]
    if dropped:
        out.append("\nDropped from the current universe:")
        by_ticker = {s.ticker: s for s in stocks}
        for t in dropped:
            s = by_ticker.get(t)
            why = ("not in the pool" if s is None
                   else "failed " + ", ".join(s.failed) if s.failed
                   else "passed but ranked out (list size or sector cap)")
            out.append(f"  {t}: {why}")
    added = [t for t in picked if t not in current]
    out.append(f"\nChange: +{len(added)} {added}  -{len(dropped)} {dropped}")
    return "\n".join(out)


def run(scan_path: str, cfg_path: str, rules_path: str, apply_to: list[Path] | None) -> str:
    cfg = tomllib.loads(Path(cfg_path).read_text())
    stocks = load_scan(scan_path)
    current = current_universe(rules_path)
    chosen = select(stocks, cfg, current)
    text = report(stocks, chosen, current)
    if apply_to is not None:
        apply_universe(apply_to, [s.ticker for s in chosen])
        text += "\n\nApplied to: " + ", ".join(str(p) for p in apply_to)
    return text
