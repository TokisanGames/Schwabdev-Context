import json
import threading
import time as _time


def find(o, key):
    """First value for `key` found anywhere in a nested dict/list, else None."""
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            r = find(v, key)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find(v, key)
            if r is not None:
                return r
    return None


def dec(node):
    """Decode a Schwab decimal node, e.g. {"lo": "220470000", "signScale": 12} -> 220.47.

    Scaled nodes carry `signScale` and are fixed-point with 6 implied decimals (1 share ==
    {"lo": "1000000", "signScale": 12}). Nodes WITHOUT `signScale` are plain integers — sizes
    arrive that way ({"lo": "200"} is 200 shares, not 0.0002), which is why the scale has to be
    checked rather than assumed. Plain numbers pass through; anything missing/unexpected -> 0.0."""
    if isinstance(node, dict):
        try:
            lo = int(node.get("lo", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return lo / 1e6 if "signScale" in node else float(lo)
    if isinstance(node, bool):
        return 0.0
    if isinstance(node, (int, float)):
        return float(node)
    return 0.0


class Costs:
    """Transaction-cost model applied at fill time (simulation only). `apply` returns the
    effective execution price (reference price moved against you by half-spread + slippage) and
    the cash fee (flat + per-share + percentage, plus sell-only SEC/FINRA regulatory fees).

    `worst` caps the price for orders that legally cannot fill through their own level: a BUY LIMIT
    never pays more than the limit, a SELL LIMIT never receives less. Slippage on a limit order
    shows up as a missed fill in reality, not a worse price."""

    def __init__(self, flat=0.0, per_share=0.0, pct=0.00_05,
                 half_spread=0.0, slip=0.0,
                 sec_fee=20.60e-6, taf_per_share=0.000195):
        self.flat, self.per_share, self.pct = flat, per_share, pct
        self.half_spread, self.slip = half_spread, slip
        self.sec_fee, self.taf = sec_fee, taf_per_share

    def apply(self, instruction, qty, ref_price, limit=None):
        side = 1 if instruction == "BUY" else -1
        exec_price = ref_price * (1 + side * (self.half_spread + self.slip))
        if limit is not None:                                # can't fill through your own limit
            exec_price = min(exec_price, limit) if side > 0 else max(exec_price, limit)
        exec_price = max(exec_price, 0.0)
        fee = self.flat + self.per_share * qty + self.pct * qty * exec_price
        if instruction == "SELL":                            # regulatory: sells only
            fee += self.sec_fee * qty * exec_price + self.taf * qty
        return exec_price, fee


class Context:
    # Account-activity event types (field "2" of an ACCT_ACTIVITY message). Matched by keyword
    # against the upper-cased type, so new spellings of the same event still land in the right
    # bucket — but the keywords must NOT be so loose that a routing/lifecycle event looks like a
    # fill. "ExecutionRequested", "ExecutionRequestCreated", "ExecutionRequestCompleted" and
    # "ExecutionCreated" are all lifecycle events that carry an ExecutionQuantity; only a
    # *FillCompleted event books shares.
    _FILL_TYPES = ("FILLCOMPLETED", "FILLED")
    _OUT_TYPES = ("UROUT", "CANCELACCEPTED", "CANCELED", "CANCELLED",
                  "REJECT", "EXPIRE", "EXPIRED")

    def __init__(self, tickers, cash=10_000, costs=None, fill_delay=2):
        self.tickers = [t.upper() for t in tickers]
        self.candles = {t: [] for t in self.tickers}  # symbol -> [candle, ...] seen so far; latest is candles[s][-1]
        self.quotes = {}            # symbol -> latest level-1 quote {"time","bid","ask","last","bid_size","ask_size"}
        self.books = {}             # symbol -> latest level-2 snapshot {"time","bids","asks"}
        self.plots = {}             # overlay name -> [(time, value), ...]
        self.positions = {}         # symbol -> shares owned (float); absent/0 == flat
        self.orders = {}            # symbol -> order_id -> record (see `_open`)
        self.costs = costs if isinstance(costs, Costs) else Costs()
        self.fill_delay = fill_delay
        self.fees_paid = 0.0        # cumulative simulated commission/fees
        self._starting_cash = cash
        self._cash = cash           # settled cash
        self._order_seq = 0         # monotonic local id source (never reused, unlike a count)
        self._lock = threading.RLock()

    # market data -------------------------------------------------------------

    def _last(self, symbol):
        """Latest close for `symbol`, or None if no candle has arrived yet."""
        cs = self.candles.get(symbol.upper())
        return cs[-1]["close"] if cs else None

    def _now(self, symbol):
        """Timestamp to stamp a fill with: the latest candle for `symbol`, else the newest candle
        anywhere, else the wall clock. Never 0 — a fill stamped at the epoch is invisible to the
        chart and sorts before every candle."""
        cs = self.candles.get(symbol.upper())
        if cs:
            return cs[-1]["time"]
        times = [c[-1]["time"] for c in self.candles.values() if c]
        return max(times) if times else int(_time.time() * 1000)

    def _ingest(self, events):
        """Route one tick's mixed batch of events into the market-data stores. Level-2 snapshots
        replace the book; level-1 quotes MERGE into the previous quote (the stream sends deltas, so
        unchanged fields arrive as None); candles append to the per-symbol history — except a repeat
        of the newest timestamp, which REPLACES it, because the chart stream re-sends the forming
        minute bar until it closes (the same reason `Data.write` uses INSERT OR REPLACE). Appending
        those would duplicate minutes, inflating the bar count `_settle_pending` measures its fill
        delay in. Events are told apart by shape: candles carry "open", books carry "bids"/"asks",
        quotes carry "bid"."""
        for e in events:
            symbol = e["symbol"]
            if "open" in e:                                   # chart candle
                cs = self.candles.setdefault(symbol, [])
                if cs and cs[-1]["time"] == e["time"]:
                    cs[-1] = e                                # forming bar re-sent: overwrite
                else:
                    cs.append(e)
            elif "bids" in e or "asks" in e:                  # level-2 book snapshot
                self.books[symbol] = e
            else:                                             # level-1 quote delta
                quote = self.quotes.setdefault(symbol, {})
                quote.update({k: v for k, v in e.items() if v is not None})

    def step(self, strategy, events, notify=True):
        """One tick, shared by backtest replay, warm-up preload and the live stream: advance time
        with the new events (candles / quotes / books), settle orders resting from earlier ticks
        against the candles, then run the strategy (whose new orders settle on a later step).

        With notify=False the tick still advances time and settles fills, but the strategy is not
        called — that is how candle data keeps driving order fulfillment when the strategy has
        opted out of chart events (chart=False)."""
        self._ingest(events)
        self._settle_pending()
        if notify:
            strategy(self, events)

    # order plumbing (shared in the parent) -----------------------------------

    def _parse(self, order):
        """Validate + normalize an equity MARKET/LIMIT/STOP single-leg order. Raises ValueError on
        anything unsupported (options, multi-leg, trailing, unknown instruction). MARKET orders
        take the latest close as their reference price; LIMIT uses `price`; STOP uses `stopPrice`
        (its intrabar trigger level)."""
        legs = order.get("orderLegCollection") or []
        if len(legs) != 1:
            raise ValueError("only single-leg orders are supported")
        leg = legs[0]
        instrument = leg.get("instrument") or {}
        if instrument.get("assetType") != "EQUITY":
            raise ValueError("only equity orders are supported")
        order_type = order.get("orderType")
        if order_type not in ("MARKET", "LIMIT", "STOP"):
            raise ValueError("only MARKET, LIMIT and STOP orders are supported")
        instruction = leg.get("instruction")
        if instruction not in ("BUY", "SELL"):
            raise ValueError("only BUY and SELL instructions are supported")
        symbol = str(instrument["symbol"]).upper()
        quantity = int(leg["quantity"])  # fractional shares are not supported
        if quantity <= 0:
            raise ValueError("quantity must be a positive whole number of shares")
        if order_type == "LIMIT":
            if order.get("price") is None:
                raise ValueError("LIMIT orders require a price")
            price = float(order["price"])
        elif order_type == "STOP":
            stop = order.get("stopPrice", order.get("price"))  # trigger level
            if stop is None:
                raise ValueError("STOP orders require a stopPrice")
            price = float(stop)
        else:  # MARKET
            price = self._last(symbol)
            if price is None:
                raise ValueError(f"no market price yet for {symbol}; cannot price a MARKET order")
        if price <= 0:
            raise ValueError(f"non-positive price for {symbol}: {price}")
        return {"symbol": symbol, "instruction": instruction, "quantity": quantity,
                "order_type": order_type, "price": price}

    def _open(self, parsed, raw, order_id=None, sim=True):
        """Record a parsed order's OPEN legs in the ledger and return the record. Adopts the
        broker's `order_id` when given, otherwise mints a local one from a monotonic counter (a
        running COUNT would collide once broker ids are mixed in and would be reused if a record
        were ever dropped).

        `price` stays the order's own level (limit / stop / market reference) for the life of the
        record — `_settle_pending` reads it as the trigger, so a partial fill must not overwrite it.
        Executions accumulate in `filled` / `avg_price` instead. `sim=False` marks an order that
        the broker owns, which `_settle_pending` must never touch."""
        with self._lock:
            if order_id is None:
                self._order_seq += 1
                order_id = self._order_seq
            rec = {"id": order_id, "symbol": parsed["symbol"], "instruction": parsed["instruction"],
                   "order_type": parsed["order_type"], "quantity": parsed["quantity"], "filled": 0,
                   "price": parsed["price"], "avg_price": 0.0, "fees": 0.0,
                   "time": self._now(parsed["symbol"]), "placed_len": len(self.candles.get(parsed["symbol"], [])),
                   "status": "OPEN", "sim": sim, "order": raw}
            self.orders.setdefault(parsed["symbol"], {})[rec["id"]] = rec
            return rec

    def _find_order(self, order_id):
        """Locate a ledger record by id across all symbols, or None. Ids from the broker are
        strings while local ids are ints, so both spellings are tried."""
        keys = [order_id, str(order_id)]
        if str(order_id).isdigit():
            keys.append(int(order_id))
        with self._lock:
            for book in self.orders.values():
                for key in keys:
                    if key in book:
                        return book[key]
        return None

    def snapshot_orders(self):
        """Flat list of every ledger record, copied under the lock. Readers on another thread (the
        viewer's HTTP handler) must use this: walking `orders` live races the stream thread's
        inserts and raises "dictionary changed size during iteration"."""
        with self._lock:
            return [dict(r) for book in self.orders.values() for r in book.values()]

    def order(self, order):
        """Place an order. In a simulated context it rests in the ledger (reserving cash via `cash`
        / committing shares via `sellable`) and settles in a later `step()`; a MARKET order settles
        at once when `fill_delay` is 0, since with real time moving forward there is no look-ahead
        to protect against. Returns the order id."""
        parsed = self._parse(order)
        rec = self._open(parsed, order)
        if self.fill_delay <= 0 and parsed["order_type"] == "MARKET":
            self._sim_fill(rec, rec["quantity"], parsed["price"])
        return rec["id"]

    def cancel(self, order_id):
        """Cancel a still-OPEN order, freeing the cash it reserved / shares it committed. Returns
        True if the order was open and is now canceled. `LiveContext` overrides this to route the
        cancel to the broker first."""
        with self._lock:
            rec = self._find_order(order_id)
            if rec is not None and rec["status"] == "OPEN":
                rec["status"] = "CANCELED"
                return True
        return False

    def _settle_pending(self):
        """Fill eligible resting SIMULATED orders against each symbol's latest candle. Called once
        per tick from `step()`, before the strategy runs, so a backtest order can never settle on
        the candle that triggered it. Orders the broker owns (`sim=False`) are skipped — their
        fills arrive on the account-activity stream.

          * MARKET : fills once `fill_delay` candles have elapsed since placement, at that candle's
                     OPEN — a realistic next-bar fill rather than a look-ahead same-close fill.
          * LIMIT  : rests until a later candle's open or close crosses the limit (>= for SELL,
                     <= for BUY), then fills at the limit price.
          * STOP   : triggers intrabar off the candle's low (SELL) / high (BUY) rather than the
                     close, filling at the stop level — or at the open if the bar gapped through
                     it, which is the worse price. This lets losses land mid-bar instead of waiting
                     for the close.

        `fill_delay <= 0` (a live paper session) also allows same-bar settlement: the candle in
        hand is the present, not a bar the strategy has already seen the end of."""
        same_bar_ok = self.fill_delay <= 0
        with self._lock:
            resting = [r for book in self.orders.values() for r in book.values()
                       if r["status"] == "OPEN" and r.get("sim", True)]
        for rec in resting:
            cs = self.candles.get(rec["symbol"])
            if not cs:
                continue
            candle = cs[-1]                                # this symbol's newest candle
            elapsed = len(cs) - rec.get("placed_len", 0)
            remaining = rec["quantity"] - rec["filled"]
            if remaining <= 0:
                continue

            otype = rec["order_type"]
            limit = None
            if otype == "MARKET":
                if elapsed < self.fill_delay:              # wait the fixed delay, then fill
                    continue
                ref = candle["open"] if self.fill_delay > 0 else candle["close"]
            elif otype == "LIMIT":                         # rest until price is touched
                if elapsed < 1 and not same_bar_ok:        # never on the triggering candle
                    continue
                limit = rec["price"]
                if rec["instruction"] == "SELL":
                    hit = candle["open"] >= limit or candle["close"] >= limit
                else:                                      # BUY
                    hit = candle["open"] <= limit or candle["close"] <= limit
                if not hit:
                    continue
                ref = limit
            else:                                          # STOP: intrabar trigger on high/low
                if elapsed < 1 and not same_bar_ok:        # never on the triggering candle
                    continue
                stop = rec["price"]
                if rec["instruction"] == "SELL":           # protective stop under a long
                    if candle["low"] > stop:               # bar never traded down to the stop
                        continue
                    # triggered intrabar; a market-on-trigger fills at the stop, or worse if the
                    # bar gapped open straight through it (fill at the open, then slippage).
                    ref = min(candle["open"], stop)
                else:                                      # buy stop above the market (breakout)
                    if candle["high"] < stop:              # bar never traded up to the stop
                        continue
                    ref = max(candle["open"], stop)

            self._sim_fill(rec, remaining, ref, limit=limit)

    def _sim_fill(self, rec, quantity, ref_price, limit=None):
        """Apply the cost model to a simulated execution and settle it. The only place `Costs` is
        used, so real broker fills (which carry their own commissions) never get charged twice."""
        px, fee = self.costs.apply(rec["instruction"], quantity, ref_price, limit=limit)
        with self._lock:
            self._fill(rec, quantity, px)
            rec["fees"] += fee
            self.fees_paid += fee
            self._cash -= fee

    def _fill(self, rec, quantity, price):
        """Settle `quantity` shares of `rec` at `price`: move settled cash, update the position's
        share count, accumulate the volume-weighted average execution price, stamp the fill time
        and advance the order's progress/status. This is the single settlement engine used by the
        backtest, live paper trades, and real broker fills.

        `rec["price"]` is deliberately NOT touched: it is the order's trigger level, and a partial
        fill that rewrote it would move the limit the rest of the order is still resting on."""
        with self._lock:
            symbol = rec["symbol"]
            quantity = min(quantity, rec["quantity"] - rec["filled"])   # never over-fill
            if quantity <= 0:
                return
            signed = quantity if rec["instruction"] == "BUY" else -quantity

            self._cash -= signed * price                       # buys spend cash, sells collect it
            held = self.positions.get(symbol, 0.0) + signed
            if abs(held) < 1e-9:
                self.positions.pop(symbol, None)               # flat -> drop the entry
            else:
                self.positions[symbol] = held

            filled = rec["filled"] + quantity
            rec["avg_price"] = (rec["avg_price"] * rec["filled"] + price * quantity) / filled
            rec["filled"] = filled
            rec["time"] = self._now(symbol)
            if filled >= rec["quantity"] - 1e-9:
                rec["status"] = "FILLED"

    # strategy-facing surface -------------------------------------------------

    @property
    def starting_cash(self):
        """Cash the run started with (buy-and-hold baseline)."""
        return self._starting_cash

    @property
    def cash(self):
        """Spendable cash: settled cash minus the notional reserved by still-open BUY orders."""
        with self._lock:
            reserved = sum((r["quantity"] - r["filled"]) * r["price"]
                           for book in self.orders.values() for r in book.values()
                           if r["status"] == "OPEN" and r["instruction"] == "BUY")
            return self._cash - reserved

    def sellable(self, symbol):
        """Shares free to sell = owned − shares already committed to still-open SELL orders."""
        symbol = symbol.upper()
        with self._lock:
            owned = self.positions.get(symbol, 0.0)
            committed = sum(r["quantity"] - r["filled"]
                            for r in self.orders.get(symbol, {}).values()
                            if r["status"] == "OPEN" and r["instruction"] == "SELL")
            return owned - committed

    def portfolio_value(self):
        """Settled cash + open positions marked to their latest close."""
        with self._lock:
            value = self._cash
            for symbol, qty in list(self.positions.items()):
                last = self._last(symbol)
                if last is not None:
                    value += qty * last
            return value

    def plot(self):
        from .analysis import plot
        plot(self)

    def serve(self, **kwargs):
        from .analysis import serve
        return serve(self, **kwargs)

    def report(self):
        from .analysis import report
        return report(self)


class BacktestContext(Context):
    """A single, self-contained backtest: its own candles / orders / positions / cash / overlays /
    stats. Keep several around to compare strategies/parameter sets without interference.

    Orders do NOT fill on submission — see `Context._settle_pending` for the fill rules. Spread /
    slippage / fees are applied at fill time through `self.costs`.

    `step()` also records an equity curve (one point per tick that carried a candle), which is what
    lets `analysis` report max drawdown and a Sharpe ratio rather than just a net return."""

    def __init__(self, tickers, cash=10_000, costs=None, fill_delay=2, track_equity=True):
        super().__init__(tickers, cash, costs=costs, fill_delay=fill_delay)
        self.equity = []                 # [(time_ms, portfolio_value), ...]
        self.track_equity = track_equity

    def step(self, strategy, events, notify=True):
        super().step(strategy, events, notify=notify)
        if self.track_equity:
            times = [e["time"] for e in events if "open" in e]
            if times:
                self.equity.append((max(times), self.portfolio_value()))


class LiveContext(Context):
    """The live trading session.

    With a client and account hash, orders are routed to Schwab and fills arrive later on the
    account-activity stream (`_on_activity`); those records are marked `sim=False` so the simulator
    leaves them alone. With no broker the session PAPER trades on live data, running the same
    simulated lifecycle as a backtest but with `fill_delay=0`: a MARKET order fills immediately at
    the last price, while LIMIT and STOP orders rest until the market actually touches them.
    (Filling a resting limit the moment it is placed — at its own price, whatever the market was
    doing — is the one way paper results can flatter a strategy without bound.)"""

    def __init__(self, tickers, cash, client=None, account_hash=None, costs=None):
        super().__init__(tickers, cash, costs=costs, fill_delay=0)
        self._client = client
        self._account_hash = account_hash
        self.warming = False       # True while replaying preloaded history: orders are ignored
        self._executions = set()   # ExecutionIds already booked (the stream can repeat them)

    @property
    def live_orders(self):
        """True when orders leave this process for the broker; False when paper trading."""
        return bool(self._client and self._account_hash)

    def sync_account(self, positions=True):
        """Adopt the account's real settled cash (and optionally its share positions) from Schwab,
        so `tc.cash` / `tc.sellable()` mean the same thing live as in a backtest. Called by
        `Trader.deploy` when trading for real; a no-op without a client. Returns the cash figure."""
        if not (self._client and self._account_hash):
            return self._cash
        try:
            r = self._client.account_details(self._account_hash,
                                             fields="positions" if positions else None)
            if not r.ok:
                return self._cash
            acct = (r.json() or {}).get("securitiesAccount", {})
        except Exception as exc:
            print(f"[live] could not read account balances ({exc}); keeping cash={self._cash}")
            return self._cash

        balances = acct.get("currentBalances", {}) or {}
        cash = balances.get("cashAvailableForTrading", balances.get("cashBalance"))
        with self._lock:
            if cash is not None:
                self._cash = float(cash)
                self._starting_cash = float(cash)
            if positions:
                for p in acct.get("positions", []) or []:
                    sym = str((p.get("instrument") or {}).get("symbol", "")).upper()
                    qty = float(p.get("longQuantity", 0.0)) - float(p.get("shortQuantity", 0.0))
                    if sym and abs(qty) > 1e-9:
                        self.positions[sym] = qty
            return self._cash

    def order(self, order):
        if self.warming:
            return None  # warm-up replay: the strategy runs, but nothing is placed or filled
        parsed = self._parse(order)  # validate before anything leaves this process
        if not self.live_orders:
            return super().order(order)                    # paper: simulate, same as a backtest

        resp = self._client.place_order(self._account_hash, order)
        if not getattr(resp, "ok", False):
            # A rejected order must NOT be booked. Previously an error response fell through to the
            # instant-fill path below, inventing a position the broker never gave us.
            body = ""
            try:
                body = resp.text[:200]
            except Exception:
                pass
            raise RuntimeError(f"broker rejected order ({getattr(resp, 'status_code', '?')}): {body}")

        order_id = (resp.headers.get("location", "/").rsplit("/", 1)[-1] or "").strip() or None
        if order_id:
            # normal case: record OPEN, marked as the broker's, and wait for fills on the stream
            return self._open(parsed, order, order_id, sim=False)["id"]
        # accepted but no id => filled on the spot, and no stream fill will reference it; book it
        rec = self._open(parsed, order, sim=False)
        self._fill(rec, rec["quantity"], parsed["price"])
        return rec["id"]

    def cancel(self, order_id):
        """Route the cancel to Schwab before marking the local record. Marking it locally first is
        how a still-live order goes silently missing: `_on_activity` ignores messages for records
        that are no longer OPEN, so the eventual fill would never reach the ledger."""
        rec = self._find_order(order_id)
        if rec is None or rec["status"] != "OPEN":
            return False
        if self.live_orders and not rec.get("sim", True):
            try:
                resp = self._client.cancel_order(self._account_hash, rec["id"])
            except Exception as exc:
                print(f"[live] cancel of {rec['id']} failed to send: {exc}")
                return False
            if not getattr(resp, "ok", False):
                print(f"[live] broker refused to cancel {rec['id']} "
                      f"({getattr(resp, 'status_code', '?')}); leaving it OPEN")
                return False
            return True   # the stream's UROut message closes the record
        return super().cancel(order_id)

    def _on_activity(self, item):
        """Handle one ACCT_ACTIVITY message. Schwab puts the event type in field "2" and a nested
        JSON payload in field "3"; we dig the order id / executed quantity / price out of that
        payload (see `find`/`dec`) and settle it through the same `_fill` the backtester uses.
        Messages for orders we didn't place (or already settled) are ignored.

        Only *FillCompleted events book shares. The order lifecycle also emits ExecutionRequested,
        ExecutionRequestCreated/Completed and ExecutionCreated — all of which carry an
        ExecutionQuantity, and one of which (ExecutionCreated with ExecutionTransType "UROut") is
        part of the CANCEL path. Treating "contains EXECUTION" as a fill books cancels as
        purchases. Executions are also de-duplicated by ExecutionId, since a repeat of the same
        message would otherwise double the position."""
        msg_type = str(item.get("2", "")).upper()
        raw = item.get("3")
        if raw in (None, ""):
            return
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return

        oid = find(payload, "SchwabOrderID") or find(payload, "schwabOrderID") or find(payload, "OrderId")
        rec = self._find_order(str(oid)) if oid is not None else None
        if rec is None or rec["status"] != "OPEN":
            return

        if any(k in msg_type for k in self._OUT_TYPES):        # canceled / rejected / expired / UR-out
            with self._lock:
                rec["status"] = "CANCELED"
        elif any(k in msg_type for k in self._FILL_TYPES):     # an execution -> book what filled
            trans = str(find(payload, "ExecutionTransType") or "FILL").upper()
            if "FILL" not in trans:                            # e.g. a UROut execution record
                return
            exec_id = find(payload, "ExecutionId") or find(payload, "ExecutionID")
            if exec_id is not None:
                if exec_id in self._executions:
                    return                                     # already booked this execution
                self._executions.add(exec_id)
            qty = dec(find(payload, "ExecutionQuantity") or find(payload, "Quantity"))
            price = dec(find(payload, "ExecutionPrice") or find(payload, "AveragePrice")
                        or find(payload, "Price"))
            if qty > 0 and price > 0:
                self._fill(rec, qty, price)

    def report(self):
        from .analysis import report
        return report(self, title="LIVE SESSION REPORT")
