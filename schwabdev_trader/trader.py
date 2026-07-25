import time
from itertools import groupby

from .context import BacktestContext, LiveContext
from .data import Data


def _kind(event):
    """Which subscription an event came from, by shape: candles carry "open", books carry
    "bids"/"asks", everything else is a level-one quote."""
    if "open" in event:
        return "chart"
    if "bids" in event or "asks" in event:
        return "l2"
    return "l1"


def _wakes(group, chart, level1, level2):
    """Whether a group of same-timestamp events should wake the strategy: true if the group holds
    at least one event of an ENABLED type. Testing `"open" not in e` instead would let a disabled
    type wake the strategy the moment a recording contained one."""
    enabled = {"chart": chart, "l1": level1, "l2": level2}
    return any(enabled[_kind(e)] for e in group)


class Trader:
    def __init__(self, client=None, account_hash=None, live_orders=False, cache_db="~/.schwabdev/candles.db"):
        if live_orders and not (account_hash and client):
            raise ValueError("client and account_hash are required for live trading (hint: client.linked_accounts().json())")
        self._client = client
        self._account_hash = account_hash
        self._live_orders = live_orders
        self.data = Data(cache_db, client)  # (caching + recording)
        self._streamer = None
        self.live = None

    # shared replay -----------------------------------------------------------

    def _load(self, tickers, days, level1, level2):
        """Chronological events for `tickers` over `days`: candles always (they settle orders and
        mark positions), plus recorded l1/l2 when those flags are set."""
        events = []
        for ticker in tickers:
            events.extend(self.data.get_candles(ticker, days))  # always: fills/marking
            if level1 or level2:
                events.extend(self.data.get_events(ticker, days, level1, level2))
        events.sort(key=lambda e: e["time"])
        return events

    @staticmethod
    def _replay(context, strategy, events, chart, level1, level2):
        """Feed `events` through `strategy` one timestamp at a time, grouping everything that shares
        a timestamp into a single tick. Used for both backtest replay and live warm-up preload."""
        for _, group in groupby(events, key=lambda e: e["time"]):
            group = list(group)
            context.step(strategy, group, notify=_wakes(group, chart, level1, level2))

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

        self._replay(run, strategy, self._load(run.tickers, history_days, level1, level2),
                     chart, level1, level2)

        if report:
            run.report()
        return run

    def record(self, tickers, chart=True, level1=True, level2=True, verbose=True, **start_auto_kwargs):
        """Record live market data into the DB (see `Data.record`); returns the streamer."""
        return self.data.record(tickers, chart=chart, level1=level1, level2=level2,
                                verbose=verbose, **start_auto_kwargs)

    # live trading ------------------------------------------------------------

    def deploy(self, strategy, tickers, cash=None, preload_days=0, plot=True,
               chart=True, level1=False, level2=False, record=True, costs=None,
               sync_positions=True):
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
        the candle cache AND captures l1/l2 history for later backtests, for free.

        Before the stream starts, the previous `preload_days` of candles (and recorded l1/l2, when
        flagged) are replayed THROUGH the strategy with ordering suppressed (session.warming), so
        its filters/indicators start hot and the viewer opens with history — but no orders can fire
        on stale data. With plot=True the browser viewer is served immediately and streams the
        session as it trades."""
        account_hash = self._account_hash if self._live_orders else None  # None => paper-trade
        session = LiveContext(tickers, 0.0 if cash is None else cash,
                              self._client, account_hash, costs=costs)
        self.live = session
        if cash is None:
            if session.live_orders:
                session.sync_account(positions=sync_positions)
                print(f"[live] account cash ${session.cash:,.2f}, "
                      f"positions {session.positions or '{}'}")
            else:
                session._cash = session._starting_cash = 10_000.0  # paper default

        if preload_days:
            session.warming = True   # strategy runs, orders are ignored
            self._replay(session, strategy,
                         self._load(session.tickers, preload_days, level1, level2),
                         chart, level1, level2)
            session.warming = False  # from here on, orders are real (or paper)

        if plot:
            session.serve(port=8000)  # live-streaming viewer; no refresh needed

        import schwabdev
        self._streamer = schwabdev.Stream(self._client)

        def handler(msg):
            try:
                events = self.data.parse(msg)          # one decoder for record() and deploy()
                if record:
                    self.data.write(events)            # persist everything we subscribed to
                for item in events["activity"]:
                    session._on_activity(item)

                batch = list(events["chart"])          # candles always ingested: pricing + marking
                if level1:
                    batch.extend(events["l1"])
                if level2:
                    batch.extend(events["l2"])
                if batch:
                    notify = bool((chart and events["chart"]) or (level1 and events["l1"])
                                  or (level2 and events["l2"]))
                    session.step(strategy, batch, notify=notify)
            except Exception:
                # The handler runs on the stream thread; letting an exception escape can silently
                # kill the reader loop, leaving a session that looks connected but is deaf.
                import traceback
                print("[live] handler error (session continues):")
                traceback.print_exc()

        self._streamer.start(handler, daemon=False)
        time.sleep(1.0)  # let the socket finish connecting before subscriptions are sent
        self.data.subscribe(self._streamer, session.tickers,
                            chart=True, level1=level1, level2=level2)  # chart always: fills need it
        self._streamer.send(self._streamer.account_activity("Account Activity", "0,1,2,3"))
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
