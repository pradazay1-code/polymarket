"""Analytics: backtest, edge scoring, and PnL for the stink-bid strategy.

All three share one assumption you should not forget: dry-run / backtest
fills are OPTIMISTIC. They assume our deep limit order is at the front of
the queue when the price prints through our bid, which is rarely true in
practice. Real-world fills will be a fraction of the simulated fills.
The output of every analytics function prints this caveat explicitly.
"""
