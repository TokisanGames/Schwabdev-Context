"""
Tools for analyzing backtest runs and charting.
"""

import datetime
import json as _json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _orders(run):
    """Flat list of every order in the run's ledger, newest state, safe to walk on another thread
    (a live session's ledger is mutated from the stream thread while the viewer reads it)."""
    snapshot = getattr(run, "snapshot_orders", None)
    if callable(snapshot):
        return snapshot()
    return [dict(o) for book in run.orders.values() for o in book.values()]


def _fill_price(order):
    """Average execution price of an order. `price` is the order's trigger level (limit / stop /
    market reference); `avg_price` is what it actually traded at."""
    return order.get("avg_price") or order["price"]


def trades(run):
    """Close every position FIFO and return the realized round trips, chronologically:
        {"symbol","qty","entry","exit","opened","closed","long","pnl","ret","fees"}

    Executions are matched share by share against the open lots, so buy/buy/sell leaves half a
    position open instead of silently discarding an order, and a partial fill of 30 out of 100
    shares closes 30. Pairing the buy list against the sell list positionally (`zip(buys, sells)`)
    quietly mismatches every trade after the first unequal-sized order and ignores quantity
    entirely, which inflates or deflates the win rate depending on the strategy's shape."""
    fills = sorted((o for o in _orders(run) if o["filled"] > 0), key=lambda o: o["time"])
    out, lots = [], {}
    for o in fills:
        symbol, qty = o["symbol"], o["filled"]
        price = _fill_price(o)
        fee_per_share = (o.get("fees", 0.0) / qty) if qty else 0.0
        side = 1 if "BUY" in o["instruction"] else -1
        book = lots.setdefault(symbol, [])

        remaining = qty
        while remaining > 1e-9 and book and book[0]["side"] != side:   # closing existing lots
            lot = book[0]
            matched = min(remaining, lot["qty"])
            fees = (lot["fee_per_share"] + fee_per_share) * matched
            pnl = (price - lot["price"]) * matched * lot["side"] - fees
            basis = lot["price"] * matched
            out.append({"symbol": symbol, "qty": matched, "entry": lot["price"], "exit": price,
                        "opened": lot["time"], "closed": o["time"], "long": lot["side"] > 0,
                        "fees": fees, "pnl": pnl,
                        "ret": (pnl / basis * 100) if basis else 0.0})
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-9:
                book.pop(0)
        if remaining > 1e-9:                                          # opening (or adding to) a lot
            book.append({"side": side, "qty": remaining, "price": price,
                         "fee_per_share": fee_per_share, "time": o["time"]})
    return out


def _curve_stats(equity):
    """Max drawdown (%) and an annualized Sharpe ratio from an [(time_ms, value), ...] curve.
    The annualization factor comes from the curve's own median sampling interval, so a
    minute-candle backtest and a daily one are both scaled correctly. Returns zeros for a curve
    too short to say anything about."""
    if len(equity) < 3:
        return {"max_drawdown": 0.0, "sharpe": 0.0}

    peak, max_dd = equity[0][1], 0.0
    for _, value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)

    rets, gaps = [], []
    for (t0, v0), (t1, v1) in zip(equity, equity[1:]):
        if v0 > 0:
            rets.append(v1 / v0 - 1)
            gaps.append(max(t1 - t0, 1))
    sharpe = 0.0
    if len(rets) > 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var > 0:
            gaps.sort()
            per_year = 365.25 * 24 * 3600 * 1000 / gaps[len(gaps) // 2]
            sharpe = mean / var ** 0.5 * per_year ** 0.5
    return {"max_drawdown": max_dd, "sharpe": sharpe}


def compute_stats(run):
    """Build the stats dict report() prints and cache it on `run.stats` (no printing). Net return
    vs uniform buy-and-hold, risk figures off the equity curve when one was recorded, and
    per-ticker stats from the FIFO round trips in `trades()`. `max_possible` is the summed up-moves
    (a loose ceiling)."""
    closed = trades(run)
    starting_cash = run._starting_cash
    final = run.portfolio_value()
    ret = (final - starting_cash) / starting_cash * 100 if starting_cash else 0.0
    orders = _orders(run)

    stats = {"starting_cash": starting_cash, "final_value": final, "net_return": ret,
             "fees_paid": getattr(run, "fees_paid", 0.0),
             "realized_pnl": sum(t["pnl"] for t in closed),
             "n_orders": len(orders),
             "n_open_orders": sum(1 for o in orders if o["status"] == "OPEN"),
             "n_trades": len(closed), "open_positions": dict(run.positions),
             "tickers": {}}
    stats.update(_curve_stats(getattr(run, "equity", []) or []))
    bhs = []

    for sym in run.tickers:
        cs = run.candles.get(sym, [])
        if not cs:
            continue
        f0, f1 = cs[0]["close"], cs[-1]["close"]
        bh = (f1 - f0) / f0 * 100
        max_gain = sum(max(b["close"] - a["close"], 0) for a, b in zip(cs, cs[1:])) / f0 * 100
        bhs.append(bh)

        buys = [o for o in orders if o["symbol"] == sym and "BUY" in o["instruction"] and o["filled"] > 0]
        sells = [o for o in orders if o["symbol"] == sym and "SELL" in o["instruction"] and o["filled"] > 0]
        sym_trades = [t for t in closed if t["symbol"] == sym]
        rets = [t["ret"] for t in sym_trades]
        wins = sum(1 for t in sym_trades if t["pnl"] > 0)
        gross_win = sum(t["pnl"] for t in sym_trades if t["pnl"] > 0)
        gross_loss = -sum(t["pnl"] for t in sym_trades if t["pnl"] < 0)

        stats["tickers"][sym] = {
            "buy_hold": bh, "max_possible": max_gain,
            "n_buys": len(buys), "n_sells": len(sells),
            "n_trades": len(sym_trades), "n_pairs": len(sym_trades),   # n_pairs: legacy alias
            "shares": sum(o["filled"] for o in buys) - sum(o["filled"] for o in sells),
            "wins": wins, "losses": len(sym_trades) - wins,
            "win_rate": (wins / len(sym_trades) * 100) if sym_trades else 0,
            "pnl": sum(t["pnl"] for t in sym_trades),
            "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf") if gross_win else 0.0,
            "avg": (sum(rets) / len(rets)) if rets else 0,
            "best": max(rets) if rets else 0,
            "worst": min(rets) if rets else 0,
            "returns": rets,
            "trades": sym_trades,
        }
    stats["uniform_b&h"] = sum(bhs) / len(bhs) if bhs else 0
    run.stats = stats
    return stats


def report(run, title="BACKTEST REPORT"):
    """Print the summary built by compute_stats(); the stats dict is cached on `run.stats` and
    also returned. Trades are FIFO round trips (see `trades()`); `max_possible` is a loose
    ceiling."""
    stats = compute_stats(run)

    S = "\u2500" * 58
    pf = lambda v: "  n/a" if v in (0.0, float("inf")) else f"{v:.2f}"
    print(f"\n{S}\n  {title}\n{S}")
    print(f"  Cash start / end     : ${stats['starting_cash']:,.2f}  \u2192  ${stats['final_value']:,.2f}")
    print(f"  Net return           : {stats['net_return']:+.2f}%")
    print(f"  Uniform buy-and-hold : {stats['uniform_b&h']:+.2f}%")
    print(f"  Realized / fees      : ${stats['realized_pnl']:+,.2f}  /  ${stats['fees_paid']:,.2f}")
    if stats["max_drawdown"] or stats["sharpe"]:
        print(f"  Max drawdown         : {stats['max_drawdown']:.2f}%")
        print(f"  Sharpe (annualized)  : {stats['sharpe']:.2f}")
    print(f"  Orders / round trips : {stats['n_orders']} ({stats['n_open_orders']} open)"
          f"  /  {stats['n_trades']}")
    if stats["open_positions"]:
        held = ", ".join(f"{s} {q:g}" for s, q in sorted(stats["open_positions"].items()))
        print(f"  Still holding        : {held}")

    for sym, t in stats["tickers"].items():
        if not (t["n_buys"] or t["n_sells"]):
            continue                       # never traded: the buy-and-hold line says nothing new
        print(f"\n  [{sym}]")
        print(f"  Buy-and-hold         : {t['buy_hold']:+.2f}%")
        print(f"  Max possible (sum\u2191)  : {t['max_possible']:+.2f}%")
        print(f"  Orders               : {t['n_buys']} buys, {t['n_sells']} sells, "
              f"{t['n_trades']} round trips")
        if t["n_trades"]:
            print(f"  Win rate             : {t['win_rate']:.1f}%  ({t['wins']}W/{t['losses']}L)"
                  f"   profit factor {pf(t['profit_factor'])}")
            print(f"  Avg / best / worst   : {t['avg']:+.2f}%  /  {t['best']:+.2f}%  /  {t['worst']:+.2f}%")
            print(f"  Realized P&L         : ${t['pnl']:+,.2f}")
        if abs(t["shares"]) > 1e-9:
            print(f"  Open position        : {t['shares']:g} shares")
    print(f"{S}\n")
    return stats


_OVERLAY_COLORS = ["#F39C12", "#9B59B6", "#1ABC9C", "#E67E22"]


def _tail(seq, since_ms, key):
    """Items of `seq` with key(item) > since_ms, walking from the end (cheap for polls). A
    non-positive `since_ms` returns a full copy. Safe against concurrent appends: it snapshots
    the length first and never looks past it."""
    n = len(seq)
    if since_ms <= 0:
        return list(seq[:n])
    out = []
    for i in range(n - 1, -1, -1):
        item = seq[i]
        if key(item) <= since_ms:
            break
        out.append(item)
    out.reverse()
    return out


def plot_payload(run, since_ms=0):
    """Build the JSON the browser view renders. With since_ms=0 it is the full snapshot; with a
    watermark it contains only candle/overlay points and (filled) order markers newer than the
    watermark, so the page can poll cheaply and stream a running backtest or live session.
    Times are emitted in SECONDS (lightweight-charts wants UNIX seconds); `now` is the ms
    watermark the client should send back (clients may re-request a small overlap — point
    updates are idempotent)."""
    tickers = [t for t in (run.tickers or list(run.candles.keys())) if run.candles.get(t)]
    orders = _orders(run)
    panels = []
    now = 0

    for sym in tickers:
        candles = _tail(run.candles[sym], since_ms, lambda c: c["time"])
        all_cs = run.candles[sym]
        if all_cs:
            now = max(now, all_cs[-1]["time"])
        closes  = [{"time": c["time"] // 1000, "value": c["close"]}  for c in candles]
        volumes = [{"time": c["time"] // 1000, "value": c["volume"]} for c in candles]

        # same overlay-selection rule as before: include an overlay if its name has no ticker in
        # it (a global series) OR this ticker's name is in it; the global index keeps colours
        # stable across polls (dict order is insertion order).
        overlays = []
        for i, (name, pts) in enumerate(list(run.plots.items())):
            if any(t in name for t in tickers) and sym not in name:
                continue
            new_pts = _tail(pts, since_ms, lambda p: p[0])
            overlays.append({
                "name":  name,
                "color": _OVERLAY_COLORS[i % len(_OVERLAY_COLORS)],
                "data":  [{"time": t // 1000, "value": v} for t, v in new_pts],
            })

        markers = []
        for o in orders:
            if o["symbol"] != sym or o["filled"] <= 0 or o["time"] <= since_ms:
                continue                          # only executed orders make markers
            buy = "BUY" in o["instruction"]
            markers.append({
                "id":       str(o["id"]),          # broker ids are strings, local ones ints
                "time":     o["time"] // 1000,
                "position": "belowBar" if buy else "aboveBar",
                "color":    "#27AE60"  if buy else "#E74C3C",
                "shape":    "arrowUp"  if buy else "arrowDown",
                # the price it actually traded at, not the limit/stop it was resting on
                "text":     f'{"BUY" if buy else "SELL"} {o["filled"]:g} @ {_fill_price(o):.2f}',
            })

        if since_ms <= 0 or closes or volumes or markers or any(o["data"] for o in overlays):
            panels.append({"symbol": sym, "close": closes, "volume": volumes,
                           "overlays": overlays, "markers": markers})

    return {"portfolio_value": run.portfolio_value(), "now": now, "panels": panels}


def serve(run, port=8000, host="127.0.0.1", open_browser=True):
    """Start (or reuse) a background HTTP server streaming `run` to a browser viewer.

    Non-blocking: returns the server object immediately, so it can be called at ANY moment —
    before or during a backtest replay, or on a live session — and the page will show the
    strategy's current state and keep streaming as new candles, overlays and fills arrive
    (the page polls /data?since=<watermark> about once a second; /data is regenerated from the
    live run object per request). Repeated calls on the same run reuse the running server."""
    existing = getattr(run, "_server", None)
    if existing is not None:
        return existing

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence the default request logging
            pass

        def _send(self, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                self._send(_CHART_HTML, "text/html; charset=utf-8")
            elif path == "/data":
                since = 0
                for part in query.split("&"):
                    if part.startswith("since="):
                        try:
                            since = int(float(part[6:]))
                        except ValueError:
                            pass
                try:
                    self._send(_json.dumps(plot_payload(run, since)), "application/json")
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_response(404); self.end_headers()

    # bind, trying a few ports if the first is taken
    server, bound = None, None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, p), _Handler); bound = p; break
        except OSError:
            continue
    if server is None:
        raise OSError(f"could not bind a port in {port}..{port + 19}")

    url = f"http://{host}:{bound}"
    print(f"[serve] live charts at {url}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    run._server = server
    return server


def plot(run, port=8000, host="127.0.0.1", open_browser=True, block=True):
    """Serve the live view for `run`. Blocks like plt.show() (Ctrl-C to stop); pass block=False
    to just start the background server (same as serve())."""
    server = serve(run, port=port, host=host, open_browser=open_browser)
    if not block:
        return server
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("\n[serve] stopped")
        server.shutdown()
        server.server_close()
        run._server = None


# Browser page. Plain triple-quoted string (braces are literal); fetches /data and renders one
# lightweight-charts chart per ticker.
_CHART_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trader charts</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root {
    --bg:#0e1116; --panel:#151a21; --line:rgba(255,255,255,.06);
    --ink:#e7ecf3; --muted:#8a93a3; --close:#4C9BE8; --buy:#27AE60; --sell:#E74C3C; --vol:#7FB3D3;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:"SF Mono",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace; }
  header { position:sticky; top:0; z-index:10; display:flex; align-items:baseline; gap:18px;
           padding:14px 20px; background:rgba(14,17,22,.92); backdrop-filter:blur(6px);
           border-bottom:1px solid var(--line); }
  header .label { color:var(--muted); font-size:11px; letter-spacing:.18em; text-transform:uppercase; }
  header .pv { font-size:22px; font-weight:600; letter-spacing:-.01em; font-variant-numeric:tabular-nums; }
  header .spacer { flex:1; }
  button { font:inherit; font-size:12px; color:var(--ink); background:var(--panel);
           border:1px solid var(--line); border-radius:7px; padding:7px 13px; cursor:pointer; }
  button:hover { border-color:rgba(255,255,255,.22); }
  main { padding:16px 20px 40px; display:flex; flex-direction:column; gap:18px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .card .bar { display:flex; align-items:center; gap:16px; padding:11px 16px; border-bottom:1px solid var(--line); }
  .card .sym { font-size:14px; font-weight:600; letter-spacing:.04em; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; color:var(--muted); font-size:11px; }
  .legend span { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
  .swatch { width:14px; height:0; border-top:2px solid currentColor; }
  .swatch.dash { border-top-style:dashed; }
  .dot { width:0; height:0; border-left:5px solid transparent; border-right:5px solid transparent; }
  .dot.up { border-bottom:8px solid var(--buy); }
  .dot.down { border-top:8px solid var(--sell); }
  .chart { height:380px; width:100%; }
  .empty,.err { padding:40px 20px; color:var(--muted); text-align:center; }
  .err { color:var(--sell); }
  .live { font-size:11px; letter-spacing:.14em; padding:5px 10px; border-radius:6px; border:1px solid var(--line); }
  .live.ok { color:var(--buy); } .live.bad { color:var(--sell); }
</style>
</head>
<body>
<header>
  <div><div class="label">Portfolio</div><div class="pv" id="pv">--</div></div>
  <div class="spacer"></div>
  <span class="live ok" id="live">CONNECTING</span>
  <button id="refresh">Reload all</button>
</header>
<main id="main"><div class="empty">Loading…</div></main>

<script>
const fmtUSD = v => "$" + (v ?? 0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const LAP_MS = 120000;              // re-request a 2-min overlap; point updates are idempotent
let panels = new Map();             // symbol -> {chart, close, vol, overlays:Map, markers:Map}
let watermark = 0;                  // ms watermark from the server
let timer = null;

function destroyAll() {
  for (const p of panels.values()) { try { p.chart.remove(); } catch (e) {} }
  panels = new Map(); watermark = 0;
  document.getElementById("main").innerHTML = "";
}

function legendItem(cls, label, color) {
  const s = document.createElement("span"); s.style.color = color || "var(--muted)";
  const m = document.createElement("span"); m.className = cls; m.style.color = color || "currentColor";
  s.append(m, document.createTextNode(label)); return s;
}

function ensurePanel(sym) {
  if (panels.has(sym)) return panels.get(sym);
  const card = document.createElement("div"); card.className = "card";
  const bar = document.createElement("div"); bar.className = "bar";
  const s = document.createElement("div"); s.className = "sym"; s.textContent = sym;
  const legend = document.createElement("div"); legend.className = "legend";
  legend.append(legendItem("swatch", "Close", getComputedStyle(document.documentElement).getPropertyValue("--close")));
  legend.append(legendItem("dot up", "Buy"));
  legend.append(legendItem("dot down", "Sell"));
  legend.append(legendItem("swatch", "Volume", getComputedStyle(document.documentElement).getPropertyValue("--vol")));
  bar.append(s, legend); card.append(bar);
  const host = document.createElement("div"); host.className = "chart"; card.append(host);
  document.getElementById("main").append(card);

  const LC = LightweightCharts;
  const chart = LC.createChart(host, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: "#8a93a3",
              fontFamily: getComputedStyle(document.body).fontFamily },
    grid: { vertLines: { color: "rgba(255,255,255,.05)" }, horzLines: { color: "rgba(255,255,255,.05)" } },
    rightPriceScale: { borderColor: "rgba(255,255,255,.08)" },
    timeScale: { borderColor: "rgba(255,255,255,.08)", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LC.CrosshairMode.Normal },
  });
  chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.30 } });
  const vol = chart.addHistogramSeries({ priceScaleId: "volume", color: "#7FB3D3",
                                         priceFormat: { type: "volume" } });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.74, bottom: 0 } });
  const close = chart.addLineSeries({ color: "#4C9BE8", lineWidth: 2,
                                      priceLineVisible: false, lastValueVisible: true });
  const p = { chart, close, vol, overlays: new Map(), markers: new Map(), legend, fitted: false };
  panels.set(sym, p);
  return p;
}

function ensureOverlay(p, name, color) {
  if (p.overlays.has(name)) return p.overlays.get(name);
  const LC = LightweightCharts;
  const s = p.chart.addLineSeries({ color, lineWidth: 2, lineStyle: LC.LineStyle.Dashed,
                                    priceLineVisible: false, lastValueVisible: false });
  p.overlays.set(name, s);
  p.legend.insertBefore(legendItem("swatch dash", name, color), p.legend.children[1]);
  return s;
}

function applyPanel(d) {
  const p = ensurePanel(d.symbol);
  for (const pt of d.close)  { try { p.close.update(pt); } catch (e) {} }   // idempotent per time
  for (const pt of d.volume) { try { p.vol.update(pt);   } catch (e) {} }
  for (const o of d.overlays) {
    const s = ensureOverlay(p, o.name, o.color);
    for (const pt of o.data) { try { s.update(pt); } catch (e) {} }
  }
  if (d.markers.length) {
    for (const m of d.markers) p.markers.set(m.id, m);   // Map dedupes re-sent markers by order id
    const ms = [...p.markers.values()].sort((a, b) => a.time - b.time);
    try { p.close.setMarkers(ms); } catch (e) {}
  }
  if (!p.fitted && d.close.length) { p.chart.timeScale().fitContent(); p.fitted = true; }
}

function setLive(ok, extra) {
  const el = document.getElementById("live");
  el.textContent = ok ? ("LIVE " + (extra || "")) : "DISCONNECTED";
  el.className = ok ? "live ok" : "live bad";
}

async function poll() {
  const main = document.getElementById("main");
  try {
    const since = Math.max(0, watermark - LAP_MS);
    const r = await fetch("/data?since=" + since, { cache: "no-store" });
    const d = await r.json();
    document.getElementById("pv").textContent = fmtUSD(d.portfolio_value);
    if (typeof LightweightCharts === "undefined") {
      main.innerHTML = '<div class="err">Charting library failed to load (no network to unpkg.com).</div>'; return;
    }
    if (main.firstElementChild && main.firstElementChild.className === "empty" && d.panels.length)
      main.innerHTML = "";
    for (const pd of d.panels) applyPanel(pd);
    if (d.now > watermark) watermark = d.now;
    if (!panels.size) main.innerHTML = '<div class="empty">No data yet — waiting for candles…</div>';
    setLive(true, watermark ? new Date(watermark).toLocaleTimeString() : "");
  } catch (e) {
    setLive(false);
  } finally {
    timer = setTimeout(poll, 1000);
  }
}

document.getElementById("refresh").addEventListener("click", () => {
  clearTimeout(timer); destroyAll();
  document.getElementById("main").innerHTML = '<div class="empty">Loading…</div>';
  poll();
});
poll();
</script>
</body>
</html>"""