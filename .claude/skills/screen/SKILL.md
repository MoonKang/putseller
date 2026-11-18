---
name: screen
description: Occasionally re-pick the stocks /scan sells puts on. Pulls fundamentals and volatility for the S&P 100 from the Robinhood scanner, applies screen.toml, and proposes universe changes. Edits rules.toml and strategies/*.toml only after the user confirms.
---

# /screen

Stock quality changes slowly, so this runs occasionally (monthly, or after
earnings season), not on every /scan.

## Hard rules

- Read-only on Robinhood: use `preview_scan` only. Never `create_scan`,
  `run_scan`, or any order tool.
- **Propose, then wait.** Never pass `--apply` until the user says yes to the
  proposal shown in this run.
- Take numbers from the scanner as returned. Missing data fails a stock; do not
  look values up elsewhere to rescue it.

## Steps

1. **Check the connection.** If no `mcp__robinhood-trading__*` tools are
   available, tell the user to run `/mcp`, select `robinhood-trading`, and
   authenticate. Then stop.

2. **Pull the pool.** Read `pool` from `screen.toml`. Call `preview_scan` with:

   ```json
   {"filters": [{"expression": "symbol", "predicate": "PREDICATE_IN_LIST", "values": ["<pool>"]}],
    "columns": [
      {"display_name": "Mkt cap", "expression": "fundamental.marketCap"},
      {"display_name": "Beta", "expression": "fundamental.beta"},
      {"display_name": "ROE", "expression": "fundamental.returnOnEquity"},
      {"display_name": "Net margin", "expression": "fundamental.netProfitMargin"},
      {"display_name": "LT debt/cap", "expression": "fundamental.longTermDebtTotalCapitalRatio"},
      {"display_name": "FCF/share", "expression": "fundamental.cashFlowFreePerShare"},
      {"display_name": "Rev growth", "expression": "fundamental.quarterlyRevenueGrowth"},
      {"display_name": "Fwd PE", "expression": "fundamental.forwardPeRatio"},
      {"display_name": "Sector", "expression": "fundamental.morningstarSectorLabel"},
      {"display_name": "Opt OI", "expression": "optionsTotalOpenInterest"},
      {"display_name": "IV30", "expression": "atmIv30Day"},
      {"display_name": "IV rank", "expression": "ivRank"},
      {"display_name": "RV 1M", "expression": "statVol1Month"}]}
   ```

   The result is large and is saved to a file. Copy that file unchanged to
   `data/screens/<YYYY-MM-DD>.json`. If the scanner renames a column to a name
   `putseller/screen.py` does not know (`COLUMNS`), add the alias there; do
   not edit the saved file.

3. **Propose.** Run `python3 -m putseller screen data/screens/<date>.json`.
   Show the user the proposed list, what is added and dropped, and why each
   dropped stock was dropped.

   For each dropped ticker, check for open paper positions:

   ```bash
   for db in data/paper*.db; do sqlite3 "$db" "SELECT '$db', ticker, expiry, strike FROM positions WHERE settled_date IS NULL AND ticker IN (<dropped>)"; done
   ```

   Tell the user that /scan keeps fetching those tickers to mark the positions
   until they settle, but opens nothing new on them.

   Ask whether to apply. The user may also want thresholds in `screen.toml`
   changed first; if so, edit it and rerun this step.

4. **Apply** only after a yes:
   `python3 -m putseller screen data/screens/<date>.json --apply`. This
   rewrites the `universe = [...]` line in `rules.toml` and every
   `strategies/*.toml`. Show `git diff --stat` afterwards.
