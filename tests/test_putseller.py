import unittest
from pathlib import Path

from putseller import bs, paper, report, screen, store
from putseller.config import load_rules
from putseller.models import parse_snapshot
from putseller import rules as rules_mod
from putseller.rules import evaluate

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules(ROOT / "rules.toml")


def snap(day="2026-11-17", price=62.10, puts=None, earnings="2027-02-10",
         sma200=60.40, cash=60000, ticker="KO", open_puts=None, portfolio_value=100000,
         closes=None, market=None):
    if puts is None:
        puts = [{"expiry": "2026-12-18", "strike": 60.0, "bid": 0.78, "ask": 0.82,
                 "open_interest": 5100}]
    return parse_snapshot({
        "taken_at": f"{day}T10:30:00-05:00",
        "account": {"portfolio_value": portfolio_value, "cash": cash},
        "open_puts": open_puts or [],
        "market": market or {},
        "tickers": [{"ticker": ticker, "sector": "Consumer Staples", "price": price,
                     "sma200": sma200, "next_earnings": earnings, "puts": puts,
                     "closes": closes or []}],
    })


CALM = [62.0 + (0.1 if i % 2 else 0.0) for i in range(21)]     # about 3% realized vol
CHOPPY = [62.0 + (3.0 if i % 2 else 0.0) for i in range(21)]   # about 77% realized vol
VIX_YEAR = [12.0 + 0.1 * i for i in range(250)]                # 12.0 to 36.9


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

    def test_timing_rules_are_na_without_data(self):
        e = eval_first(snap())
        for rule in ("iv_vs_realized", "vix_level", "vix_rank"):
            self.assertEqual(status(e, rule), "na")
        self.assertEqual(e.verdict, "pass")

    def test_timing_rules_pass_in_a_normal_market(self):
        e = eval_first(snap(closes=CALM, market={"vix": 30.0, "vix_closes_1y": VIX_YEAR}))
        for rule in ("iv_vs_realized", "vix_level", "vix_rank"):
            self.assertEqual(status(e, rule), "pass")

    def test_premium_below_realized_vol_blocks(self):
        self.assertEqual(eval_first(snap(closes=CHOPPY)).failed, ["iv_vs_realized"])

    def test_panic_vix_and_cheap_vix_block(self):
        e = eval_first(snap(market={"vix": 40.0, "vix_closes_1y": VIX_YEAR}))
        self.assertEqual(e.failed, ["vix_level"])
        e = eval_first(snap(market={"vix": 13.0, "vix_closes_1y": VIX_YEAR}))
        self.assertEqual(e.failed, ["vix_rank"])

    def test_atm_iv_interpolates_between_strikes_around_the_price(self):
        puts = [{"expiry": "2026-12-18", "strike": 60.0, "bid": 0.7, "ask": 0.8, "iv": 0.30},
                {"expiry": "2026-12-18", "strike": 65.0, "bid": 3.0, "ask": 3.1, "iv": 0.20}]
        s = snap(price=62.0, puts=puts)
        t = s.tickers["KO"]
        self.assertAlmostEqual(rules_mod.atm_iv(t, s.date, 0.04), 0.26)   # 2/5 of the way
        one_side = snap(price=62.0, puts=puts[:1]).tickers["KO"]
        self.assertAlmostEqual(rules_mod.atm_iv(one_side, s.date, 0.04), 0.30)

    def test_realized_vol_needs_enough_history(self):
        self.assertIsNone(rules_mod.realized_vol(tuple(CALM[:10])))
        self.assertAlmostEqual(rules_mod.realized_vol(tuple(CHOPPY)), 0.77, places=2)

    def test_empty_account_fails_sizing_rules_instead_of_crashing(self):
        e = eval_first(snap(portfolio_value=0, cash=0))
        for rule in ("cash_secured", "ticker_concentration", "sector_concentration",
                     "total_collateral", "stress"):
            self.assertIn(rule, e.failed)


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

    def test_paper_account_in_rules_replaces_an_empty_real_account(self):
        empty = snap(portfolio_value=0, cash=0)
        self.assertEqual(paper.run(self.conn, empty, RULES).verdicts, {"fail": 1})
        sized = load_rules(ROOT / "strategies" / "baseline.toml")
        result = paper.run(self.conn, snap(day="2026-11-18", portfolio_value=0, cash=0), sized)
        self.assertEqual(result.verdicts, {"pass": 1})

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

    def test_dropped_ticker_is_still_marked_but_not_traded(self):
        from dataclasses import replace
        paper.run(self.conn, snap(), RULES)
        dropped = replace(RULES, raw={**RULES.raw, "universe": ["AAPL"]})
        result = paper.run(self.conn, snap(day="2026-11-18"), dropped)
        self.assertEqual(result.unmarked, [])
        self.assertEqual(result.verdicts, {})
        self.assertEqual(len(self.positions()), 1)

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


class Screen(unittest.TestCase):
    CFG = {"max_stocks": 3, "max_per_sector": 1, "keep_current": True, "rank_by": "iv",
           "criteria": {"min_market_cap": 50e9, "min_roe": 0.10, "min_fcf_per_share": 0,
                        "fcf_exempt_sectors": ["Financial Services"], "max_price": 500}}

    @staticmethod
    def stock(ticker, sector="Technology", iv=0.3, name=None, **over):
        m = {k: None for k in screen.COLUMNS}
        m.update(price=100.0, market_cap=100e9, roe=0.2, fcf_per_share=5.0, iv=iv)
        m.update(over)
        return screen.Stock(ticker, name or ticker, sector, m)

    def test_criteria_fail_on_bad_or_missing_data(self):
        s = self.stock("X", roe=None, price=600.0)
        self.assertEqual(screen.check(s, self.CFG["criteria"]), ["roe", "price"])

    def test_banks_are_exempt_from_free_cash_flow(self):
        bank = self.stock("B", sector="Financial Services", fcf_per_share=-30.0)
        self.assertEqual(screen.check(bank, self.CFG["criteria"]), [])
        self.assertEqual(screen.check(self.stock("T", fcf_per_share=-1.0), self.CFG["criteria"]),
                         ["fcf_per_share"])

    def test_select_ranks_caps_sectors_dedupes_and_keeps_incumbents(self):
        stocks = [
            self.stock("HOT", iv=0.9),                         # tech, but OLD holds the tech slot
            self.stock("OLD", iv=0.2),                         # tech, current holding
            self.stock("GOOG", sector="Energy", iv=0.6, name="Alphabet Class C"),
            self.stock("GOOGL", sector="Comm", iv=0.5, name="Alphabet Class A"),  # same company
            self.stock("OIL", sector="Energy", iv=0.4),        # energy slot taken
            self.stock("BANK", sector="Financial Services", iv=0.1),
            self.stock("LATE", sector="Health", iv=0.05),      # list is full
            self.stock("PRICY", sector="Utilities", iv=0.99, price=900.0),  # fails criteria
        ]
        chosen = [s.ticker for s in screen.select(stocks, self.CFG, current=["OLD"])]
        self.assertEqual(chosen, ["OLD", "GOOG", "BANK"])

    def test_apply_universe_rewrites_the_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "r.toml"
            f.write_text('a = 1\nuniverse = ["X", "Y"]\nb = 2\n')
            screen.apply_universe([f], ["A", "B"])
            self.assertEqual(f.read_text(), 'a = 1\nuniverse = ["A", "B"]\nb = 2\n')
            f.write_text("a = 1\n")
            with self.assertRaises(ValueError):
                screen.apply_universe([f], ["A"])
