Trader: a Schwabdev wrapper for backtesting and live trading.

    trader = Trader(client, account_hash="...")
    run = trader.backtest(my_strategy, ["AMD"], cash=10_000)   # returns a BacktestContext
    run.report()      # text report
    run.serve()       # interactive browser charts (run.plot() to serve and block)
    run.stats         # stats dict: each run is independent and savable

    # compare strategies/params freely; runs never clobber each other:
    runs = [trader.backtest(s, ["AMD"], report=False) for s in strategies]

A strategy is any callable `strategy(tc, events)`. The `tc` it receives is a Context (the "trader
context"). It exposes the actions a strategy needs, identically in backtest and
live, so the strategy can't tell the two apart:
    tc.order(order_dict)     place an order (simulated in backtest/paper, sent to Schwab when live)
    tc.cancel(order_id)      cancel a resting order (routed to Schwab when live)
    tc.cash                  spendable cash = settled cash − balance reserved by open buys
    tc.sellable(symbol)      shares free to sell = owned − committed to open sells
    tc.positions[symbol]     shares owned (partial fills included)
    tc.portfolio_value()     settled cash + marked-to-market positions
    tc.candles[symbol]       candle history so far; latest is candles[symbol][-1]
    tc.quotes[symbol]        latest level-1 quote {"bid","ask","last","bid_size","ask_size",...}
    tc.books[symbol]         latest level-2 snapshot {"bids": [...], "asks": [...], "time": ...}
    tc.plots[name]           overlay points [(time, value), ...] drawn on the price panel

Both backtest() and deploy() take chart/level1/level2 flags choosing which event types wake the
strategy; candles are always ingested since they track order fulfillment. Schwab has no l1/l2
history API, so those must be captured live:  `trader.data.record(tickers, level1=True,
level2=True, chart=True)` streams them into the DB (auto-starting with market hours), and a live
deploy records whatever it subscribes to by default. Backtests then replay the recordings.

All data:  cached candles, recorded l1/l2, and live recording:  goes through `trader.data` (a
Data instance), the only class that touches the DB.

Modules:
    trader   Trader:  config + orchestration (backtest / deploy)
    context  Context, BacktestContext, LiveContext:  the `tc` handed to the strategy, plus the Costs, find, dec order / fill / reservation engine
    data     Data:  the sole DB gateway (cache + recording)
    analysis compute_stats, trades, report, plot, serve:  read-only views of a run

## Trader

Trader: configuration plus orchestration for backtesting and live trading.

* The Trader holds the Schwab client, a `Data` store, and (for live) the account hash.
* `backtest()` builds and returns a fresh `BacktestContext` each call.
* `deploy()` opens a single `LiveContext` and wires the Schwab stream into it.
* The strategy never sees the Trader: it is handed a `Context` (referred to as "trader context").
* All data — cached candles, recorded level-1/level-2, and live recording — goes through `self.data`.

Both `backtest` and `deploy` take the same data-type flags: `chart` (minute candles), `level1`
(quotes), `level2` (order books). The flags choose which events wake the strategy; candles are
always ingested regardless, because they drive order fulfillment (backtest fills, MARKET pricing,
mark-to-market). Level-1/level-2 in a backtest replays whatever `Data.record()` (or a previous
live deploy) captured, since Schwab has no l1/l2 history API.

## Context

The trader context handed to a strategy — the `tc` in `strategy(tc, candles)`.

A `Context` is the strategy-facing surface AND the accounting engine: it owns the market data
(candles, plus the latest level-1 quotes and level-2 books when those streams are enabled), the
plots, and the settled cash / positions / order ledger, and exposes order / cancel / cash /
sellable / positions / portfolio_value.

The order lifecycle is split by the "shared in the parent, unique in the child" rule:
  * `Context`          -- the whole simulated lifecycle: `_parse` (validate + normalize), `_open`
                          (record the OPEN legs), `_settle_pending` (fill resting orders against
                          later candles) and `_fill` (the one settlement engine).
  * `BacktestContext`  -- `fill_delay=2`: MARKET orders fill at the open of the candle N bars
                          after placement, so an order can never settle on the candle that
                          triggered it (no same-bar look-ahead).
  * `LiveContext`      -- with a broker: orders are routed and fills arrive on the account
                          activity stream (`_on_activity`). With no broker (paper): the SAME
                          simulated lifecycle runs against the live candles, with `fill_delay=0`
                          because real time already moves forward — MARKET fills at once, LIMIT and
                          STOP rest until the market touches them.
So a strategy sees the same Context interface whether backtesting, paper trading or live, and can't
tell them apart: open buys reserve balance (see `cash`) and filled shares become sell-able
immediately. Every fill — backtest, live-paper, and real broker fills off the stream — flows
through the one `_fill` method, so cash / positions stay consistent everywhere.

Thread safety: a live session is mutated from the stream thread while the viewer's HTTP thread
reads it. Ledger mutations take `self._lock`, and readers should use `snapshot_orders()` rather
than walking `orders` directly.

## Data

"""Data: the single gateway to the candle store — the ONLY class that touches the DB.

Backtests read cached minute candles (and any recorded level-1 / level-2 events) from it; live
recording writes candles / quotes / order-book snapshots into it. One SQLite table per ticker and
data type:

    chart_{ticker} : minute candles      (time PRIMARY KEY, open, high, low, close, volume)
    l1_{ticker}    : level-one quotes    (time, bid, ask, last, bid_size, ask_size)
    l2_{ticker}    : order-book snapshots (time, bids TEXT(json), asks TEXT(json))

For candles, only the missing date ranges are fetched from Schwab (minute candles cap at
10 days/request, so gaps are pulled in 10-day chunks). Schwab has NO history API for l1/l2 — those
tables can only be populated live, via `record()` or a `Trader.deploy()` session (which records
whatever it subscribes to through the same `parse`/`write` pair). With no client the store runs
read-only, replaying purely from disk."""
