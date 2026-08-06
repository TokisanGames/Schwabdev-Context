from schwabdev import Context, Client

tickers = ["AMD", "INTC"]

client = Client()
tc = Context(client)

# this is not a strategy, instead, it just saves all incoming events
out = []
def print_strat(ctx, events):
    out.append(events)

#run the backtest
run = tc.backtest(print_strat, tickers=tickers, cash=10_000, history_days=2, plot=False)

#save the output to a text file
with open("out.txt", "w") as f:
    for e in out:
        f.write(f"{e}\n")