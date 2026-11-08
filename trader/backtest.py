"""Usage: python3 trader/backtest.py  (results in trader/state/bt_results.json; delete
trader/state/bt_cache.json to fetch fresh bars). Not financial advice: a replay is an estimate.

Replay the autopilot's opening-range-breakout rules over Yahoo's last ~60 sessions of
5-minute bars, for several stop/target settings, with the live bot's sizing and day limits.

Conservative choices: when one bar touches both the stop and the target, the stop wins;
a gap through the stop fills at the bar's open; 1 cent of slippage a share on entry and exit.
"""
import datetime
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap  # noqa: E402

scan = ap.scan
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
CACHE = os.path.join(HERE, "bt_cache.json")
OPEN = datetime.time(9, 30)


def load():
    os.makedirs(HERE, exist_ok=True)
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    syms = ap.universe()
    data = {}
    for i, s in enumerate(syms):
        try:
            data[s] = ap.chart(s, "60d")
        except Exception as e:  # noqa: BLE001
            print("skip", s, e, file=sys.stderr)
        time.sleep(0.3)
    json.dump(data, open(CACHE, "w"))
    return data


def days_of(data):
    ds = set()
    for s in data.values():
        for t in s["t"][:: 50]:
            ds.add(scan.ts_to_eastern(t).date())
    return sorted(d for d in ds if scan.trading_day(d)[0])


def by_day(data, days):
    """{day: {sym: bars}} built once: one pass over each symbol's bars."""
    out = {d: {} for d in days}
    for sym, s in data.items():
        o = s.get("o")
        rows = {}
        for i, ts in enumerate(s["t"]):
            c = s["c"][i]
            if c is None:
                continue
            et = scan.ts_to_eastern(ts)
            d = et.date()
            if d not in out or et.minute % 5 or et.second:
                continue
            sess = scan.session(d)
            if et.time() < OPEN or et.time() >= sess[1]:
                continue
            h, l = s["h"][i], s["l"][i]
            rows.setdefault(d, {})[et] = {"start": et, "c": float(c), "h": None if h is None else float(h),
                                          "l": None if l is None else float(l),
                                          "o": None if not o or o[i] is None else float(o[i]),
                                          "v": (s["v"][i] or 0) if s.get("v") else 0}
        for d, r in rows.items():
            out[d][sym] = [r[k] for k in sorted(r)]
    return out


def candidates(daybars, day, stop_rule, target_r, last_entry=datetime.time(15, 30)):
    """Every first opening-range breakout on `day` the live bot could take."""
    sess = scan.session(day)
    close_dt = datetime.datetime.combine(day, sess[1])
    flat_dt = close_dt - datetime.timedelta(minutes=5)
    out = []
    for sym, bars in daybars.items():
        if len(bars) < 10 or bars[0]["start"].time() != OPEN:
            continue
        if any(b["h"] is None or b["l"] is None for b in bars):
            continue
        oh = max(b["h"] for b in bars[:3])
        ol = min(b["l"] for b in bars[:3])
        for k in range(3, len(bars)):
            b = bars[k]
            if b["c"] > oh:
                break
        else:
            continue
        end = b["start"] + datetime.timedelta(minutes=5)          # bar close = signal time
        if end.time() < datetime.time(10, 0) or end.time() > last_entry:
            continue  # the live bot only takes a breakout that is fresh at 10:00 or later
        entry = round(b["c"] + 0.01, 2)                             # slippage
        if not 5 <= entry <= 150:
            continue
        if stop_rule == "or_mid":
            stop = math.floor((oh + ol) / 2 * 100) / 100
        elif stop_rule == "or_low":
            stop = math.floor(ol * 100) / 100
        else:  # "atr-ish": half the opening range under the entry
            stop = math.floor((entry - (oh - ol) / 2) * 100) / 100
        risk = entry - stop
        if risk <= 0 or risk < entry * 0.0005:
            continue
        target = round(entry + target_r * risk, 2) if target_r else None
        # walk forward
        exit_px, how, exit_t = None, None, None
        for j in range(k + 1, len(bars)):
            x = bars[j]
            if x["start"] >= flat_dt:
                break
            if x["l"] <= stop:
                o = x["c"] if x.get("o") is None else x["o"]
                exit_px, how = min(stop, o if o is not None else stop), "stop"
            elif target and x["h"] >= target:
                exit_px, how = target, "target"
            if how:
                exit_t = x["start"] + datetime.timedelta(minutes=5)
                break
        if not how:
            last = [x for x in bars if x["start"] < flat_dt][-1]
            exit_px, how, exit_t = last["c"], "close", flat_dt
        out.append({"sym": sym, "t": end, "exit_t": exit_t, "entry": entry, "stop": stop,
                    "exit": round(exit_px - 0.01, 2), "how": how, "risk": risk,
                    "dv": entry * sum(z["v"] for z in bars[:k + 1])})
    out.sort(key=lambda c: (c["t"], -c["dv"]))
    return out


def run_day(cands, risk_usd=10, budget=2000, max_trades=20, daily=100):
    cash_cap = budget * 0.9
    opened, used, trades = [], 0.0, []
    seen_t = set()
    for c in cands:
        if len(trades) >= max_trades:
            break
        if c["t"] in seen_t:      # live bot: one new entry per 5-minute cycle
            continue
        busy = sum(o["cost"] for o in opened if o["exit_t"] > c["t"])
        qty = min(int(risk_usd // c["risk"]), int(cash_cap // c["entry"]),
                  int((cash_cap - busy) // c["entry"]))
        if qty <= 0:
            continue
        ml = qty * c["risk"]
        if used + ml > daily + 0.01:
            continue
        used += ml
        seen_t.add(c["t"])
        pnl = qty * (c["exit"] - c["entry"])
        opened.append({"cost": qty * c["entry"], "exit_t": c["exit_t"]})
        trades.append(dict(c, qty=qty, pnl=round(pnl, 2)))
    return trades


def summary(name, all_trades, days):
    pnl = [t["pnl"] for t in all_trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    by_day = {}
    for t in all_trades:
        by_day.setdefault(t["t"].date(), 0.0)
        by_day[t["t"].date()] += t["pnl"]
    eq, peak, dd = 0.0, 0.0, 0.0
    for d in days:
        eq += by_day.get(d, 0.0)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    half = len(days) // 2
    first = sum(by_day.get(d, 0) for d in days[:half])
    second = sum(by_day.get(d, 0) for d in days[half:])
    hows = {h: sum(1 for t in all_trades if t["how"] == h) for h in ("target", "stop", "close")}
    return {"name": name, "trades": len(pnl), "total": round(sum(pnl), 2),
            "win_rate": round(100 * len(wins) / max(1, len(pnl)), 1),
            "avg_win": round(sum(wins) / max(1, len(wins)), 2),
            "avg_loss": round(sum(losses) / max(1, len(losses)), 2),
            "green_days": sum(1 for d in days if by_day.get(d, 0) > 0), "days": len(days),
            "max_drawdown": round(dd, 2), "first_half": round(first, 2), "second_half": round(second, 2),
            "per_day": round(sum(pnl) / len(days), 2), "exits": hows}


def main():
    data = load()
    days = days_of(data)
    print("symbols", len(data), "days", len(days), days[0], days[-1], file=sys.stderr)
    variants = [("NOW: stop mid, goal 2R", "or_mid", 2), ("stop mid, goal 1.5R", "or_mid", 1.5),
                ("stop mid, goal 1R", "or_mid", 1), ("stop mid, goal 3R", "or_mid", 3),
                ("stop mid, no goal (hold to 3:55)", "or_mid", None),
                ("stop low, goal 1.5R", "or_low", 1.5), ("stop low, goal 2R", "or_low", 2),
                ("stop low, goal 1R", "or_low", 1)]
    DB = by_day(data, days)
    res = []
    for name, sr, tr in variants:
        allt = []
        for d in days:
            allt += run_day(candidates(DB[d], d, sr, tr))
        res.append(summary(name, allt, days))
        print(json.dumps(res[-1]), file=sys.stderr)
    json.dump(res, open(os.path.join(HERE, "bt_results.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
