import time
import schwabdev
from itertools import groupby
from .context import BacktestContext, LiveContext
from .data import Data


class Trader:
    def __init__(self, client: object = None, account_hash: str = None, cache_db: str = "~/.schwabdev/candles.db"):
        if account_hash and not client:
            raise ValueError("client and account_hash are required for live trading (hint: client.linked_accounts().json())")
        self._client = client
        self._account_hash = account_hash
        self.data = Data(cache_db, client)
        self._streamer = None
        self.live = None

    # backtesting -------------------------------------------------------------

    def backtest(self, strategy, tickers, history_days=90, cash=10_000, report=True, plot=False,
                 chart=True, level1=False, level2=False, costs=None, fill_delay=2):
        """Replay cached data through `strategy` and return a backtest run.

        `chart`/`level1`/`level2` pick which event types are sent to the strategy. Candles are
        always replayed and always settle orders — with chart=False they still track order
        fulfillment and mark positions, they just don't wake the strategy, which instead runs on
        the recorded quote/book events (tc.quotes / tc.books hold the latest per symbol). l1/l2
        events exist only where `Data.record()` or a live deploy previously captured them.

        `costs` (a `Costs` instance) sets the spread/slippage/fee model and `fill_delay` how many
        candles a MARKET order waits before filling at that candle's open."""
        run = BacktestContext(tickers, cash, costs=costs, fill_delay=fill_delay)

        if plot:
            run.serve(port=8000)

        events = []
        for ticker in tickers:
            events.extend(self.data.get_candles(ticker, history_days))  # always: fills/marking
            if level1 or level2:
                events.extend(self.data.get_events(ticker, history_days, level1, level2))
        events.sort(key=lambda e: e["time"])

        enabled = {"c": chart, "l1": level1, "l2": level2}
        for _, group in groupby(events, key=lambda e: e["time"]):
            group = list(group)
            run.step(strategy, group, notify=any(enabled[e["type"]] for e in group))

        if report:
            run.report()
        return run

    def record(self, tickers, chart=True, level1=True, level2=True, verbose=True, **start_auto_kwargs):
        """Record live market data into the DB (see `Data.record`); returns the streamer."""
        return self.data.record(tickers, chart=chart, level1=level1, level2=level2, verbose=verbose, **start_auto_kwargs)

    # live trading ------------------------------------------------------------

    def deploy(self, strategy, tickers, cash=0, plot=True, chart=True, level1=False, level2=False, record=True, costs=None, sync_positions=True):
        """Open a live session and stream market data + account activity into it. Orders are sent
        to the broker only when this Trader was created with live_orders=True; otherwise the
        session paper-trades on live data. Returns the LiveContext (also available as `self.live`).

        `cash` defaults to the account's real settled cash when trading live (and to 10,000 for a
        paper session with no account to read), so `tc.cash` means the same thing live as in a
        backtest; pass a number to override. With sync_positions the account's existing share
        positions are adopted too, so `tc.sellable()` doesn't report flat on stock you own.

        `chart`/`level1`/`level2` pick which event types wake the strategy; candles are always
        subscribed and ingested since they price MARKET orders, settle paper fills and mark
        positions. With record=True (default) every subscribed data type is also written to the DB
        through the same `Data.parse`/`Data.write` pair `record()` uses — so a live session backfills
        the candle cache AND captures l1/l2 history for later backtests, for free."""
        if self._account_hash:
            input("LIVE ORDERS ENABLED, orders will be sent to Schwab. Press ENTER to continue or Ctrl-C to abort.")
        session = LiveContext(tickers, cash, self._client, self._account_hash, costs=costs)
        self.live = session
        if session.live_orders:
            session.sync_account(positions=sync_positions)
            print(f"[live] account cash ${session.cash:,.2f}, positions {session.positions or '{}'}")

        if plot:
            session.serve(port=8000) 

        import traceback
        self._streamer = schwabdev.Stream(self._client)
        
        def handler(msg):
            try:
                events = self.data.parse(msg) 
                if record:
                    self.data.write(events) 
                for item in events["activity"]:
                    session._on_activity(item)

                batch = list(events["chart"])
                if level1:
                    batch.extend(events["l1"])
                if level2:
                    batch.extend(events["l2"])
                if batch:
                    notify = bool((chart and events["chart"]) or (level1 and events["l1"]) or (level2 and events["l2"]))
                    session.step(strategy, batch, notify=notify)
            except Exception:
                print("[live] handler error (session continues):")
                traceback.print_exc()

        self._streamer.start(handler, daemon=False)
        time.sleep(1.0)
        self._streamer.send(self._streamer.account_activity("Account Activity", "0,1,2,3"))
        self.data.subscribe(self._streamer, session.tickers, chart=True, level1=level1, level2=level2)
        return session

    def stop(self):
        """Stop the stream, close the viewer and release the DB write connection."""
        if self._streamer:
            self._streamer.stop()
            self._streamer = None
        server = getattr(self.live, "_server", None)
        if server is not None:
            server.shutdown()
            server.server_close()
            self.live._server = None
        self.data.close()
