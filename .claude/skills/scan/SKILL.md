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

   The tickers to fetch are `universe` plus any ticker with an open paper
   position in any log (it may have been dropped by `/screen` but still needs
   marking until it settles):

   ```bash
   for db in data/paper*.db; do sqlite3 "$db" "SELECT DISTINCT ticker FROM positions WHERE settled_date IS NULL"; done | sort -u
   ```

   putseller only opens trades on tickers in the universe.

3. **Account** (use the MCP account, the one the agent trades in; find it with
   `get_accounts`):
   - `get_portfolio` gives `account.portfolio_value` (total value) and
     `account.cash` (cash / buying power available for collateral).
   - `get_equity_positions` gives `holdings`: `ticker`, `market_value`.
   - `get_option_positions` gives `open_puts`: short puts only, with `ticker`,
     `strike`, `expiry`, `contracts`.

4. **Per ticker to fetch:**
   - `get_equity_quotes` (up to 20 symbols per call) gives `price` (last trade).
   - `get_equity_fundamentals` gives `sector` if it is there.
   - `sma200`: the mean of the last 200 daily closes from
     `get_equity_historicals`, or the 200-day SMA from
     `get_equity_technical_indicators`. Omit it if neither works.
   - `closes`: the last 21 official daily closes, oldest first, from
     `get_equity_historicals` (interval `day`, about 35 calendar days). Skip
     bars with `interpolated: true`. If today's session has closed but its bar
     is not in yet, append today's `close.price` from `get_equity_quotes`.
     Omit `closes` if fewer than 21 are available.
   - `get_earnings_results` (one symbol per call) gives `next_earnings`
     (YYYY-MM-DD): the report date of the entry whose `eps.actual` is null.
     Omit it if unknown.
   - Leave `iv_rank` as `null`. putseller computes it from stored IV history.
   - Options: `get_option_chains` and `get_option_instruments` for **puts**
     whose expiry is **21 to 60 days out**, with strikes between about 70%
     and 100% of the price, plus the first strike above the price (needed
     for at-the-money IV). Then `get_option_quotes` for each one gives
     `bid`, `ask`, `open_interest`, `volume`, `iv` (a fraction: 0.25 = 25%),
     and `delta`. This is wider than any strategy trades on purpose: each
     snapshot doubles as a daily chain archive for backtesting (about 450
     contracts, 30-35 requests). Widen it if a strategy's DTE window ever
     goes outside 21-60 days. Save contract IDs and quotes to files as you
     go, keyed by instrument ID, so nothing depends on memory.

   **Market** (once per scan): `get_index_quotes` with symbol `VIX` gives
   `market.vix` (its `value`). `get_indexes` (`VIX`) gives its instrument id;
   `get_index_historicals` with that id, interval `day`, starting one year
   back, gives `market.vix_closes_1y`: every `close_value`, oldest first,
   skipping `interpolated` bars. Omit either field if it cannot be fetched.

5. **Write the snapshot** to `data/snapshots/<YYYY-MM-DDTHHMM>.json` in the
   format described in `README.md`. Set `taken_at` to the current time in
   America/New_York with its offset (`TZ=America/New_York date -Iseconds`).

6. **Run** `python3 -m putseller run <snapshot>`. If it fails with a validation
   error, fix the snapshot from the tool data (never from guesses) and rerun.

   Then run every paper strategy on the same snapshot, each into its own log:

   ```bash
   for f in strategies/*.toml; do s=$(basename "$f" .toml); echo "== $s"; python3 -m putseller --db "data/paper-$s.db" run <snapshot> --rules "$f"; done
   ```

   Strategy files size their own pretend account (`[paper_account]`), so
   their passes are paper-only. Never propose a trade from a strategy run.
   If a strategy's DTE window is wider than `rules.toml`'s, fetch the wider
   window in step 4 so every strategy sees its contracts.

7. **Propose.** For each `OPEN [pass]` line in the `rules.toml` run (not the
   strategy runs), call
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

9. **Compare strategies.** Summarize each strategy run in one short table:
   what it opened, exited and settled. For the running totals, the user can run
   `python3 -m putseller --db data/paper-<name>.db report` per strategy.
