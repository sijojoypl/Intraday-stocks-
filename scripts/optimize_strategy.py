"""
Parameter sweep with an out-of-sample check for the 3-minute breakout strategy.

Days are split in time order: settings are ranked on the first part (train) and
then scored on the later part (test) that the ranking never saw. Only the test
numbers say anything about whether a change is likely to hold up.

Usage:
    python scripts/optimize_strategy.py --start 2026-03-01 --end 2026-09-25 --source dhan
    python scripts/optimize_strategy.py --days 180 --source csv --data-dir data/backtest_cache
    python scripts/optimize_strategy.py --days 180 --source dhan --quick
"""

import argparse
import itertools
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runpy

from loguru import logger

from src.backtest.backtest_engine import BacktestEngine

_runner = runpy.run_path(str(Path(__file__).resolve().parent / "backtest_range.py"), run_name="backtest_range")
load_days = _runner["load_days"]
load_config = _runner["load_config"]
load_stock_list = _runner["load_stock_list"]
trade_net_pnl = _runner["trade_net_pnl"]
TransactionCostCalculator = _runner["TransactionCostCalculator"]

FULL_GRID = {
    "direction_mode": ["fade", "follow"],
    "sides": ["both", "long", "short"],
    "entry_cutoff": ["15:00", "12:00", "10:30"],
    "max_ref_range_percent": [100.0, 1.5, 1.0],
    "target": ["pct1.0", "pct1.5", "rr1.5", "rr2.0"],
    "breakeven_at_percent": [0.0, 0.5],
    "trail_percent": [0.0, 0.5],
    "nifty_confirm": [False, True],
}
QUICK_GRID = {
    "direction_mode": ["fade", "follow"],
    "sides": ["both", "long", "short"],
    "entry_cutoff": ["15:00", "11:00"],
    "max_ref_range_percent": [100.0, 1.0],
    "target": ["pct1.0", "rr2.0"],
    "breakeven_at_percent": [0.0],
    "trail_percent": [0.0, 0.5],
    "nifty_confirm": [False, True],
}
BASELINE = {"direction_mode": "fade", "sides": "both", "entry_cutoff": "15:00",
            "max_ref_range_percent": 100.0, "target": "pct1.0", "breakeven_at_percent": 0.0,
            "trail_percent": 0.0, "nifty_confirm": False}


def to_params(base_params: Dict, combo: Dict) -> Dict:
    p = dict(base_params)
    p.update({
        "direction_mode": combo["direction_mode"],
        "allow_long": combo["sides"] in ("both", "long"),
        "allow_short": combo["sides"] in ("both", "short"),
        "entry_cutoff": combo["entry_cutoff"],
        "max_ref_range_percent": combo["max_ref_range_percent"],
        "breakeven_at_percent": combo["breakeven_at_percent"],
        "trail_percent": combo["trail_percent"],
        "nifty_confirm": combo["nifty_confirm"],
        "save_reports": False,
    })
    t = combo["target"]
    if t.startswith("pct"):
        p["target_percent"] = float(t[3:])
        p["target_rr"] = 0.0
    else:
        p["target_rr"] = float(t[2:])
    return p


def prepare_days(days, engine: BacktestEngine, stock_list: List[Dict]) -> List[Dict]:
    """Per-day values that don't depend on the swept settings."""
    prepared = []
    for ds, day in days:
        gap = engine._classify_nifty_gap(day["nifty"])
        if not gap.get("valid"):
            continue
        prepared.append({
            "date": ds,
            "gap": gap,
            "ranked": engine._rank_stocks_by_gap(day["stocks"], stock_list),
            "stocks": day["stocks"],
            "nifty_by_ts": {c.get("timestamp", ""): c
                            for c in engine._parse_candles_for_test_date(day["nifty"])},
        })
    return prepared


def simulate(engine: BacktestEngine, prepared: List[Dict], calc, capital: float) -> List[Dict]:
    trades = []
    for day in prepared:
        for pick in engine._select_stocks(day["ranked"], day["gap"]):
            candles = day["stocks"].get(pick["symbol"], [])
            if not candles:
                continue
            trade, _ = engine._simulate_stock(pick["symbol"], pick["direction"], candles, day["nifty_by_ts"])
            if trade:
                t = trade.to_dict()
                t.update(trade_net_pnl(calc, t, capital))
                t["date"] = day["date"]
                trades.append(t)
    return trades


def stats(trades: List[Dict], n_days: int) -> Dict:
    if not trades:
        return {"trades": 0, "net": 0.0, "win_rate": 0.0, "pf": None, "max_dd": 0.0, "per_day": 0.0}
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [-t["net"] for t in trades if t["net"] <= 0]
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "trades": len(trades),
        "net": round(eq, 2),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "pf": round(sum(wins) / sum(losses), 2) if losses and sum(losses) > 0 else None,
        "max_dd": round(dd, 2),
        "per_day": round(eq / max(n_days, 1), 2),
    }


def fmt(combo: Dict) -> str:
    return (f"{combo['direction_mode']}/{combo['sides']} cutoff {combo['entry_cutoff']} "
            f"ref<={combo['max_ref_range_percent']:g}% {combo['target']} "
            f"be{combo['breakeven_at_percent']:g} trail{combo['trail_percent']:g} "
            f"nifty{'Y' if combo['nifty_confirm'] else 'N'}")


def main():
    parser = argparse.ArgumentParser(description="Sweep strategy settings with a train/test split")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--days", type=int, default=180, help="Calendar days back if --start not given")
    parser.add_argument("--source", choices=["angel", "dhan", "csv"], default="dhan")
    parser.add_argument("--data-dir", default="data/backtest_cache")
    parser.add_argument("--capital-per-trade", type=float, default=100000.0)
    parser.add_argument("--broker", default="angel_one")
    parser.add_argument("--train-fraction", type=float, default=0.67)
    parser.add_argument("--min-trades", type=int, default=20, help="Min train trades for a setting to rank")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--quick", action="store_true", help="Smaller grid")
    parser.add_argument("--out", default="data/backtest_reports/optimize_summary.json")
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today() - timedelta(days=1)
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else end - timedelta(days=args.days - 1)

    config = load_config()
    base_params = config.get("strategies", {}).get("three_minute", {}).get("params", {})
    stock_list = load_stock_list()
    calc = TransactionCostCalculator(args.broker)

    days = list(load_days(args.source, start, end, stock_list, Path(args.data_dir)))
    prepared = prepare_days(days, BacktestEngine(config), stock_list)
    if len(prepared) < 10:
        logger.error(f"Only {len(prepared)} usable days — need more data for a meaningful split")
        sys.exit(1)
    split = int(len(prepared) * args.train_fraction)
    train, test = prepared[:split], prepared[split:]
    logger.info(f"{len(prepared)} days: train {train[0]['date']}..{train[-1]['date']} ({len(train)}), "
                f"test {test[0]['date']}..{test[-1]['date']} ({len(test)})")

    grid = QUICK_GRID if args.quick else FULL_GRID
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*grid.values())]
    if BASELINE not in combos:
        combos.append(BASELINE)
    logger.info(f"Testing {len(combos)} settings")

    results = []
    for i, combo in enumerate(combos, 1):
        engine = BacktestEngine({"strategies": {"three_minute": {"params": to_params(base_params, combo)}}})
        tr = simulate(engine, train, calc, args.capital_per_trade)
        te = simulate(engine, test, calc, args.capital_per_trade)
        results.append({"combo": combo, "train": stats(tr, len(train)), "test": stats(te, len(test))})
        if i % 200 == 0:
            logger.info(f"  {i}/{len(combos)}")

    baseline = next(r for r in results if r["combo"] == BASELINE)
    eligible = [r for r in results if r["train"]["trades"] >= args.min_trades]
    ranked = sorted(eligible, key=lambda r: r["train"]["net"], reverse=True)
    positive_train = [r for r in eligible if r["train"]["net"] > 0]
    both_positive = [r for r in positive_train if r["test"]["net"] > 0]

    def line(r):
        a, b = r["train"], r["test"]
        return (f"{fmt(r['combo']):<70} | train {a['trades']:>3}t {a['win_rate']:>5}% ₹{a['net']:>10,.0f} "
                f"PF {a['pf']} | test {b['trades']:>3}t {b['win_rate']:>5}% ₹{b['net']:>10,.0f} PF {b['pf']}")

    print(f"\nTrain {train[0]['date']}..{train[-1]['date']} ({len(train)} days) | "
          f"Test {test[0]['date']}..{test[-1]['date']} ({len(test)} days) | ₹{args.capital_per_trade:,.0f}/trade\n")
    print("BASELINE (current live settings)")
    print(line(baseline))
    print(f"\nTOP {args.top} BY TRAIN NET (min {args.min_trades} train trades) — judge them by the TEST column")
    for r in ranked[:args.top]:
        print(line(r))
    print(f"\n{len(positive_train)} of {len(eligible)} eligible settings made money on train; "
          f"{len(both_positive)} of those also made money on test.")
    if ranked:
        best = ranked[0]
        verdict = "held up" if best["test"]["net"] > 0 else "did NOT hold up"
        print(f"Best-on-train setting {verdict} out of sample: test net ₹{best['test']['net']:,.0f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"train_days": [d["date"] for d in train], "test_days": [d["date"] for d in test],
                   "baseline": baseline, "ranked": ranked}, f, indent=2)
    print(f"\nFull results: {out}")


if __name__ == "__main__":
    main()
