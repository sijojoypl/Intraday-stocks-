# Dhan 1-min data collection (30 Jun – 25 Sep 2026)

Market data pulled through the Dhan MCP connector for the strategy optimisation.

- `stocks_daily_all.json`, `nifty_daily.json`: official daily OHLC (29 Jun – 25 Sep).
- `selection_all.json`: per day, Nifty gap class and the 2 stocks the strategy picks.
- `bars_state/<date>_<symbol>.json`: 1-min bars 09:15–15:14 for each pick (`done: true` when complete).
- `bars.py`: resumable collector (5 bars per Dhan call). `bars_prompt.txt`: relay instructions for agents.
- `build_days.py`: turns the above into engine-ready day files in `days/`.

Resume: for every `selection_all.json` pick whose state file isn't `done`, run
`python3 bars.py init <date> <symbol> <securityId>` and follow `bars_prompt.txt`.
Then: `python3 build_days.py` and
`python scripts/optimize_strategy.py --start 2026-06-30 --end 2026-09-25 --source csv --data-dir backtests/dhan_collection/days`.
