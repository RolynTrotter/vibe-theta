# vibe-theta

A paper-trading experiment in Early Retirement Now's far out-of-the-money SPX put
selling (0DTE at the open, overnight puts in the last hour), collateralized by
inherited AAPL that is gradually decumulated into SPY, with a T-bill sleeve sized to
absorb crash days.

Two portfolios run side by side and differ only in a System One gut check
([vibe-classification](https://github.com/RolynTrotter/vibe-classification)):
**A** trades only when the day feels ordinary, **B** always trades.

- `SKILL.md`: the runbook Claude follows for the morning, afternoon and close runs
- `scripts/theta.py`: the engine (planning, gating, paper fills, stop simulation,
  settlement, decumulation, approval staging)
- `scripts/pricing.py`: Black-Scholes, smile, fat-tailed realized-vol probabilities
- `dashboard/index.html`: the Theta Desk page (tickets, approvals, probability space)
- `tests/test_engine.py`: synthetic calm and crash days, end to end

Paper only. The approval path stages IBKR order *instructions*, which a person has to
submit in IBKR; test instructions buy one put at $0.05 so nothing risky can happen by
accident. Not investment or tax advice.
