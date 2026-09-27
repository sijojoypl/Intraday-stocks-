"""Build engine-ready day files (scripts/backtest_range.py --source csv format) from
daily candles + collected 1-min bars of the picked stocks."""
import json
import sys
from pathlib import Path

S = Path(__file__).parent
OUT = S / "days"
OUT.mkdir(exist_ok=True)
FIRST = 9 * 60 + 15

daily = json.load(open(S / "stocks_daily_all.json"))
nifty = json.load(open(S / "nifty_daily.json"))
sel = json.load(open(S / "selection_all.json"))
dates = sorted(nifty)


def ts(d, m):
    return f"{d}T{m // 60:02d}:{m % 60:02d}:00+05:30"


def stub(prev_d, d, prev_close, open_):
    return [
        {"timestamp": ts(prev_d, 15 * 60 + 27), "open": prev_close, "high": prev_close, "low": prev_close,
         "close": prev_close, "volume": 0},
        {"timestamp": ts(d, FIRST), "open": open_, "high": open_, "low": open_, "close": open_, "volume": 0},
    ]


def three_min(d, bars):
    groups = {}
    for m, (o, h, l, c) in sorted((int(k), v) for k, v in bars.items()):
        g = FIRST + 3 * ((m - FIRST) // 3)
        b = groups.get(g)
        if b is None:
            groups[g] = {"timestamp": ts(d, g), "open": o, "high": h, "low": l, "close": c, "volume": 0}
        else:
            b["high"] = max(b["high"], h)
            b["low"] = min(b["low"], l)
            b["close"] = c
    return [groups[g] for g in sorted(groups)]


built, incomplete = 0, []
for d, _status, _gap, picks in sel:
    prev_d = dates[dates.index(d) - 1]
    day = {"nifty": stub(prev_d, d, nifty[prev_d][3], nifty[d][0]), "stocks": {}}
    for sym, v in daily.items():
        if d in v and prev_d in v:
            day["stocks"][sym] = stub(prev_d, d, v[prev_d][3], v[d][0])
    ok = True
    for sym, _dir, _g in picks:
        p = S / "bars_state" / f"{d}_{sym}.json"
        st = json.loads(p.read_text()) if p.exists() else {}
        if not st.get("done"):
            ok = False
            continue
        day["stocks"][sym] = day["stocks"][sym][:1] + three_min(d, st["bars"])
    if ok:
        (OUT / f"{d}.json").write_text(json.dumps(day))
        built += 1
    else:
        incomplete.append(d)
print(f"built {built} day files; incomplete: {incomplete}")
