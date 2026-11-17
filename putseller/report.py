"""Plain-text summaries of a run and of the whole log."""
from __future__ import annotations

import sqlite3

from .paper import RunResult


def _money(x: float | None) -> str:
    return "-" if x is None else f"{x:+,.0f}"


def format_run(r: RunResult) -> str:
    out = [f"Scan #{r.scan_id}: {r.evaluated} contracts in the delta/expiry window"]
    if r.verdicts:
        out.append("  " + ", ".join(f"{k} {v}" for k, v in sorted(r.verdicts.items())))
    if r.missing_tickers:
        out.append("  watchlist tickers missing from snapshot: " + ", ".join(r.missing_tickers))
    for o in r.opened:
        why = "passed all rules" if o["kind"] == "pass" else (
            f"blocked by {o['blocked_by']} ({o['blocked_detail']})")
        delta = "-" if o["delta"] is None else f"{o['delta']:.2f}"
        out.append(
            f"  OPEN  [{o['kind']}] {o['position']}  credit {o['credit']:.2f}  "
            f"delta {delta}  {o['annualized_return_pct']:.1f}%/yr  - {why}")
    for e in r.exits:
        out.append(f"  EXIT  [{e['kind']}] {e['position']}  {e['reason']}  P&L {_money(e['pnl'])}")
    if r.unmarked:
        out.append("  no quote today for: " + ", ".join(r.unmarked))
    if not r.opened and not r.exits:
        out.append("  nothing opened or closed")
    return "\n".join(out)


def format_report(conn: sqlite3.Connection) -> str:
    out: list[str] = []
    n, first, last = conn.execute(
        "SELECT COUNT(*), MIN(scan_date), MAX(scan_date) FROM scans").fetchone()
    if not n:
        return "No scans logged yet."
    out.append(f"{n} scans, {first} to {last}")

    out.append("\nVerdicts (contracts inside the delta/expiry window)")
    for row in conn.execute(
            "SELECT verdict, COUNT(*) c FROM evaluations GROUP BY verdict ORDER BY c DESC"):
        out.append(f"  {row['verdict']:<10} {row['c']}")

    counts: dict[str, int] = {}
    for (failed,) in conn.execute("SELECT failed_rules FROM evaluations WHERE failed_rules != ''"):
        for rule in failed.split(","):
            counts[rule] = counts.get(rule, 0) + 1
    if counts:
        out.append("\nRules that blocked most often")
        for rule, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            out.append(f"  {rule:<22} {c}")

    out.append("\nPaper results, dollars per contract")
    out.append(f"  {'group':<32}{'opened':>7}{'closed':>7}{'win%':>6}"
               f"{'avg':>8}{'worst':>8}{'total':>9}{'held':>9}")
    groups = conn.execute(
        "SELECT kind, blocked_by, COUNT(*) opened, COUNT(exit_date) closed,"
        " SUM(exit_pnl > 0) wins, AVG(exit_pnl) avg, MIN(exit_pnl) worst,"
        " SUM(exit_pnl) total, SUM(held_pnl) held"
        " FROM positions GROUP BY kind, blocked_by ORDER BY kind DESC, opened DESC")
    for g in groups:
        name = "passed all rules" if g["kind"] == "pass" else f"blocked by {g['blocked_by']}"
        win = "-" if not g["closed"] else f"{100 * (g['wins'] or 0) / g['closed']:.0f}"
        out.append(f"  {name:<32}{g['opened']:>7}{g['closed']:>7}{win:>6}"
                   f"{_money(g['avg']):>8}{_money(g['worst']):>8}"
                   f"{_money(g['total']):>9}{_money(g['held']):>9}")
    out.append("  (total = outcome under the exit rules; held = outcome if held to expiry,"
               " settled positions only)")

    open_rows = conn.execute(
        "SELECT * FROM positions WHERE exit_date IS NULL ORDER BY expiry, ticker").fetchall()
    if open_rows:
        out.append("\nOpen paper positions")
        for p in open_rows:
            tag = "pass" if p["kind"] == "pass" else f"blocked by {p['blocked_by']}"
            mark = conn.execute(
                "SELECT ask FROM marks WHERE position_id = ? AND ask IS NOT NULL"
                " ORDER BY mark_date DESC LIMIT 1", (p["id"],)).fetchone()
            pnl = None if mark is None else (p["entry_credit"] - mark["ask"]) * 100
            out.append(
                f"  {p['ticker']} {p['expiry']} {p['strike']:g}P  opened {p['entry_date']}"
                f"  credit {p['entry_credit']:.2f}  unrealized {_money(pnl)}  [{tag}]")
    return "\n".join(out)
