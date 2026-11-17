# put-seller

Paper trading log for a rule-based put-selling strategy. It places no orders and
talks to no broker: it reads a snapshot file, applies the rules in `rules.toml`,
and records what would have been sold and how it turned out.

Standard library only (Python 3.11+).

## Usage

```bash
python3 -m putseller run data/snapshots/example.json
```

```bash
python3 -m putseller report
```

```bash
python3 -m unittest discover -s tests
```

`run` does two things with one snapshot:

1. **Marks** every open paper position: applies the exit rules, and settles
   anything at or past expiry.
2. **Scans** every put in the snapshot and logs each contract inside the
   delta/expiry window with its verdict and the rules it failed.

The log lives in `data/paper.db` (SQLite, not committed).

## What gets paper traded

One contract per position, sold at the bid and bought back at the ask.

- **pass**: the best contract (highest annualized return) per ticker that passed
  every rule. One running position per ticker.
- **near_miss**: a contract blocked by exactly one filter rule. One running
  position per ticker per blocking rule. These answer "what did this rule cost
  or save me?".

Each position records two outcomes: `exit_pnl` under the exit rules, and
`held_pnl` had it been held to expiry.

## Rules

`delta` and `dte` are selection rules: they choose which contracts on a chain are
candidates. Everything else is a filter. A rule whose data is missing from the
snapshot is recorded as `na` and does not block.

Every scan stores a hash and a copy of `rules.toml`, so each trade is tied to the
rules that produced it.

Portfolio rules (cash, concentration, collateral, stress) are checked against the
real account in the snapshot. Paper positions do not use up paper cash.

## Snapshot format

One JSON file per scan. `taken_at` must be in market local time and later than
the previous snapshot.

```json
{
  "taken_at": "2026-11-17T10:30:00-05:00",
  "account": {"portfolio_value": 100000, "cash": 60000},
  "holdings": [{"ticker": "MSFT", "sector": "Technology", "market_value": 9000}],
  "open_puts": [{"ticker": "KO", "strike": 60, "expiry": "2026-12-18",
                 "contracts": 1, "underlying_price": 62.1}],
  "tickers": [{
    "ticker": "KO", "sector": "Consumer Staples", "price": 62.10,
    "sma200": 60.40, "next_earnings": "2027-02-10", "iv_rank": null,
    "puts": [{"expiry": "2026-12-18", "strike": 60.0, "bid": 0.78, "ask": 0.82,
              "open_interest": 5100, "iv": null, "delta": null}]
  }]
}
```

Required: `taken_at`, `account`, and for each ticker `ticker`, `price`, and each
put's `expiry`, `strike`, `bid`, `ask`. Everything else is optional.

- `delta` / `iv` missing: computed from the mid price with Black-Scholes
  (European, no dividends, so approximate).
- `iv_rank` missing: computed from the at-the-money IV stored on each run, once
  `iv_rank.min_history_days` days exist.
- A snapshot must keep including the tickers and contracts of open paper
  positions, or they cannot be marked. Missing ones are listed in the run output.

## Known simplifications

- Expiry settles at the price in the first snapshot on or after the expiry date.
- No commissions, fees or early assignment.
- Exit rules are checked only when a snapshot arrives, so a mid-day breach
  between snapshots is seen late.
