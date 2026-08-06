from schwabdev import Context, Client

client = Client()
tickers = ["AMD", "INTC"]

def order(symbol, instruction, quantity):
    """A minimal Schwab MARKET order dict."""
    return {"orderType": "MARKET", "session": "NORMAL", "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{"instruction": instruction, "quantity": quantity,
                                    "instrument": {"symbol": symbol, "assetType": "EQUITY"}}]}


class sma_Strategy:
    def __init__(self, tickers, fast=1, slow=4, slice_pct=0.10):  # windows in minute bars
        self.fast, self.slow = fast, slow
        self.slice_pct = slice_pct          # fraction of cash to spend per buy signal

    def __call__(self, tc, events):
        for e in events:
            if e["type"] != "c":            # act on candles only
                continue
            sym = e["symbol"]
            closes = [c["close"] for c in tc.candles[sym]]
            if len(closes) <= self.slow:    # not enough history for the slow SMA yet
                continue

            sma = lambda n, end: sum(closes[end - n:end]) / n
            fast_now,  slow_now  = sma(self.fast, len(closes)),     sma(self.slow, len(closes))
            fast_prev, slow_prev = sma(self.fast, len(closes) - 1), sma(self.slow, len(closes) - 1)
            tc.plots.setdefault(f"sma{self.fast} {sym}", []).append((e["time"], fast_now))
            tc.plots.setdefault(f"sma{self.slow} {sym}", []).append((e["time"], slow_now))

            price = closes[-1]
            if fast_prev <= slow_prev and fast_now > slow_now:      # crossed up -> buy a slice
                qty = int(tc.cash * self.slice_pct / price)
                if qty > 0:
                    tc.order(order(sym, "BUY", qty))
            elif fast_prev >= slow_prev and fast_now < slow_now:    # crossed down -> exit
                qty = int(tc.sellable(sym))
                if qty > 0:
                    tc.order(order(sym, "SELL", qty))


tc = Context(client)
strat = sma_Strategy(tickers=tickers)
run = tc.deploy(strat, tickers=tickers, cash=10_000, plot=True)