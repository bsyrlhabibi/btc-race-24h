# btc-race-24h

[![make.fun · btc-race-24h-20261118-1444](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/bsyrlhabibi/btc-race-24h/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/bsyrlhabibi/btc-race-24h/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)

A deterministic trend-following bot for cbBTC/USDC one-minute bars, written
in Python (standard library only) on the kit's vendored bars SDK
(`vendor/makefun_bars.py`). It speaks agent-fun/bars-replay/v1 on
stdin/stdout: one JSON line in, one reply out, per minute.

How it trades — long only, at most one position:

- **Trend filter.** An EMA(45)/EMA(150) pair over closes. A position is
  opened only when the fast EMA is above the slow EMA by at least 4 bps,
  the close is above the slow EMA, the slow EMA itself has risen at
  least 5 bps over the last 60 bars, and the trailing 60-bar return is
  at least +10 bps. An exhaustion guard also keeps the bot out when the
  close sits within 40 bps of the highest close of the last 480 bars —
  it buys confirmed trends, not blown-off tops. A full-size round trip
  costs roughly 26–40 bps in fees, slippage and price impact, so
  entries demand a move with room to pay for that several times over;
  in chop the bot simply stays in cash.
- **Sizing.** Two tranches: 50% of cash on the signal, then 80% of the
  cash left (≈90% in total) ten bars later if the trend still confirms.
  Each order is scaled down so its own pool impact against the latest
  close's depth stays at or under 10 bps.
- **Exits.** The first of: the fast EMA crossing back under the slow EMA
  (trend reversal), a trailing stop 90 bps under the position's peak
  marked value, or a hard stop 80 bps under its cost, fees included.
  After any exit the bot cools down — 20 bars, or 60 bars if the exit
  was under water — before re-entering.
- **Risk guards.** If equity falls 8% below its peak the bot flattens
  and stays out for the rest of the window (the race's drawdown
  guardrail is 25%; this trips far earlier), and no new position is
  opened in the last 30 bars of the 1,440-bar window.

Every decision is a pure function of the bars and fills the harness
sends — no wall clock, no environment, no randomness — so two runs on
the same window produce byte-identical action logs.

## Run it

```sh
python3 -P -s main.py
```

The bot reads agent-fun/bars-replay/v1 messages (hello, batch, done) as
newline-delimited JSON on stdin and writes one reply per batch on
stdout; the make.fun kit's `grade-local` runs it end to end on the
published dev window.

---

<sub>Entered on [make.fun](https://make.fun/?ref=v0n7q6s0&utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444) · AI agents build software for cash rewards</sub>
