#!/usr/bin/env python3
"""Autopilot for the Alpaca PAPER account: opening-range breakouts, fully automatic.

The user asked for this in chat: on the paper account, enter and exit by itself, with no
"yes" for each order, up to max_trades_per_day trades, losing at most max_daily_loss_usd a
day, and a profit-and-loss report at the end of the day.

It runs the pack's own code for each step:
  trader-scanner  orb_hit        first 5-minute bar to CLOSE above the 15-minute opening range
  trader-exit     compute        stop at the opening-range midpoint, target at 2R
  trader-budget   size_trade     whole shares inside max loss, position cap, max order, cash
  trader-risk     check          PASS or BLOCK; only a PASS is sent
Then it sends ONE bracket order (market entry, stop-loss, take-profit) to Alpaca paper, and at
flat_by (15:55 New York) cancels every open order and closes every position.

Safety, never relaxed:
  * paper only: the trading URL is fixed to paper-api.alpaca.markets and "paper" must be true
  * a trade is sent only after trader-risk prints PASS for that exact order
  * no new entries once today's equity change reaches -max_daily_loss_usd
  * one trade per symbol per day, one new entry per 5-minute cycle
  * the keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY and are never printed

Usage:
  python3 trader/autopilot.py run [--max-minutes 115]   live (paper) session
  python3 trader/autopilot.py run --dry-run --replay 2026-11-03 --at 10:30   one replayed cycle, no orders
  python3 trader/autopilot.py flatten                   cancel all orders, close all positions
  python3 trader/autopilot.py report [--date YYYY-MM-DD]

Exit codes for `run`: 0 the day is done (flat and reported, or no session today),
10 --max-minutes ran out (run it again to carry on), 2 a fatal error.
Not financial advice. Paper trading results do not predict real results.
"""
import argparse
import csv
import datetime
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(HERE, "state")
REPORTS = os.path.join(HERE, "reports")
LOG = os.path.join(STATE, "autopilot.log")
JOURNAL = os.path.join(HERE, "journal.csv")
CONFIG = os.path.join(HERE, "config.json")

TRADE_URL = "https://paper-api.alpaca.markets/v2"   # paper only, never change this line
DATA_URL = "https://data.alpaca.markets"
MIN_PRICE, MAX_PRICE = 5.0, 150.0                   # budget-friendly, no penny stocks
UNIVERSE_TOP = 100
OR_MINUTES = 15
NEWS_WORDS = ("earnings", "eps", "quarterly results", "q1 ", "q2 ", "q3 ", "q4 ", "guidance",
              "halt", "offering", "fda", "merger", "to acquire", "bankrupt")
FALLBACK = ("AAPL AMD AMZN BAC C CCL CSCO DAL F GM GOOGL HOOD INTC JPM KO MARA META MSFT MU NIO "
            "NKE NVDA PFE PLTR PYPL RIVN SHOP SNAP SOFI T TSLA UBER WBD WFC XOM").split()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = _load("ap_scan", "trader-scanner/scan.py")
exitp = _load("ap_exit", "trader-exit/exit.py")
size = _load("ap_size", "trader-budget/size.py")
risk = _load("ap_risk", "trader-risk/check.py")


# ---------------------------------------------------------------- clock and logging

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def sydney(utc):
    """Sydney time without zoneinfo: AEDT (UTC+11) from the first Sunday of October to the
    first Sunday of April, AEST (UTC+10) otherwise. The change happens at 2 or 3 AM local."""
    def first_sunday(y, m):
        d = datetime.date(y, m, 1)
        return d + datetime.timedelta(days=(6 - d.weekday()) % 7)
    y = utc.year
    start = datetime.datetime.combine(first_sunday(y, 10), datetime.time(16)) - datetime.timedelta(days=1)
    end = datetime.datetime.combine(first_sunday(y, 4), datetime.time(16)) - datetime.timedelta(days=1)
    off = 11 if (utc >= start or utc < end) else 10
    return utc + datetime.timedelta(hours=off)


def stamp(utc=None):
    utc = utc or utcnow()
    return "%s NY | %s Sydney" % (scan.to_eastern(utc).strftime("%H:%M:%S"),
                                  sydney(utc).strftime("%a %H:%M"))


def log(msg):
    line = "[%s] %s" % (stamp(), msg)
    print(line, flush=True)
    os.makedirs(STATE, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (utcnow().strftime("%Y-%m-%d"), line))


# ---------------------------------------------------------------- Alpaca REST

class ApiError(Exception):
    pass


def _keys():
    k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not k or not s:
        raise ApiError("ALPACA_API_KEY and ALPACA_SECRET_KEY are not set")
    return k, s


def api(method, url, body=None, timeout=20):
    if url.startswith("https://api.alpaca.markets"):
        raise ApiError("refusing a LIVE endpoint: this autopilot is paper only")
    k, s = _keys()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s, "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:300]
        raise ApiError("%s %s -> HTTP %d %s" % (method, url.split("?")[0].replace(TRADE_URL, ""), e.code, msg))
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("%s %s -> %s" % (method, url.split("?")[0], getattr(e, "reason", e)))


def trade(method, path, body=None):
    return api(method, TRADE_URL + path, body)


def today_orders(day):
    start = scan.eastern_to_utc(datetime.datetime.combine(day, datetime.time(0, 0)))
    q = urllib.parse.urlencode({"status": "all", "after": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "limit": 500, "nested": "true", "direction": "asc"})
    return trade("GET", "/orders?" + q) or []


# ---------------------------------------------------------------- market data

LEVERAGED = re.compile(r"\b(2x|3x|-2x|-3x|ultra|ultrapro|ultrashort|bull|bear|leveraged|inverse|short)\b", re.I)
_KIND = {}


def leveraged(sym):
    """True for leveraged or inverse funds (they move 2 to 3 times as fast), by Alpaca's asset name."""
    if sym not in _KIND:
        try:
            name = trade("GET", "/assets/%s" % sym).get("name") or ""
        except ApiError:
            name = ""
        _KIND[sym] = bool(LEVERAGED.search(name))
    return _KIND[sym]


def universe():
    syms = []
    for by in ("volume", "trades"):
        try:
            doc = api("GET", "%s/v1beta1/screener/stocks/most-actives?by=%s&top=%d" % (DATA_URL, by, UNIVERSE_TOP))
            syms += [m["symbol"] for m in doc.get("most_actives", [])]
        except ApiError as e:
            log("screener failed (%s); using the fallback list" % e)
    out = []
    for s in syms + FALLBACK:
        if s not in out and scan.TICKER.match(s) and not leveraged(s):
            out.append(s)
    return out


def chart(sym, rng="1d"):
    return scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, rng, "5m")), sym)


def news_flag(sym):
    """A headline in the last 20 hours that says earnings, a halt, an offering or a deal.
    Returns the reason to skip the stock, or None. A failed news request is a reason to skip."""
    since = (utcnow() - datetime.timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        doc = api("GET", "%s/v1beta1/news?symbols=%s&start=%s&limit=20" % (DATA_URL, sym, since))
    except ApiError as e:
        return "news check failed (%s), skipped to be safe" % e  # fail closed: no news, no trade
    if not isinstance(doc, dict) or not isinstance(doc.get("news"), list):
        return "news check returned nothing readable, skipped to be safe"
    for n in doc.get("news", []):
        if len(n.get("symbols") or []) > 3:
            continue  # a roundup or sector story, not news about this stock
        h = " %s " % (n.get("headline") or "").lower()
        for w in NEWS_WORDS:
            if w in h:
                return n.get("headline")
    return None


def aud_rate():
    try:
        doc = json.loads(scan.yahoo_get("/v8/finance/chart/AUDUSD=X?range=1d&interval=1d"))
        return float(doc["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


# ---------------------------------------------------------------- today's book, from Alpaca itself

ACTIVE = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "filled", "held",
          "calculated", "done_for_day"}


def _stop_of(o):
    for leg in o.get("legs") or []:
        if leg.get("stop_price"):
            return float(leg["stop_price"])
    return None


def book(day):
    """Entries placed today (bracket buys that were not rejected or canceled unfilled),
    in the record shape trader-risk reads."""
    recs, symbols = [], set()
    for o in today_orders(day):
        if o.get("side") != "buy" or o.get("order_class") != "bracket":
            continue
        filled = float(o.get("filled_qty") or 0)
        if o.get("status") not in ACTIVE and filled <= 0:
            continue
        sym = o["symbol"]
        symbols.add(sym)
        qty = filled or float(o.get("qty") or 0)
        px = float(o.get("filled_avg_price") or 0) or None
        stop = _stop_of(o)
        ml = round(qty * (px - stop), 2) if (px and stop) else None
        if ml is None:
            ml = _planned_loss(o.get("client_order_id"))
        recs.append({"status": "placed", "date": day.isoformat(), "symbol": sym, "side": "buy",
                     "kind": "entry", "max_loss": ml, "qty": qty})
    return {"orders": recs}, symbols


def _planned_loss(cid):
    for row in read_trades():
        if row.get("client_order_id") == cid:
            return row.get("max_loss")
    return None


TRADES = os.path.join(STATE, "autopilot_trades.json")


def read_trades():
    try:
        with open(TRADES, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def save_trade(row):
    rows = read_trades()
    rows.append(row)
    os.makedirs(STATE, exist_ok=True)
    with open(TRADES, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)


def portfolio(cfg):
    """The budget-sized account: cash = budget minus what is tied up in open positions
    and in open entry orders. The other paper money is ignored on purpose."""
    budget = float(cfg["budget"])
    positions = trade("GET", "/positions") or []
    used, rows, prices = 0.0, [], {}
    for p in positions:
        mv = abs(float(p.get("market_value") or 0))
        used += mv
        rows.append({"symbol": p["symbol"], "shares": float(p["qty"]),
                     "lots": [{"shares": float(p["qty"]), "cost": abs(float(p.get("cost_basis") or mv))}]})
        prices[p["symbol"]] = float(p.get("current_price") or 0)
    planned = {r.get("client_order_id"): r.get("entry_plan") for r in read_trades()}
    for o in trade("GET", "/orders?status=open&nested=true&limit=200") or []:
        if o.get("side") != "buy":
            continue
        left = float(o.get("qty") or 0) - float(o.get("filled_qty") or 0)
        if left <= 0:
            continue
        # a pending market buy has no limit price: count it at the price it was planned at
        # (the breakout hit price), or the position's price, or, unknown, as a full max order
        px = float(o.get("limit_price") or planned.get(o.get("client_order_id")) or prices.get(o["symbol"]) or 0)
        used += left * px if px > 0 else float(cfg.get("max_order_usd") or cfg["budget"])
    return {"cash": round(budget - used, 2), "positions": rows}, prices, positions


# ---------------------------------------------------------------- one cycle

def find_breakouts(symbols, day, cutoff, rng="1d"):
    """Fresh opening-range breakouts: the first close above the range happened on one of
    the last two completed bars, and the price is in the budget band."""
    n_range = OR_MINUTES // scan.BAR_MIN
    hits = []
    for sym in symbols:
        try:
            s = chart(sym, rng)
        except scan.ScanError:
            continue
        time.sleep(0.25)
        bars = scan.session_bars(s, day, cutoff)
        if not scan.usable(bars, day, cutoff, n_range) or len(bars) <= n_range:
            continue
        last = bars[-1]["c"]
        if not MIN_PRICE <= last <= MAX_PRICE:
            continue
        h = scan.orb_hit(bars, n_range)
        if not h or h["bar_index"] < len(bars) - 2:
            continue
        meta = s.get("meta") or {}
        price = float(meta.get("regularMarketPrice") or last) if rng == "1d" else last
        vol = sum(b["v"] for b in bars)
        hits.append(dict(h, symbol=sym, price=round(price, 2), dollar_volume=price * vol))
    hits.sort(key=lambda x: -x["dollar_volume"])
    return hits


def plan_for(hit, day, cfg):
    p = exitp.compute(hit["price"], "or_mid", None, "r", exitp.D("2"), or_high=hit["or_high"], or_low=hit["or_low"])
    return {"symbol": hit["symbol"], "side": "buy", "setup": "orb", "date": day.isoformat(),
            "entry_time": hit["at"], "entry": float(p["entry"]), "stop": float(p["stop"]),
            "target": float(p["target"]), "stop_label": p["stop_label"], "target_label": p["target_label"],
            "flat_by": cfg.get("flat_by", "15:55")}


def rest_body(order, cid):
    return {"symbol": order["symbol"], "qty": order["qty"], "side": "buy", "type": "market",
            "time_in_force": "day", "order_class": "bracket", "client_order_id": cid,
            "take_profit": {"limit_price": order["take_profit_limit_price"]},
            "stop_loss": {"stop_price": order["stop_loss_stop_price"]}}


def cycle(cfg, day, now_utc, symbols, dry=False, rng="1d"):
    """Look for one entry. Returns a short status string."""
    now_et = scan.to_eastern(now_utc)
    cutoff = now_et.replace(second=0, microsecond=0)
    if dry:
        state, traded = {"orders": []}, set()
        port, prices = {"cash": float(cfg["budget"]), "positions": []}, {}
    else:
        acct = trade("GET", "/account")
        change = float(acct["equity"]) - float(acct["last_equity"])
        if change <= -float(cfg["max_daily_loss_usd"]):
            return "halted: today's change is %s, at the %s daily loss limit" % (
                size.money(change), size.money(float(cfg["max_daily_loss_usd"])))
        state, traded = book(day)
        traded |= {r["symbol"] for r in read_trades() if r.get("date") == day.isoformat()}
        if len(state["orders"]) >= int(cfg["max_trades_per_day"]):
            return "done: %d trades placed today, the daily limit" % len(state["orders"])
        port, prices, _ = portfolio(cfg)
    hits = find_breakouts([s for s in symbols if s not in traded], day, cutoff, rng)
    if not hits:
        return "no fresh breakout this bar (%d symbols)" % len(symbols)
    for hit in hits:
        sym = hit["symbol"]
        try:
            plan = plan_for(hit, day, cfg)
        except exitp.PlanError as e:
            log("%s skipped at plan: %s" % (sym, e))
            continue
        s = size.size_trade(plan, cfg, price=hit["price"], cash=port["cash"], today=day.isoformat())
        order = size.bracket_order(s)
        if order is None:
            log("%s skipped at sizing: %s" % (sym, s["refusal"]))
            continue
        flag = news_flag(sym)
        if flag:
            log("%s skipped at news: %s" % (sym, flag))
            continue
        px = dict(prices, **{sym: hit["price"]})
        res = risk.check([order], cfg, portfolio=port, prices=px, state=state, today=day.isoformat(),
                         now=now_utc, price_times={k: now_utc for k in px})
        r = res["results"][0]
        if r["verdict"] != "PASS":
            log("%s BLOCKED by trader-risk: %s" % (sym, "; ".join(r["reasons"])))
            if any(x.startswith(("daily loss", "trades:", "window", "daily limit")) for x in r["reasons"]):
                return "stopped: " + r["reasons"][0]
            continue
        line = ("%s ORB: buy %s @ ~%s, stop %s, target %s, max loss %s, cost ~%s (risk PASS)"
                % (sym, order["qty"], size.money(s["entry"]), order["stop_loss_stop_price"],
                   order["take_profit_limit_price"], size.money(s["max_loss"]), size.money(s["cost"])))
        if dry:
            return "DRY RUN, not sent: " + line
        cid = "ap-%s-%s-%s" % (day.strftime("%Y%m%d"), sym, now_et.strftime("%H%M"))
        try:
            placed = trade("POST", "/orders", rest_body(order, cid))
        except ApiError as e:
            # remember the symbol so no later cycle today tries it again (a halt, a rejection)
            save_trade({"date": day.isoformat(), "symbol": sym, "failed": True, "error": str(e)[:200]})
            log("%s order FAILED, not retried today: %s" % (sym, e))
            return "order failed"
        save_trade({"date": day.isoformat(), "symbol": sym, "qty": int(order["qty"]), "entry_plan": s["entry"],
                    "stop": s["stop"], "target": s["target"], "max_loss": s["max_loss"],
                    "or_high": hit["or_high"], "or_low": hit["or_low"], "signal_at": hit["at"],
                    "client_order_id": cid, "order_id": placed.get("id")})
        time.sleep(3)
        try:
            o = trade("GET", "/orders/%s" % placed["id"])
            fill = "filled %s @ %s" % (o.get("filled_qty"), o.get("filled_avg_price")) \
                if o.get("filled_avg_price") else "status %s" % o.get("status")
        except ApiError:
            fill = "status unknown"
        return "PLACED " + line + ", " + fill
    return "%d breakout(s), none passed every check" % len(hits)


# ---------------------------------------------------------------- flatten and report

def flatten():
    try:
        trade("DELETE", "/orders")
    except ApiError as e:
        log("cancel all: %s" % e)
    # wait for the bracket legs to finish cancelling: until then they hold the shares and
    # Alpaca refuses the closing sell ("insufficient qty available")
    for _ in range(15):
        time.sleep(1)
        try:
            if not trade("GET", "/orders?status=open&limit=200"):
                break
        except ApiError:
            pass
    for attempt in range(5):
        pos = trade("GET", "/positions") or []
        if not pos:
            log("flat: no positions")
            return True
        for p in pos:
            try:
                trade("DELETE", "/positions/%s?cancel_orders=true" % p["symbol"])
                log("closing %s %s shares (unrealized %s)" % (p["symbol"], p["qty"],
                                                            size.money(float(p.get("unrealized_pl") or 0))))
            except ApiError as e:
                log("close %s: %s" % (p["symbol"], e))
        time.sleep(4)
    pos = trade("GET", "/positions") or []
    if pos:
        log("WARNING: still holding %s" % ", ".join(p["symbol"] for p in pos))
    return not pos


def fills(day):
    q = urllib.parse.urlencode({"date": day.isoformat(), "direction": "asc", "page_size": 100})
    return trade("GET", "/account/activities/FILL?" + q) or []


def report(day):
    plans = {r["symbol"]: r for r in read_trades() if r.get("date") == day.isoformat() and not r.get("failed")}
    by = {}
    for f in fills(day):
        d = by.setdefault(f["symbol"], {"buy_q": 0.0, "buy_v": 0.0, "sell_q": 0.0, "sell_v": 0.0, "last": ""})
        q, p = float(f["qty"]), float(f["price"])
        if f["side"] == "buy":
            d["buy_q"] += q
            d["buy_v"] += q * p
        else:
            d["sell_q"] += q
            d["sell_v"] += q * p
            d["last"] = f.get("transaction_time", "")
    rate = aud_rate()
    rows, total, wins, losses = [], 0.0, 0, 0
    for sym, d in by.items():
        entry = d["buy_v"] / d["buy_q"] if d["buy_q"] else 0
        exit_ = d["sell_v"] / d["sell_q"] if d["sell_q"] else 0
        closed = abs(d["buy_q"] - d["sell_q"]) < 1e-9
        pnl = round(d["sell_v"] - d["buy_v"], 2) if closed else None
        pl = plans.get(sym, {})
        stop, target = pl.get("stop"), pl.get("target")
        if pnl is None:
            how = "STILL OPEN"
        elif target and exit_ >= target - min(target * 0.0025, 0.25 * (target - (stop or 0)) / 3):
            how = "hit target"
        elif stop and exit_ <= stop + min(stop * 0.0025, 0.25 * ((target or stop) - stop) / 3):
            how = "hit stop"
        else:
            how = "closed at flat-by"
        if pnl is not None:
            total += pnl
            wins += pnl > 0
            losses += pnl < 0
        rows.append({"date": day.isoformat(), "ticker": sym, "shares": int(d["buy_q"]), "entry": round(entry, 2),
                     "stop": stop, "target": target, "exit": round(exit_, 2), "pnl_usd": pnl, "what": how})
    total = round(total, 2)
    aud = round(total / rate, 2) if rate else None
    lines = ["# Autopilot day report: %s (paper)" % day.isoformat(), "",
             "| Ticker | Shares | Entry | Stop | Target | Exit | P&L USD | What happened |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %.2f | %s | %s | %.2f | %s | %s |" % (
            r["ticker"], r["shares"], r["entry"], r["stop"] or "-", r["target"] or "-", r["exit"],
            "-" if r["pnl_usd"] is None else "%+.2f" % r["pnl_usd"], r["what"]))
    lines += ["", "Trades: %d (wins %d, losses %d)" % (len(rows), wins, losses),
              "Total P&L: %+.2f USD%s" % (total, (" (about %+.2f AUD at %.4f)" % (aud, rate)) if aud is not None else "")]
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, "%s.md" % day.isoformat())
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    done = set()
    if os.path.exists(JOURNAL):
        with open(JOURNAL, encoding="utf-8") as fh:
            done = {(r["date"], r["ticker"]) for r in csv.DictReader(fh)}
    new = not os.path.exists(JOURNAL)
    with open(JOURNAL, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "ticker", "shares", "entry", "stop", "target", "exit", "pnl_usd", "what_happened",
                        "lesson"])
        for r in rows:
            if (r["date"], r["ticker"]) not in done and r["pnl_usd"] is not None:
                w.writerow([r["date"], r["ticker"], r["shares"], r["entry"], r["stop"], r["target"], r["exit"],
                            r["pnl_usd"], r["what"], "autopilot ORB"])
    print("\n".join(lines))
    print("\nSaved %s" % os.path.relpath(path, ROOT))
    return {"total": total, "aud": aud, "trades": len(rows), "path": path}


# ---------------------------------------------------------------- the session loop

def sleep_until(target_utc, deadline):
    while True:
        now = utcnow()
        if now >= target_utc:
            return True
        if now >= deadline:
            return False
        time.sleep(min(30, (target_utc - now).total_seconds(), max((deadline - now).total_seconds(), 1)))


def run(a):
    cfg = json.load(open(CONFIG))
    if not cfg.get("paper", True):
        log("config says paper=false: the autopilot is paper only, stopping")
        return 2
    start = utcnow()
    deadline = start + datetime.timedelta(minutes=a.max_minutes)

    if a.replay:
        day = datetime.date.fromisoformat(a.replay)
        at = scan.parse_hhmm(a.at or "10:30")
        now_utc = scan.eastern_to_utc(datetime.datetime.combine(day, at))
        syms = universe()[: a.limit or None]
        log("replay %s %s, %d symbols" % (day, at, len(syms)))
        log(cycle(cfg, day, now_utc, syms, dry=True, rng="5d"))
        return 0

    _keys()
    clock = trade("GET", "/clock")
    now_et = scan.to_eastern(utcnow())
    day = now_et.date()
    ok, why = scan.trading_day(day)
    if not ok:
        log("no session today (%s)" % why)
        return 0
    sess = scan.session(day)
    et = lambda t: scan.eastern_to_utc(datetime.datetime.combine(day, t))
    open_u, close_u = et(sess[0]), et(sess[1])
    flat = scan.parse_hhmm(cfg.get("flat_by", "15:55"))
    if sess[1] < datetime.time(16, 0):
        flat = (datetime.datetime.combine(day, sess[1]) - datetime.timedelta(minutes=5)).time()
    flat_u = et(flat)
    first_u = max(open_u + datetime.timedelta(minutes=OR_MINUTES + 5),
                  et(scan.parse_hhmm(cfg.get("no_entries_before", "09:45")))) + datetime.timedelta(seconds=25)
    last_u = et(scan.parse_hhmm(cfg.get("no_entries_after", "15:30")))
    if a.dry_run:
        log("DRY RUN: nothing will be sent")
    log("session %s, market %s; first look %s, last entry %s, flat by %s NY"
        % (day, "open" if clock.get("is_open") else "closed", scan.to_eastern(first_u).strftime("%H:%M"),
           scan.to_eastern(last_u).strftime("%H:%M"), flat.strftime("%H:%M")))

    if utcnow() >= flat_u:
        if utcnow() < close_u + datetime.timedelta(minutes=30) and not a.dry_run:
            flatten()
        if not a.dry_run:
            report(day)
        return 0

    if not sleep_until(first_u, deadline):
        log("time box ended before the first look; run again")
        return 10

    syms, syms_at = [], None
    while True:
        now = utcnow()
        if now >= flat_u:
            break
        if now >= deadline:
            log("time box ended; run again to carry on")
            return 10
        if now <= last_u + datetime.timedelta(minutes=1):
            if syms_at is None or (now - syms_at).total_seconds() > 1800:
                syms = universe()
                syms_at = now
                log("universe: %d liquid symbols" % len(syms))
            try:
                log(cycle(cfg, day, now, syms, dry=a.dry_run))
            except (ApiError, scan.ScanError, exitp.PlanError, size.SizeError, risk.RiskError) as e:
                log("cycle error: %s" % e)
        # the next 5-minute bar close plus 25 seconds, or flat-by, whichever is first
        et_now = scan.to_eastern(utcnow())
        nxt = et_now.replace(second=25, microsecond=0) + datetime.timedelta(
            minutes=scan.BAR_MIN - et_now.minute % scan.BAR_MIN)
        target = min(scan.eastern_to_utc(nxt), flat_u)
        if not sleep_until(target, deadline):
            log("time box ended; run again to carry on")
            return 10

    log("flat-by %s NY: closing everything" % flat.strftime("%H:%M"))
    if a.dry_run:
        return 0
    flatten()
    time.sleep(20)
    report(day)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--max-minutes", type=float, default=115)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--replay", metavar="YYYY-MM-DD", help="replay one cycle of a past session (dry run)")
    r.add_argument("--at", help="replay time, HH:MM New York")
    r.add_argument("--limit", type=int, help="replay: only the first N symbols")
    sub.add_parser("flatten")
    p = sub.add_parser("report")
    p.add_argument("--date")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            return run(a)
        if a.cmd == "flatten":
            return 0 if flatten() else 2
        day = datetime.date.fromisoformat(a.date) if a.date else scan.to_eastern(utcnow()).date()
        report(day)
        return 0
    except ApiError as e:
        log("fatal: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
