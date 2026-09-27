"""
Collect full 1-min bars (09:15-15:14) for one stock-day through the Dhan MCP tool,
5 bars per call. Dhan window rule: bars with from < start < to, so bars a..a+4 are
fetched with fromDate=a-1min, toDate=a+5min.

  python3 bars.py init <date> <symbol> <securityId>
  python3 bars.py put  <date> <symbol> <DataPoints> <SumO> <SumH> <SumL> <SumC> "<o,h,l,c;o,h,l,c;...>"
  python3 bars.py put  <date> <symbol> EMPTY
Prints NEXT {payload}, DONE, or ERROR (then re-query the same payload).
"""
import json
import sys
from pathlib import Path

DIR = Path(__file__).parent / "bars_state"
DIR.mkdir(exist_ok=True)
FIRST, LAST = 9 * 60 + 15, 15 * 60 + 14


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}:00"


def path(d, s):
    return DIR / f"{d}_{s}.json"


def save(st):
    path(st["date"], st["symbol"]).write_text(json.dumps(st))


def nxt(st):
    if st["mode"] == "window":
        a = st["a"]
        if a > LAST:
            st["done"] = True
            save(st)
            print(f"DONE {st['date']} {st['symbol']} bars={len(st['bars'])} missing={len(st['missing'])}")
            return
        f, t = a - 1, min(a + 4, LAST) + 1
    else:
        m = st["pending"][0]
        f, t = m - 1, m + 1
    save(st)
    print("NEXT " + json.dumps({"securityId": st["sid"], "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
                                "interval": 1, "oi": False,
                                "fromDate": f"{st['date']} {hm(f)}", "toDate": f"{st['date']} {hm(t)}"}))


def main():
    cmd, d, s = sys.argv[1:4]
    if cmd == "init":
        if path(d, s).exists():
            st = json.loads(path(d, s).read_text())
            if st.get("done"):
                print("ALREADY DONE")
                return
            return nxt(st)
        st = {"date": d, "symbol": s, "sid": sys.argv[4], "mode": "window", "a": FIRST,
              "bars": {}, "missing": [], "pending": []}
        return nxt(st)

    st = json.loads(path(d, s).read_text())
    if st.get("done"):
        print("ALREADY DONE")
        return
    empty = sys.argv[4] == "EMPTY"
    if st["mode"] == "window":
        a = st["a"]
        mins = list(range(a, min(a + 4, LAST) + 1))
        if not empty:
            n = int(sys.argv[4])
            so, sh, sl, sc = map(float, sys.argv[5:9])
            cs = [list(map(float, c.split(","))) for c in sys.argv[9].strip(";").split(";") if c]
            if len(cs) != n:
                print(f"ERROR candle count {len(cs)} != DataPoints {n}; re-query")
                return
            bad = [c for c in cs if not (c[1] >= max(c[0], c[3]) - 1e-9 and c[2] <= min(c[0], c[3]) + 1e-9)]
            if bad or abs(cs[0][0] - so) > 1e-6 or abs(cs[-1][3] - sc) > 1e-6 \
                    or abs(max(c[1] for c in cs) - sh) > 1e-6 or abs(min(c[2] for c in cs) - sl) > 1e-6:
                print("ERROR candles don't match summary (copy error?); re-query")
                return
            if n == len(mins):
                for m, c in zip(mins, cs):
                    st["bars"][str(m)] = c
                st["a"] = a + 5
                return nxt(st)
        # some bars missing in this window: fetch minute by minute
        st["mode"] = "single"
        st["pending"] = mins
        return nxt(st)

    m = st["pending"].pop(0)
    if empty:
        st["missing"].append(m)
    else:
        n = int(sys.argv[4])
        cs = [list(map(float, c.split(","))) for c in sys.argv[9].strip(";").split(";") if c]
        if n != 1 or len(cs) != 1:
            st["pending"].insert(0, m)
            print("ERROR expected exactly 1 candle; re-query")
            return
        st["bars"][str(m)] = cs[0]
    if not st["pending"]:
        st["mode"] = "window"
        st["a"] = st["a"] + 5
    return nxt(st)


if __name__ == "__main__":
    main()
