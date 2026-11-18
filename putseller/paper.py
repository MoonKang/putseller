"""One paper-trading run: mark open positions, then scan for new ones."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta

from .config import Rules
from .models import Snapshot, TickerSnap
from .rules import Evaluation, atm_iv, evaluate, greeks


@dataclass
class RunResult:
    scan_id: int
    exits: list[dict] = field(default_factory=list)
    settled: list[dict] = field(default_factory=list)
    unmarked: list[str] = field(default_factory=list)   # positions with no data today
    opened: list[dict] = field(default_factory=list)
    evaluated: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    missing_tickers: list[str] = field(default_factory=list)


def run(conn: sqlite3.Connection, snap: Snapshot, rules: Rules,
        snapshot_path: str | None = None) -> RunResult:
    taken_at = snap.taken_at.isoformat()
    last = conn.execute("SELECT MAX(taken_at) FROM scans").fetchone()[0]
    if conn.execute("SELECT 1 FROM scans WHERE taken_at = ?", (taken_at,)).fetchone():
        raise ValueError(f"snapshot taken at {taken_at} was already logged")
    if last and taken_at < last:
        raise ValueError(f"snapshot ({taken_at}) is older than the latest logged scan ({last})")

    # A strategy file may size its paper account itself instead of using the real one.
    acct = rules.raw.get("paper_account")
    if acct:
        snap = replace(snap, portfolio_value=float(acct["portfolio_value"]),
                       cash=float(acct["cash"]))

    with conn:
        scan_id = conn.execute(
            "INSERT INTO scans (taken_at, scan_date, snapshot_path, rules_version, rules_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (taken_at, snap.date.isoformat(), snapshot_path, rules.version,
             json.dumps(rules.raw, sort_keys=True)),
        ).lastrowid
        result = RunResult(scan_id=scan_id)
        _mark_positions(conn, snap, rules, result)
        _scan(conn, snap, rules, result)
    return result


# ---------------------------------------------------------------- marking

def _mark_positions(conn, snap: Snapshot, rules: Rules, result: RunResult) -> None:
    ex = rules["exit"]
    today = snap.date
    rows = conn.execute(
        "SELECT * FROM positions WHERE settled_date IS NULL ORDER BY id").fetchall()
    for p in rows:
        label = f"{p['ticker']} {p['expiry']} {p['strike']:g}P"
        t = snap.tickers.get(p["ticker"])
        if t is None:
            result.unmarked.append(label)
            continue
        expiry = date.fromisoformat(p["expiry"])
        credit = p["entry_credit"]

        if today >= expiry:
            # Settled at whatever price the first snapshot on/after expiry shows.
            intrinsic = max(p["strike"] - t.price, 0.0)
            pnl = round((credit - intrinsic) * 100, 2)
            conn.execute(
                "UPDATE positions SET settled_date=?, settle_underlying=?, held_pnl=?"
                " WHERE id=?", (today.isoformat(), t.price, pnl, p["id"]))
            if p["exit_date"] is None:
                reason = "assigned" if intrinsic > 0 else "expired"
                conn.execute(
                    "UPDATE positions SET exit_date=?, exit_reason=?, exit_cost=?, exit_pnl=?"
                    " WHERE id=?", (today.isoformat(), reason, intrinsic, pnl, p["id"]))
                result.exits.append(_exit_row(p, label, reason, pnl))
            result.settled.append({"position": label, "kind": p["kind"], "held_pnl": pnl})
            continue

        quote = t.find_put(expiry, p["strike"])
        dte = (expiry - today).days
        delta = None
        if quote is not None:
            delta, _ = greeks(quote, t.price, dte, rules.risk_free_rate)
        conn.execute(
            "INSERT OR REPLACE INTO marks VALUES (?, ?, ?, ?, ?, ?)",
            (p["id"], today.isoformat(), t.price,
             quote.bid if quote else None, quote.ask if quote else None, delta))
        if p["exit_date"] is not None:
            continue            # already exited; only waiting for expiry
        if quote is None or quote.ask <= 0:
            result.unmarked.append(label)
            continue

        cost = quote.ask
        reason = None
        if cost <= credit * (1 - ex["take_profit_pct"] / 100):
            reason = "take_profit"
        elif cost >= credit * ex["max_loss_multiple"]:
            reason = "max_loss"
        elif t.price < p["strike"] or (delta is not None and delta > ex["defend_delta"]):
            reason = "defend"
        elif dte <= ex["time_exit_dte"]:
            reason = "time_exit"
        if reason:
            pnl = round((credit - cost) * 100, 2)
            conn.execute(
                "UPDATE positions SET exit_date=?, exit_reason=?, exit_cost=?, exit_pnl=?"
                " WHERE id=?", (today.isoformat(), reason, cost, pnl, p["id"]))
            result.exits.append(_exit_row(p, label, reason, pnl))


def _exit_row(p, label: str, reason: str, pnl: float) -> dict:
    return {"position": label, "kind": p["kind"], "blocked_by": p["blocked_by"],
            "reason": reason, "pnl": pnl}


# ---------------------------------------------------------------- IV rank

def _iv_rank(conn, t: TickerSnap, today: date, rules: Rules) -> float | None:
    iv = atm_iv(t, today, rules.risk_free_rate)
    if iv is not None:
        conn.execute("INSERT OR REPLACE INTO iv_history VALUES (?, ?, ?)",
                     (t.ticker, today.isoformat(), iv))
    if iv is None:
        return None
    since = (today - timedelta(days=365)).isoformat()
    n, lo, hi = conn.execute(
        "SELECT COUNT(*), MIN(atm_iv), MAX(atm_iv) FROM iv_history"
        " WHERE ticker = ? AND day >= ? AND day <= ?",
        (t.ticker, since, today.isoformat())).fetchone()
    if n < rules.iv_min_history_days or hi <= lo:
        return None
    return 100 * (iv - lo) / (hi - lo)


# ---------------------------------------------------------------- scanning

def _scan(conn, snap: Snapshot, rules: Rules, result: RunResult) -> None:
    result.missing_tickers = sorted(rules.universe - set(snap.tickers))
    for t in snap.tickers.values():
        if t.ticker.upper() not in rules.universe:
            continue   # fetched only to mark a position on a dropped ticker
        rank = _iv_rank(conn, t, snap.date, rules)
        atm = atm_iv(t, snap.date, rules.risk_free_rate)
        selected = [e for e in (evaluate(snap, t, p, rules, rank, atm) for p in t.puts)
                    if e.selected]
        for e in selected:
            _log_evaluation(conn, result, e)

        passes = [e for e in selected if e.verdict == "pass"]
        if passes:
            _open(conn, snap, rules, result, max(passes, key=lambda e: e.score), "pass", None)
        blocked: dict[str, Evaluation] = {}
        for e in selected:
            if e.verdict == "near_miss":
                rule = e.failed[0]
                if rule not in blocked or e.score > blocked[rule].score:
                    blocked[rule] = e
        for rule, e in blocked.items():
            _open(conn, snap, rules, result, e, "near_miss", rule)


def _log_evaluation(conn, result: RunResult, e: Evaluation) -> None:
    conn.execute(
        "INSERT INTO evaluations (scan_id, ticker, expiry, strike, dte, underlying, bid, ask,"
        " delta, iv, annualized_return_pct, verdict, failed_rules, checks_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (result.scan_id, e.ticker, e.put.expiry.isoformat(), e.put.strike, e.dte,
         e.underlying, e.put.bid, e.put.ask, e.delta, e.iv, e.annualized_return_pct,
         e.verdict, ",".join(e.failed), json.dumps([asdict(c) for c in e.checks])))
    result.evaluated += 1
    result.verdicts[e.verdict] = result.verdicts.get(e.verdict, 0) + 1


def _open(conn, snap: Snapshot, rules: Rules, result: RunResult,
          e: Evaluation, kind: str, blocked_by: str | None) -> None:
    """Open a one-contract paper position unless a matching one is still running."""
    if e.put.bid <= 0:
        return
    running = conn.execute(
        "SELECT 1 FROM positions WHERE ticker = ? AND kind = ? AND blocked_by IS ?"
        " AND exit_date IS NULL", (e.ticker, kind, blocked_by)).fetchone()
    if running:
        return
    conn.execute(
        "INSERT INTO positions (scan_id, kind, blocked_by, ticker, expiry, strike, entry_date,"
        " entry_credit, entry_underlying, entry_delta, rules_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (result.scan_id, kind, blocked_by, e.ticker, e.put.expiry.isoformat(), e.put.strike,
         snap.date.isoformat(), e.put.bid, e.underlying, e.delta, rules.version))
    detail = next((c.detail for c in e.checks if c.rule == blocked_by), None)
    result.opened.append({
        "position": f"{e.ticker} {e.put.expiry} {e.put.strike:g}P",
        "kind": kind, "blocked_by": blocked_by, "blocked_detail": detail,
        "credit": e.put.bid, "delta": e.delta, "dte": e.dte,
        "annualized_return_pct": e.annualized_return_pct,
    })
