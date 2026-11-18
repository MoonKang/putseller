---
name: scan
description: Pull live data from the Robinhood Trading MCP, write a putseller snapshot, run the paper log, and propose (never place) cash-secured put sells for contracts that passed every rule.
---

# /scan

Builds one snapshot from Robinhood, runs `python3 -m putseller run` on it, and
turns each new `pass` into a trade proposal the user places in the Robinhood app.

## Hard rules

- **Never place, cancel, or exercise anything.** Only read tools and
  `review_option_order` (a preview) are allowed. The order tools are denied in
  `.claude/settings.json`; do not try to get around that.
- Copy numbers exactly as the tools return them. Do not estimate, round, or fill
  in a missing value. Leave an optional field out (or `null`) instead.
- If a required value (price, bid, ask, strike, expiry, portfolio value, cash)
  cannot be fetched, stop and report it. Do not write a partial snapshot.

## Steps

1. **Check the connection.** If no `mcp__robinhood-trading__*` tools are
   available, tell the user to run `/mcp`, select `robinhood-trading`, and
   authenticate. Then stop.

2. **Read the rules.** From `rules.toml`, take `universe`, `[entry] dte_min`,
   `dte_max`, `delta_min`, `delta_max`.

3. **Account** (use the MCP account, the one the agent trades in; find it with
   `get_accounts`):
   - `get_portfolio` gives `account.portfolio_value` (total value) and
     `account.cash` (cash / buying power available for collateral).
   - `get_equity_positions` gives `holdings`: `ticker`, `market_value`.
   - `get_option_positions` gives `open_puts`: short puts only, with `ticker`,
     `strike`, `expiry`, `contracts`.

4. **Per ticker in the universe:**
   - `get_equity_quotes` (up to 20 symbols per call) gives `price` (last trade).
   - `get_equity_fundamentals` gives `sector` if it is there.
   - `sma200`: the mean of the last 200 daily closes from
     `get_equity_historicals`, or the 200-day SMA from
     `get_equity_technical_indicators`. Omit it if neither works.
   - `get_earnings_calendar` gives `next_earnings` (YYYY-MM-DD), the next
     report date on or after today. Omit it if unknown.
   - Leave `iv_rank` as `null`. putseller computes it from stored IV history.
   - Options: `get_option_chains` and `get_option_instruments` for **puts**
     whose expiry is `dte_min` to `dte_max` days out, with strikes between
     about 70% and 100% of the price. Then `get_option_quotes` for each one
     gives `bid`, `ask`, `open_interest`, `volume`, `iv` (a fraction:
     0.25 = 25%), and `delta`.

5. **Write the snapshot** to `data/snapshots/<YYYY-MM-DDTHHMM>.json` in the
   format described in `README.md`. Set `taken_at` to the current time in
   America/New_York with its offset (`TZ=America/New_York date -Iseconds`).

6. **Run** `python3 -m putseller run <snapshot>`. If it fails with a validation
   error, fix the snapshot from the tool data (never from guesses) and rerun.

7. **Propose.** For each `OPEN [pass]` line in the output, call
   `review_option_order` for: sell to open, 1 contract, limit at the bid, in the
   MCP account. Show the user one table with ticker, expiry, strike, limit,
   credit, collateral (strike × 100), delta, annualized %, and anything the
   review flagged. Say clearly that nothing was placed and that they place
   each order themselves in the Robinhood app.

   `near_miss` lines are paper-only. Mention them briefly, but do not propose
   them.

8. Also show any `EXIT` lines. These are paper positions. If a real short put in
   `open_puts` has the same ticker, strike, and expiry, point it out so the user
   can decide. Do not act on it.
