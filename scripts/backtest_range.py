"""
Multi-day backtest runner for the 3-minute breakout strategy.

Runs BacktestEngine once per trading day over a date range and prints an
aggregate report (win rate, net P&L after costs, drawdown, per-day table).

Data sources:
    --source angel   Fetch 3-min candles from Angel One (needs .env creds).
                     Responses are cached under data/backtest_cache/ so a
                     re-run doesn't hit the API again.
    --source dhan    Fetch 1-min candles from Dhan v2 and roll them up to
                     3-min bars (Dhan has no 3-min interval). Needs
                     DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env, or
                     DHAN_TOKEN_FILE pointing at a JSON file with
                     "access_token" and "client_id". Uses the
                     NSE token from config/nifty50.json as Dhan securityId.
    --source csv     Read cached/exported candles from --data-dir. One JSON
                     file per day: {"nifty": [...], "stocks": {sym: [...]}},
                     each candle {timestamp, open, high, low, close, volume},
                     including the previous trading day's candles.

Usage:
    python scripts/backtest_range.py --days 30
    python scripts/backtest_range.py --start 2026-08-26 --end 2026-09-25
    python scripts/backtest_range.py --days 30 --source dhan
    python scripts/backtest_range.py --days 30 --source csv --data-dir data/backtest_cache
"""

import argparse
import json
import sys
import time as time_mod
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

# Load transaction_costs directly so we don't pull in src.analysis.__init__
# (it imports `ta`/pandas indicators the backtest doesn't need).
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "transaction_costs",
    Path(__file__).resolve().parent.parent / "src/analysis/transaction_costs.py",
)
_tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tc)
TransactionCostCalculator = _tc.TransactionCostCalculator
from src.backtest.backtest_engine import BacktestEngine

CACHE_DIR = Path("data/backtest_cache")
NIFTY_TOKEN = "99926000"


def load_config() -> Dict:
    with open("config/settings.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_stock_list() -> List[Dict]:
    with open("config/nifty50.json", "r", encoding="utf-8") as f:
        return json.load(f).get("stocks", [])


def weekdays(start: date, end: date) -> List[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def fetch_day_angel(client, date_str: str, stock_list: List[Dict]) -> Optional[Dict]:
    cache_file = CACHE_DIR / f"{date_str}.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    nifty = client.get_historical_data_for_date(
        symbol="NIFTY", token=NIFTY_TOKEN, date_str=date_str,
        interval="THREE_MINUTE", exchange="NSE", include_prev_day=True,
    )
    if not nifty:
        return None  # holiday / no data

    stocks = {}
    for stock in stock_list:
        symbol, token = stock.get("symbol", ""), stock.get("token", "")
        if not symbol or not token:
            continue
        candles = client.get_historical_data_for_date(
            symbol=symbol, token=token, date_str=date_str,
            interval="THREE_MINUTE", exchange="NSE", include_prev_day=True,
        )
        if candles:
            stocks[symbol] = candles
        time_mod.sleep(0.3)  # same throttle as the dashboard endpoint

    day = {"nifty": nifty, "stocks": stocks}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(day, f, default=str)
    return day


DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
DHAN_NIFTY_SECURITY_ID = "13"  # NIFTY 50 index, segment IDX_I


def dhan_fetch_1m(security_id: str, segment: str, instrument: str,
                  start: date, end: date) -> List[Dict]:
    """Fetch 1-min candles from Dhan in <=90-day chunks."""
    import os
    import requests

    headers = {
        "access-token": os.getenv("DHAN_ACCESS_TOKEN", ""),
        "client-id": os.getenv("DHAN_CLIENT_ID", ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    out: List[Dict] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=89))
        body = {
            "securityId": str(security_id),
            "exchangeSegment": segment,
            "instrument": instrument,
            "interval": "1",
            "oi": False,
            "fromDate": f"{chunk_start} 09:00:00",  # Dhan excludes a bar starting exactly at fromDate
            "toDate": f"{chunk_end} 15:30:00",
        }
        resp = None
        for attempt in range(4):
            try:
                resp = requests.post(DHAN_INTRADAY_URL, json=body, headers=headers, timeout=30)
            except requests.RequestException as e:
                logger.warning(f"Dhan {security_id}: {e.__class__.__name__}, retrying")
                time_mod.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                time_mod.sleep(2 ** attempt)
                continue
            break
        if resp is None:
            logger.warning(f"Dhan {security_id}: request failed after retries")
            return out
        if resp.status_code != 200:
            logger.warning(f"Dhan {security_id}: HTTP {resp.status_code} {resp.text[:200]}")
            return out
        data = resp.json()
        for i, ts in enumerate(data.get("timestamp", [])):
            # Dhan returns epoch seconds; convert to naive IST
            dt = datetime.utcfromtimestamp(ts) + timedelta(hours=5, minutes=30)
            out.append({
                "dt": dt,
                "open": data["open"][i], "high": data["high"][i],
                "low": data["low"][i], "close": data["close"][i],
                "volume": data.get("volume", [0] * (i + 1))[i],
            })
        chunk_start = chunk_end + timedelta(days=1)
    return out


def resample_3m(candles_1m: List[Dict]) -> List[Dict]:
    """Roll 1-min bars into 3-min bars anchored at 09:15 (09:15, 09:18, ...)."""
    buckets: Dict[datetime, Dict] = {}
    for c in sorted(candles_1m, key=lambda x: x["dt"]):
        dt = c["dt"]
        mins = (dt.hour * 60 + dt.minute) - (9 * 60 + 15)
        if mins < 0:
            continue
        key = dt.replace(hour=0, minute=0, second=0) + timedelta(minutes=9 * 60 + 15 + (mins // 3) * 3)
        b = buckets.get(key)
        if b is None:
            buckets[key] = {"timestamp": key.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
                            "open": c["open"], "high": c["high"], "low": c["low"],
                            "close": c["close"], "volume": c["volume"]}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c["volume"]
    return [buckets[k] for k in sorted(buckets)]


def fetch_range_dhan(start: date, end: date, stock_list: List[Dict]) -> Dict[str, List[Dict]]:
    """Fetch the whole range once per symbol (plus a few days of lead-in for prev close)."""
    cache_file = CACHE_DIR / f"dhan_{start}_{end}.json"
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    lead_start = start - timedelta(days=7)
    series = {"__NIFTY__": resample_3m(
        dhan_fetch_1m(DHAN_NIFTY_SECURITY_ID, "IDX_I", "INDEX", lead_start, end))}
    for stock in stock_list:
        symbol, token = stock.get("symbol", ""), stock.get("token", "")
        if not symbol or not token:
            continue
        series[symbol] = resample_3m(dhan_fetch_1m(token, "NSE_EQ", "EQUITY", lead_start, end))
        logger.info(f"Dhan {symbol}: {len(series[symbol])} 3-min bars")
        time_mod.sleep(0.25)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(series, f)
    return series


def slice_day(candles: List[Dict], d: date) -> List[Dict]:
    """Candles for day d plus the previous session present in the data."""
    ds = d.isoformat()
    prev_days = sorted({c["timestamp"][:10] for c in candles if c["timestamp"][:10] < ds})
    if not prev_days:
        return []
    keep = {prev_days[-1], ds}
    today = [c for c in candles if c["timestamp"][:10] in keep]
    return today if any(c["timestamp"][:10] == ds for c in today) else []


def fetch_day_csv(data_dir: Path, date_str: str) -> Optional[Dict]:
    path = data_dir / f"{date_str}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def trade_net_pnl(calc: TransactionCostCalculator, trade: Dict, capital_per_trade: float) -> Dict:
    entry, exit_ = trade["entry_price"], trade["exit_price"]
    qty = int(capital_per_trade // entry) if entry > 0 else 0
    if qty <= 0:
        return {"qty": 0, "gross": 0.0, "costs": 0.0, "net": 0.0}
    if trade["direction"] == "LONG":
        res = calc.calculate_net_pnl(qty, entry, exit_)
    else:
        res = calc.calculate_net_pnl(qty, exit_, entry)
    return {
        "qty": qty,
        "gross": res["gross_pnl"],
        "costs": res["costs"].get("total", 0.0) if isinstance(res["costs"], dict) else 0.0,
        "net": res["net_pnl"],
    }


def load_days(source: str, start: date, end: date, stock_list: List[Dict], data_dir: Path):
    """Yield (date_str, {"nifty": [...], "stocks": {...}}) for each trading day with data."""
    client = None
    dhan_series = None
    if source == "angel":
        from dotenv import load_dotenv
        from src.broker.angel_client import AngelOneClient
        load_dotenv()
        client = AngelOneClient()
        if not client.login():
            logger.error("Angel One login failed")
            sys.exit(1)
    elif source == "dhan":
        import os
        from dotenv import load_dotenv
        load_dotenv()
        token_file = os.getenv("DHAN_TOKEN_FILE")
        if token_file and not os.getenv("DHAN_ACCESS_TOKEN"):
            # JSON with "access_token" and "client_id" keys
            with open(token_file, "r", encoding="utf-8") as f:
                tok = json.load(f)
            os.environ["DHAN_ACCESS_TOKEN"] = tok.get("access_token", "")
            os.environ["DHAN_CLIENT_ID"] = str(tok.get("client_id", ""))
        if not os.getenv("DHAN_ACCESS_TOKEN") or not os.getenv("DHAN_CLIENT_ID"):
            logger.error("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (or DHAN_TOKEN_FILE) in .env")
            sys.exit(1)
        dhan_series = fetch_range_dhan(start, end, stock_list)

    for d in weekdays(start, end):
        ds = d.isoformat()
        if dhan_series is not None:
            day = {
                "nifty": slice_day(dhan_series.get("__NIFTY__", []), d),
                "stocks": {sym: sl for sym, c in dhan_series.items()
                           if sym != "__NIFTY__" and (sl := slice_day(c, d))},
            }
        elif client:
            day = fetch_day_angel(client, ds, stock_list)
        else:
            day = fetch_day_csv(data_dir, ds)
        if not day or not day.get("nifty"):
            logger.info(f"{ds}: no data (holiday?) — skipped")
            continue
        yield ds, day


def main():
    parser = argparse.ArgumentParser(description="Multi-day 3-minute breakout backtest")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--days", type=int, default=30, help="Calendar days back if --start not given")
    parser.add_argument("--source", choices=["angel", "dhan", "csv"], default="angel")
    parser.add_argument("--data-dir", default=str(CACHE_DIR))
    parser.add_argument("--capital-per-trade", type=float, default=100000.0)
    parser.add_argument("--broker", default="angel_one")
    parser.add_argument("--out", default="data/backtest_reports/range_summary.json")
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today() - timedelta(days=1)
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else end - timedelta(days=args.days - 1)

    config = load_config()
    stock_list = load_stock_list()
    engine = BacktestEngine(config)
    calc = TransactionCostCalculator(args.broker)

    days_out = []
    all_trades = []
    for ds, day in load_days(args.source, start, end, stock_list, Path(args.data_dir)):
        result = engine.run(ds, day["nifty"], day["stocks"], stock_list).to_dict()
        day_net = 0.0
        for t in result["trades"]:
            t.update(trade_net_pnl(calc, t, args.capital_per_trade))
            t["date"] = ds
            day_net += t["net"]
            all_trades.append(t)

        days_out.append({
            "date": ds,
            "gap_status": result["nifty_gap"].get("gap_status", "N/A"),
            "gap_percent": result["nifty_gap"].get("gap_percent", 0),
            "trades": len(result["trades"]),
            "no_entry": len(result["no_entry_stocks"]),
            "net_pnl": round(day_net, 2),
        })

    # --- Aggregate ---
    wins = [t for t in all_trades if t["net"] > 0]
    losses = [t for t in all_trades if t["net"] <= 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in all_trades:
        equity += t["net"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    exit_reasons: Dict[str, int] = {}
    for t in all_trades:
        exit_reasons[t["exit_reason"]] = exit_reasons.get(t["exit_reason"], 0) + 1

    summary = {
        "period": f"{start} to {end}",
        "trading_days": len(days_out),
        "total_trades": len(all_trades),
        "winners": len(wins),
        "losers": len(losses),
        "win_rate": round(len(wins) / len(all_trades) * 100, 1) if all_trades else 0,
        "gross_pnl": round(sum(t["gross"] for t in all_trades), 2),
        "total_costs": round(sum(t["costs"] for t in all_trades), 2),
        "net_pnl": round(equity, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0,
        "max_drawdown": round(max_dd, 2),
        "exit_reasons": exit_reasons,
        "capital_per_trade": args.capital_per_trade,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "days": days_out, "trades": all_trades}, f, indent=2, default=str)

    print(f"\n=== 3-Min Breakout Backtest: {summary['period']} ===")
    print(f"{'Date':<12}{'Gap':<10}{'Gap%':>7}{'Trades':>8}{'Net P&L':>12}")
    for dd in days_out:
        print(f"{dd['date']:<12}{dd['gap_status']:<10}{dd['gap_percent']:>7}{dd['trades']:>8}{dd['net_pnl']:>12,.2f}")
    print("\nTrades:")
    for t in all_trades:
        print(f"  {t['date']} {t['symbol']:<14}{t['direction']:<6} in {t['entry_price']:>9.2f} "
              f"out {t['exit_price']:>9.2f} {t['exit_reason']:<15} net {t['net']:>10,.2f}")
    print("\nSummary:")
    for k, v in summary.items():
        print(f"  {k:<18}{v}")
    print(f"\nFull report: {out}")


if __name__ == "__main__":
    main()
