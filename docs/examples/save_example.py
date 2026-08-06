from schwabdev import Trader

tickers = ["AMD", "INTC"]

t = Trader()

# this is not a strategy, instead, it just saves all incoming events
out = []
def print_strat(ctx, events):
    out.append(events)

#run the backtest
run = t.backtest(print_strat, tickers=tickers, cash=10_000, history_days=60, plot=False)

#save the output to a text file
with open("out.txt", "w") as f:
    for e in out:
        f.write(f"{e}\n")