#!/usr/bin/env python3
"""Live dashboard for the paper autopilot: one phone-friendly page that refreshes itself.

  python3 trader/dashboard.py [--port 8765]

Reads the Alpaca PAPER account (keys stay on this machine, never sent to the page) and the
autopilot's log, and explains every event in plain words. Read-only: it never places,
changes or cancels an order. Not financial advice.
"""
import argparse
import datetime
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap  # noqa: E402

CACHE = {"at": 0.0, "doc": None}
LOCK = threading.Lock()
TTL = 8.0


# ---------------------------------------------------------------- plain-words log

PLACED = re.compile(r"PLACED (\S+) ORB: buy (\d+) @ ~\$([\d.,]+), stop ([\d.]+), target ([\d.]+), "
                    r"max loss \$([\d.]+), cost ~\$([\d.,]+).*?(?:filled (\S+) @ ([\d.]+))?$")
SIZING = re.compile(r"(\S+) skipped at sizing: (.*)")
NEWS = re.compile(r"(\S+) skipped at news: (.*)")
BLOCK = re.compile(r"(\S+) BLOCKED by trader-risk: (.*)")
NOBREAK = re.compile(r"no fresh breakout this bar \((\d+) symbols\)")


def explain(msg):
    """-> (kind, title, detail)"""
    m = PLACED.search(msg)
    if m:
        sym, qty, px, stop, tgt, loss, cost = m.group(1, 2, 3, 4, 5, 6, 7)
        fill = m.group(9) or px
        gain = round(int(qty) * (float(tgt) - float(fill)), 2)
        return ("buy", "Bought %s shares of %s at $%s" % (qty, sym, fill),
                "%s closed a 5-minute bar above its first-15-minute high (an opening-range breakout). "
                "Cost about $%s. Alpaca now holds two automatic exits: sell at $%s for about +$%.2f "
                "(target), or at $%s for about -$%s (stop). Whichever comes first." %
                (sym, cost, tgt, gain, stop, loss))
    m = SIZING.search(msg)
    if m:
        why = m.group(2)
        if "cash" in why:
            short = "not enough spare budget, most of it is already in open trades"
        elif "max loss" in why:
            short = "its stop is too far away: one share would risk more than your max loss per trade"
        elif "reward:risk" in why:
            short = "the possible gain is too small for the risk"
        else:
            short = why.split(". ")[0]
        return ("skip", "Skipped %s" % m.group(1), "Breakout seen, but %s." % short)
    m = NEWS.search(msg)
    if m:
        return ("skip", "Skipped %s (news)" % m.group(1),
                "A fresh headline can make the price jump unpredictably: \"%s\"" % m.group(2)[:140])
    m = BLOCK.search(msg)
    if m:
        return ("block", "Risk check blocked %s" % m.group(1), m.group(2))
    m = NOBREAK.search(msg)
    if m:
        return ("scan", "Scanned %s stocks" % m.group(1),
                "None broke out above its first-15-minute high in the last 5 minutes. Waiting for the next bar.")
    m = re.search(r"(\d+) breakout\(s\), none passed every check", msg)
    if m:
        return ("scan", "%s breakout(s) seen, none taken" % m.group(1), "Each one failed a rule (see the skips).")
    if msg.startswith("universe"):
        return ("info", "Refreshed the watch list", "Picked the most traded US stocks right now, $5 to $150 a share.")
    if msg.startswith("session"):
        return ("info", "Autopilot started", msg)
    if msg.startswith("time box"):
        return ("info", "Autopilot restarted its timer", "Normal: it runs in 2-hour blocks and carries on.")
    if msg.startswith("closing"):
        return ("sell", "Closing at the end of the day", msg)
    if msg.startswith("flat"):
        return ("info", "All positions closed", "Nothing is held overnight.")
    if "halted" in msg or "stopped" in msg or "done:" in msg:
        return ("block", "No more new trades today", msg)
    if "error" in msg or "FAILED" in msg:
        return ("block", "Problem", msg)
    return ("info", msg[:60], msg)


LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \[(\d\d:\d\d):\d\d NY \| \w+ (\d\d:\d\d) Sydney\] (.*)$")


def events(day, limit=25):
    out = []
    try:
        with open(ap.LOG, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for ln in lines:
        m = LINE.match(ln.strip())
        if not m:
            continue
        d, ny, syd, msg = m.groups()
        # the log's date is the UTC date; keep today's New York session (09:00-17:00 NY)
        if not (d == day or d == (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()):
            continue
        if d != day and ny > "17:00":
            continue
        kind, title, detail = explain(msg)
        out.append({"ny": ny, "syd": syd, "kind": kind, "title": title, "detail": detail})
    # fold the long runs of "scanned" lines into the latest one
    folded = []
    for e in out:
        if e["kind"] in ("scan", "info") and e["title"].startswith(("Scanned", "Refreshed")) \
                and folded and folded[-1]["title"].startswith(("Scanned", "Refreshed")):
            folded[-1] = dict(e, title=e["title"] + " (and earlier)")
        else:
            folded.append(e)
    return folded[-limit:][::-1]


# ---------------------------------------------------------------- state

def build():
    now = ap.utcnow()
    now_et = ap.scan.to_eastern(now)
    day = now_et.date()
    cfg = json.load(open(ap.CONFIG))
    acct = ap.trade("GET", "/account")
    clock = ap.trade("GET", "/clock")
    positions = ap.trade("GET", "/positions") or []
    plans = {}
    for r in ap.read_trades():
        if r.get("date") == day.isoformat():
            plans[r["symbol"]] = r
    open_rows = []
    for p in positions:
        pl = plans.get(p["symbol"], {})
        cur, entry = float(p["current_price"]), float(p["avg_entry_price"])
        stop, tgt = pl.get("stop"), pl.get("target")
        pos = None
        if stop and tgt and tgt > stop:
            pos = max(0.0, min(1.0, (cur - stop) / (tgt - stop)))
        qty = float(p["qty"])
        open_rows.append({"symbol": p["symbol"], "qty": qty, "entry": entry, "price": cur, "stop": stop,
                          "target": tgt, "pnl": float(p["unrealized_pl"]), "pos": pos,
                          "entry_pos": (entry - stop) / (tgt - stop) if pos is not None else None,
                          "win_if_target": round(qty * (tgt - entry), 2) if tgt else None,
                          "loss_if_stop": round(qty * (entry - stop), 2) if stop else None,
                          "since": pl.get("signal_at", "")})
    # closed trades from today's fills
    by = {}
    for f in ap.fills(day):
        d = by.setdefault(f["symbol"], {"bq": 0.0, "bv": 0.0, "sq": 0.0, "sv": 0.0})
        q, px = float(f["qty"]), float(f["price"])
        if f["side"] == "buy":
            d["bq"] += q
            d["bv"] += q * px
        else:
            d["sq"] += q
            d["sv"] += q * px
    closed, realized, wins, losses = [], 0.0, 0, 0
    for sym, d in by.items():
        if d["sq"] <= 0 or abs(d["bq"] - d["sq"]) > 1e-9:
            continue
        pnl = round(d["sv"] - d["bv"], 2)
        pl = plans.get(sym, {})
        ex = d["sv"] / d["sq"]
        how = "hit target" if pl.get("target") and ex >= pl["target"] - 0.01 else \
              "hit stop" if pl.get("stop") and ex <= pl["stop"] + 0.01 else "closed at end of day"
        closed.append({"symbol": sym, "qty": d["bq"], "entry": d["bv"] / d["bq"], "exit": ex, "pnl": pnl, "how": how})
        realized += pnl
        wins += pnl > 0
        losses += pnl < 0
    state, _ = ap.book(day)
    risk_used = round(sum(r.get("max_loss") or 0 for r in state["orders"]), 2)
    rate = CACHE.get("rate") or ap.aud_rate()
    CACHE["rate"] = rate
    day_change = float(acct["equity"]) - float(acct["last_equity"])
    sess = ap.scan.session(day)
    et = lambda hhmm: ap.scan.eastern_to_utc(datetime.datetime.combine(day, ap.scan.parse_hhmm(hhmm)))
    marks = []
    if sess:
        for label, hhmm in (("Open", "09:30"), ("First scan", "09:50"), ("Last new trade", cfg["no_entries_after"]),
                            ("Sell everything", cfg["flat_by"]), ("Close", sess[1].strftime("%H:%M"))):
            u = et(hhmm)
            marks.append({"label": label, "ny": hhmm, "syd": ap.sydney(u).strftime("%H:%M"),
                          "in_min": round((u - now).total_seconds() / 60), "utc": u.strftime("%Y-%m-%dT%H:%M:%SZ")})
    try:
        running = (time.time() - os.path.getmtime(ap.LOG)) < 420
    except OSError:
        running = False
    return {
        "updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "now_ny": now_et.strftime("%H:%M:%S"), "now_syd": ap.sydney(now).strftime("%a %H:%M:%S"),
        "market_open": bool(clock.get("is_open")), "running": running,
        "day_change": round(day_change, 2), "realized": round(realized, 2),
        "unrealized": round(sum(r["pnl"] for r in open_rows), 2), "aud": rate,
        "trades": len(state["orders"]), "max_trades": cfg["max_trades_per_day"], "wins": wins, "losses": losses,
        "risk_used": risk_used, "daily_limit": cfg["max_daily_loss_usd"], "per_trade": cfg["max_loss_per_trade_usd"],
        "budget": cfg["budget"], "open": sorted(open_rows, key=lambda r: -r["pnl"]), "closed": closed,
        "events": events(day.isoformat()), "marks": marks,
    }


def state():
    with LOCK:
        if CACHE["doc"] is None or time.time() - CACHE["at"] > TTL:
            try:
                CACHE["doc"] = build()
                CACHE["doc"]["error"] = None
            except Exception as e:  # keep the last good picture, say what went wrong
                doc = dict(CACHE["doc"] or {})
                doc["error"] = str(e)[:200]
                CACHE["doc"] = doc
            CACHE["at"] = time.time()
        return CACHE["doc"]


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Autopilot Live</title>
<style>
:root{--bg:#0b0f14;--card:#121821;--line:#1f2a36;--text:#e6edf3;--mute:#8b98a5;--up:#2fd479;--down:#ff5c6c;
--warn:#f4b740;--blue:#4aa3ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,Roboto,sans-serif;padding:16px 16px 40px;
max-width:760px;margin:auto}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);margin:24px 0 8px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:4px 10px;border-radius:99px;
background:#17212c;color:var(--mute)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mute)}
.live .dot{background:var(--up);box-shadow:0 0 0 0 rgba(47,212,121,.6);animation:p 1.6s infinite}
@keyframes p{70%{box-shadow:0 0 0 8px rgba(47,212,121,0)}100%{box-shadow:0 0 0 0 rgba(47,212,121,0)}}
.big{font-size:44px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums;margin:6px 0 0}
.sub{color:var(--mute);font-size:13px}
.up{color:var(--up)}.down{color:var(--down)}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:12px}
.stat{background:#0f141b;border-radius:10px;padding:10px}.stat b{display:block;font-size:18px;
font-variant-numeric:tabular-nums}.stat span{font-size:12px;color:var(--mute)}
.meter{height:8px;background:#0f141b;border-radius:99px;overflow:hidden;margin-top:6px}
.meter i{display:block;height:100%;background:var(--warn);border-radius:99px}
.pos{margin-bottom:10px}.pos .row{display:flex;justify-content:space-between;align-items:baseline}
.sym{font-weight:700;font-size:17px}.track{position:relative;height:10px;border-radius:99px;margin:12px 0 4px;
background:linear-gradient(90deg,rgba(255,92,108,.35),#1b2430 45%,#1b2430 55%,rgba(47,212,121,.35))}
.track .now{position:absolute;top:-5px;width:4px;height:20px;border-radius:2px;background:#fff;transform:translateX(-2px)}
.track .ent{position:absolute;top:-2px;width:2px;height:14px;background:var(--mute)}
.ends{display:flex;justify-content:space-between;font-size:12px;color:var(--mute)}
.ev{display:flex;gap:10px;padding:10px 0;border-top:1px solid var(--line)}.ev:first-child{border-top:0}
.ev .t{font-size:12px;color:var(--mute);min-width:44px;font-variant-numeric:tabular-nums}
.ev .k{width:8px;height:8px;border-radius:50%;margin-top:7px;flex:none}
.k.buy{background:var(--up)}.k.sell{background:var(--blue)}.k.skip{background:var(--warn)}.k.block{background:var(--down)}
.k.scan,.k.info{background:#3a4654}
.ev b{display:block;font-weight:600}.ev p{margin:2px 0 0;color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
td,th{padding:8px 4px;border-top:1px solid var(--line);text-align:right}th{color:var(--mute);font-weight:500;
font-size:12px;border-top:0}td:first-child,th:first-child{text-align:left}
.time{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}.mk{flex:none;background:#0f141b;border-radius:10px;
padding:8px 10px;min-width:104px}.mk b{display:block}.mk span{font-size:12px;color:var(--mute)}.mk.past{opacity:.45}
details{margin-top:10px}summary{cursor:pointer;color:var(--blue)}
.err{background:#3a1a1f;color:#ffb3bb;padding:8px 12px;border-radius:10px;margin-top:10px;display:none}
.foot{color:var(--mute);font-size:12px;margin-top:24px}
</style></head><body>
<div class="top"><div><div style="font-weight:700;font-size:18px">Autopilot Live</div>
<div class="sub" id="clock">loading...</div></div><div><span class="pill" id="live"><i class="dot"></i><span id="livetxt">connecting</span></span>
<span class="pill">PAPER money</span></div></div>
<div class="err" id="err"></div>

<div class="card" style="margin-top:14px"><div class="sub">Today's profit / loss</div>
<div class="big" id="pnl">-</div><div class="sub" id="pnlsub"></div>
<div class="grid">
<div class="stat"><b id="real">-</b><span>Locked in (closed trades)</span></div>
<div class="stat"><b id="unreal">-</b><span>Floating (still open)</span></div>
<div class="stat"><b id="trades">-</b><span>Trades today</span></div>
<div class="stat"><b id="wl">-</b><span>Wins / losses (closed)</span></div>
</div>
<div style="margin-top:12px" class="sub">Daily loss limit used <span id="riskt"></span></div>
<div class="meter"><i id="riskm" style="width:0"></i></div>
<details><summary>What does this mean?</summary><p class="sub">Each trade's worst case (its stop) is counted against your
daily limit the moment it is placed, win or lose. When the bar is full, the autopilot stops opening new trades
for the day. It can never lose more than the limit by design, except for small slippage when a price moves fast.</p></details>
</div>

<h2>Today's timeline</h2><div class="time" id="marks"></div>

<h2>Open trades <span id="nopen"></span></h2><div id="open"></div>
<details class="card" style="margin-top:0"><summary>How to read the bars</summary><p class="sub">The left end is the
stop (automatic sell at a small loss), the right end is the target (automatic sell at a profit). The white
marker is the price now, the grey tick is where we bought. When the marker reaches either end, Alpaca sells
by itself.</p></details>

<h2>What the bot is doing</h2><div class="card" id="events"></div>

<h2>Closed trades</h2><div class="card"><table id="closed"></table></div>

<h2>How the strategy works</h2><div class="card sub">
<p><b style="color:var(--text)">1. Opening range.</b> For the first 15 minutes after the open, it just watches each
stock's highest and lowest price.</p>
<p><b style="color:var(--text)">2. Breakout.</b> Every 5 minutes it scans about 160 of the most traded stocks. When
one closes a 5-minute bar above that early high, that is a breakout.</p>
<p><b style="color:var(--text)">3. Exit plan first.</b> The stop goes at the middle of the early range, the target
at twice the risk. So a win is about 2x a loss.</p>
<p><b style="color:var(--text)">4. Size.</b> Buys only as many whole shares as keep the loss at the stop under
<span id="pt"></span>, inside a <span id="bud"></span> practice budget.</p>
<p><b style="color:var(--text)">5. Risk check.</b> Your rules (trade count, daily limit, hours, no news) must all
pass, then one bracket order goes to Alpaca with the stop and target attached.</p>
<p><b style="color:var(--text)">6. Flat by the close.</b> At 3:55 PM New York everything still open is sold.</p>
</div>
<div class="foot">Paper trading with fake money. Not financial advice. Refreshes every 10 seconds.</div>
<script>
const $=id=>document.getElementById(id);
const usd=v=>(v<0?"-":v>0?"+":"")+"$"+Math.abs(v).toFixed(2);
const cls=v=>v>0?"up":v<0?"down":"";
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function mins(m){if(m<=0)return"done";const h=Math.floor(m/60),r=m%60;return"in "+(h?h+"h ":"")+r+"m"}
async function tick(){
 let d;try{const r=await fetch("api/state",{cache:"no-store"});d=await r.json()}catch(e){
  $("livetxt").textContent="offline";$("live").className="pill";return}
 $("err").style.display=d.error?"block":"none";$("err").textContent=d.error?"Last update failed: "+d.error:"";
 $("clock").textContent="New York "+d.now_ny+"  |  Sydney "+d.now_syd;
 $("live").className="pill"+(d.running&&d.market_open?" live":"");
 $("livetxt").textContent=d.market_open?(d.running?"bot running":"bot not running"):"market closed";
 $("pnl").textContent=usd(d.day_change);$("pnl").className="big "+cls(d.day_change);
 $("pnlsub").textContent=d.aud?"about "+usd(d.day_change/d.aud).replace("$","A$")+" at "+d.aud.toFixed(4)+" AUD/USD":"";
 $("real").textContent=usd(d.realized);$("real").className=cls(d.realized);
 $("unreal").textContent=usd(d.unrealized);$("unreal").className=cls(d.unrealized);
 $("trades").textContent=d.trades+" / "+d.max_trades;$("wl").textContent=d.wins+" / "+d.losses;
 const pct=Math.min(100,100*d.risk_used/d.daily_limit);$("riskm").style.width=pct+"%";
 $("riskt").textContent="$"+d.risk_used.toFixed(2)+" of $"+d.daily_limit;
 $("pt").textContent="$"+d.per_trade;$("bud").textContent="$"+d.budget.toLocaleString();
 $("marks").innerHTML=d.marks.map(m=>`<div class="mk ${m.in_min<=0?"past":""}"><b>${esc(m.label)}</b>
  <span>${m.syd} Sydney<br>${m.ny} NY<br>${mins(m.in_min)}</span></div>`).join("");
 $("nopen").textContent="("+d.open.length+")";
 $("open").innerHTML=d.open.length?d.open.map(p=>`<div class="card pos"><div class="row"><span class="sym">${esc(p.symbol)}</span>
  <span class="${cls(p.pnl)}" style="font-weight:700">${usd(p.pnl)}</span></div>
  <div class="sub">${p.qty} shares, bought $${p.entry.toFixed(2)}, now $${p.price.toFixed(2)}${p.since?" | signal "+esc(p.since)+" NY":""}</div>
  ${p.pos!=null?`<div class="track"><i class="ent" style="left:${(100*p.entry_pos).toFixed(1)}%"></i>
  <i class="now" style="left:${(100*p.pos).toFixed(1)}%"></i></div>
  <div class="ends"><span>stop $${p.stop.toFixed(2)} (${usd(-p.loss_if_stop)})</span><span>target $${p.target.toFixed(2)} (${usd(p.win_if_target)})</span></div>`:""}
  </div>`).join(""):`<div class="card sub">No open trades right now.</div>`;
 $("events").innerHTML=d.events.length?d.events.map(e=>`<div class="ev"><div class="t">${e.syd}<br><span style="opacity:.7">${e.ny} NY</span></div>
  <i class="k ${e.kind}"></i><div><b>${esc(e.title)}</b><p>${esc(e.detail)}</p></div></div>`).join(""):"<div class='sub'>Nothing yet.</div>";
 $("closed").innerHTML=d.closed.length?"<tr><th>Stock</th><th>Shares</th><th>Buy</th><th>Sell</th><th>P&amp;L</th></tr>"+
  d.closed.map(c=>`<tr><td><b>${esc(c.symbol)}</b><br><span class="sub">${esc(c.how)}</span></td><td>${c.qty}</td>
  <td>$${c.entry.toFixed(2)}</td><td>$${c.exit.toFixed(2)}</td><td class="${cls(c.pnl)}">${usd(c.pnl)}</td></tr>`).join("")
  :"<tr><td class='sub'>No closed trades yet.</td></tr>";
}
tick();setInterval(tick,10000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/api/state") or path == "api/state":
            self._send(200, json.dumps(state()), "application/json")
        elif path in ("", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print("dashboard on port %d" % a.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
