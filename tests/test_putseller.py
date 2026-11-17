import unittest
from pathlib import Path

from putseller import bs, paper, report, store
from putseller.config import load_rules
from putseller.models import parse_snapshot
from putseller.rules import evaluate

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules(ROOT / "rules.toml")


def snap(day="2026-11-17", price=62.10, puts=None, earnings="2027-02-10",
         sma200=60.40, cash=60000, ticker="KO", open_puts=None):
    if puts is None:
        puts = [{"expiry": "2026-12-18", "strike": 60.0, "bid": 0.78, "ask": 0.82,
                 "open_interest": 5100}]
    return parse_snapshot({
        "taken_at": f"{day}T10:30:00-05:00",
        "account": {"portfolio_value": 100000, "cash": cash},
        "open_puts": open_puts or [],
        "tickers": [{"ticker": ticker, "sector": "Consumer Staples", "price": price,
                     "sma200": sma200, "next_earnings": earnings, "puts": puts}],
    })


def status(evaluation, rule):
    return next(c.status for c in evaluation.checks if c.rule == rule)


def eval_first(s):
    t = next(iter(s.tickers.values()))
    return evaluate(s, t, t.puts[0], RULES)


class BlackScholes(unittest.TestCase):
    def test_implied_vol_round_trip(self):
        price = bs.put_price(100, 95, 35 / 365, 0.04, 0.27)
        self.assertAlmostEqual(bs.implied_vol(price, 100, 95, 35 / 365, 0.04), 0.27, places=4)

    def test_delta_bounds_and_moneyness(self):
        otm = bs.put_delta(100, 90, 0.1, 0.04, 0.25)
        itm = bs.put_delta(100, 110, 0.1, 0.04, 0.25)
        self.assertTrue(-1 < itm < otm < 0)

    def test_implied_vol_rejects_impossible_price(self):
        self.assertIsNone(bs.implied_vol(0.0, 100, 95, 0.1, 0.04))
        self.assertIsNone(bs.implied_vol(200.0, 100, 95, 0.1, 0.04))


class Rules(unittest.TestCase):
    def test_clean_candidate_passes(self):
        e = eval_first(snap())
        self.assertEqual(e.failed, [])
        self.assertEqual(e.verdict, "pass")
        self.assertEqual(status(e, "iv_rank"), "na")   # no history yet: recorded, not blocking
        self.assertTrue(0.15 <= e.delta <= 0.30)

    def test_earnings_inside_window_blocks(self):
        e = eval_first(snap(earnings="2026-12-10"))
        self.assertEqual(e.failed, ["earnings"])
        self.assertEqual(e.verdict, "near_miss")

    def test_below_200dma_blocks(self):
        self.assertEqual(eval_first(snap(sma200=65.0)).failed, ["trend"])

    def test_not_enough_cash_blocks(self):
        self.assertIn("cash_secured", eval_first(snap(cash=5000)).failed)

    def test_wide_spread_and_thin_open_interest(self):
        e = eval_first(snap(puts=[{"expiry": "2026-12-18", "strike": 60.0, "bid": 0.70,
                                   "ask": 0.90, "open_interest": 40}]))
        self.assertEqual(sorted(e.failed), ["open_interest", "spread"])
        self.assertEqual(e.verdict, "fail")

    def test_out_of_window_expiry_is_not_selected(self):
        e = eval_first(snap(puts=[{"expiry": "2027-03-19", "strike": 60.0, "bid": 2.0,
                                   "ask": 2.1, "open_interest": 900}]))
        self.assertFalse(e.selected)

    def test_off_watchlist_blocks(self):
        self.assertEqual(eval_first(snap(ticker="ZZZZ")).failed, ["watchlist"])

    def test_concentration_and_collateral_count_existing_puts(self):
        existing = [{"ticker": "KO", "strike": 60, "expiry": "2026-12-18", "contracts": 8,
                     "underlying_price": 62.1}]
        e = eval_first(snap(open_puts=existing))
        for rule in ("ticker_concentration", "sector_concentration", "total_collateral"):
            self.assertIn(rule, e.failed)

    def test_stress_test_blocks_a_large_book(self):
        existing = [{"ticker": "KO", "strike": 60, "expiry": "2026-12-18", "contracts": 20,
                     "underlying_price": 62.1}]
        self.assertIn("stress", eval_first(snap(open_puts=existing)).failed)


class PaperLog(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def positions(self):
        return self.conn.execute("SELECT * FROM positions ORDER BY id").fetchall()

    def test_pass_opens_one_position_at_the_bid(self):
        result = paper.run(self.conn, snap(), RULES)
        self.assertEqual(result.verdicts, {"pass": 1})
        (p,) = self.positions()
        self.assertEqual((p["kind"], p["entry_credit"], p["rules_version"]),
                         ("pass", 0.78, RULES.version))

    def test_no_duplicate_while_position_is_running(self):
        paper.run(self.conn, snap(), RULES)
        paper.run(self.conn, snap(day="2026-11-18"), RULES)
        self.assertEqual(len(self.positions()), 1)

    def test_same_snapshot_twice_is_rejected(self):
        paper.run(self.conn, snap(), RULES)
        with self.assertRaises(ValueError):
            paper.run(self.conn, snap(), RULES)
        with self.assertRaises(ValueError):
            paper.run(self.conn, snap(day="2026-11-10"), RULES)

    def test_near_miss_is_tracked_with_its_blocking_rule(self):
        paper.run(self.conn, snap(earnings="2026-12-10"), RULES)
        (p,) = self.positions()
        self.assertEqual((p["kind"], p["blocked_by"]), ("near_miss", "earnings"))

    def test_take_profit_then_settlement_at_expiry(self):
        paper.run(self.conn, snap(), RULES)
        cheap = [{"expiry": "2026-12-18", "strike": 60.0, "bid": 0.30, "ask": 0.35,
                  "open_interest": 5100}]
        result = paper.run(self.conn, snap(day="2026-11-27", price=64.0, puts=cheap), RULES)
        self.assertEqual(result.exits[0]["reason"], "take_profit")
        self.assertAlmostEqual(result.exits[0]["pnl"], 43.0)      # (0.78 - 0.35) * 100

        # Held to expiry instead, the stock finished below the strike.
        paper.run(self.conn, snap(day="2026-12-18", price=58.0, puts=[]), RULES)
        p = self.positions()[0]
        self.assertEqual(p["exit_reason"], "take_profit")
        self.assertAlmostEqual(p["exit_pnl"], 43.0)
        self.assertAlmostEqual(p["held_pnl"], -122.0)             # (0.78 - 2.00) * 100

    def test_defend_when_stock_falls_through_strike(self):
        paper.run(self.conn, snap(), RULES)
        itm = [{"expiry": "2026-12-18", "strike": 60.0, "bid": 1.40, "ask": 1.50,
                "open_interest": 5100}]
        result = paper.run(self.conn, snap(day="2026-11-20", price=59.2, puts=itm), RULES)
        self.assertEqual(result.exits[0]["reason"], "defend")
        self.assertAlmostEqual(result.exits[0]["pnl"], -72.0)

    def test_max_loss_and_time_exit(self):
        paper.run(self.conn, snap(), RULES)
        dear = [{"expiry": "2026-12-18", "strike": 60.0, "bid": 1.55, "ask": 1.60,
                 "open_interest": 5100}]
        result = paper.run(self.conn, snap(day="2026-11-20", price=60.5, puts=dear), RULES)
        self.assertEqual(result.exits[0]["reason"], "max_loss")

        other = store.connect(":memory:")
        self.addCleanup(other.close)
        paper.run(other, snap(), RULES)
        flat = [{"expiry": "2026-12-18", "strike": 60.0, "bid": 0.60, "ask": 0.64,
                 "open_interest": 5100}]
        result = paper.run(other, snap(day="2026-11-27", puts=flat), RULES)
        self.assertEqual(result.exits[0]["reason"], "time_exit")

    def test_expires_worthless_without_an_earlier_exit(self):
        paper.run(self.conn, snap(), RULES)
        result = paper.run(self.conn, snap(day="2026-12-18", price=63.0, puts=[]), RULES)
        self.assertEqual(result.exits[0]["reason"], "expired")
        self.assertAlmostEqual(self.positions()[0]["held_pnl"], 78.0)

    def test_missing_quote_is_reported_not_guessed(self):
        paper.run(self.conn, snap(), RULES)
        result = paper.run(self.conn, snap(day="2026-11-18", puts=[]), RULES)
        self.assertEqual(result.exits, [])
        self.assertEqual(len(result.unmarked), 1)

    def test_iv_rank_appears_once_history_is_long_enough(self):
        for i in range(RULES.iv_min_history_days):
            self.conn.execute("INSERT INTO iv_history VALUES ('KO', ?, ?)",
                              (f"2026-10-{i + 1:02d}", 0.10 + i * 0.01))
        paper.run(self.conn, snap(), RULES)
        checks = self.conn.execute("SELECT checks_json FROM evaluations").fetchone()[0]
        self.assertIn("IV rank", checks)

    def test_reports_render(self):
        self.assertEqual(report.format_report(self.conn), "No scans logged yet.")
        text = report.format_run(paper.run(self.conn, snap(earnings="2026-12-10"), RULES))
        self.assertIn("blocked by earnings", text)
        self.assertIn("blocked by earnings", report.format_report(self.conn))


if __name__ == "__main__":
    unittest.main()
