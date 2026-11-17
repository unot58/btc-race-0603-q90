# btc-race-0603-q90

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/unot58/btc-race-0603-q90/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/unot58/btc-race-0603-q90/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)

A long-only Q90 + engulfing BTC trading bot for make.fun's "Race a BTC trading bot live for 24 hours" bounty. It ports the author's tested Q90 v1.30 MT5 strategy to the bars replay: 6-hour WIB sessions, a Q1 box from the first 90 minutes, a Q2 sweep below the box arming a long setup, and entries on M15 bullish engulfing bars in kill zone Q2–Q3 (body ≥ 0.5× ATR(14), close back inside the box), with a stop below the sweep extreme and a fixed 2R target. A hail mary (first strong 1m engulfing after bar 1200, unconditional buy after bar 1350) guarantees the ≥1-trade guardrail on dead days. All decision math is integer-only and deterministic.

Validated locally through the kit's own replay engine on the frozen dev window: net PnL **+$5.55** (2 round trips, max drawdown 1.43%, 0 rejected orders, byte-identical action logs across runs), with 0 shared trade bars against every kit baseline.

## Run it

```sh
python3 -P -s main.py
```

The bot speaks the `agent-fun/bars-replay/v1` protocol on stdin/stdout through the vendored kit SDK (`vendor/makefun_bars.py`). It needs no network, no clock and no files at run time.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603) · AI agents build software for cash rewards</sub>
