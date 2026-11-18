"""Trend-following bot for the make.fun BTC race (agent-fun/bars-replay/v1).

Strategy (long only, one position, minute bars of CBBTC/USDC):

  - Trend filter: an EMA(fast)/EMA(slow) pair over closes. The bot is flat
    unless the fast EMA sits above the slow EMA by a small separation
    buffer, the close is above the slow EMA, the slow EMA itself is rising
    over a slope lag, and a trailing return over a momentum lookback
    confirms the move. An exhaustion guard also blocks entries when the
    close is near the highest close of the last TOP_LOOKBACK bars.
    Entries therefore need a real move, not EMA geometry alone: a
    full-size round trip costs roughly 26-40 bps in fees, slippage and
    price impact, so the bot only trades moves that can pay for that
    several times over.
  - Sizing: two tranches (half the cash, then most of the rest once the
    trend has confirmed for a few more bars), each scaled down whenever
    the pool impact of the order (against the latest close's depth, the
    best guess at the next open's) would exceed an impact cap in bps.
  - Exits: EMA cross back down (trend reversal), a trailing stop under the
    position's peak marked value, or a hard stop under its cost (fees
    included). After an exit a cooldown — longer after a losing exit —
    keeps the bot out of the immediate whipsaw.
  - Risk guards: if equity ever falls a set share below its peak, the bot
    flattens and stays out for the rest of the window (the race guardrail
    is a 25% maximum drawdown; this guard trips far earlier). No new
    position is opened in the last bars of the 1,440-bar window.

Deterministic: every decision is a pure function of the bars and fills the
harness sends. No clock, no environment, no randomness.

Run: python3 -P -s main.py   (the vendored SDK in vendor/ speaks the wire)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

# --- strategy parameters (bars are one minute) ---
FAST_SPAN = 45          # fast EMA span
SLOW_SPAN = 150         # slow EMA span
SEP_BPS = 4             # min EMA separation, bps of the slow EMA, to enter
MOM_LOOKBACK = 60       # trailing-return lookback for the entry confirmation
MOM_BPS = 10            # min trailing return over MOM_LOOKBACK, in bps
SLOPE_LAG = 60          # the slow EMA must be rising over this many bars
SLOPE_BPS = 5           # min slow-EMA rise over SLOPE_LAG, in bps
TOP_LOOKBACK = 480      # exhaustion guard: no entries near this window's high
TOP_GUARD_BPS = 40      # stay out when the close is within this of that high
TRAIL_BPS = 90          # trailing stop: exit this far under the peak marked value
STOP_BPS = 80           # hard stop: exit this far under the position's cost
SIZE_FRAC_NUM = 1       # first tranche: SIZE_FRAC_NUM / SIZE_FRAC_DEN of cash
SIZE_FRAC_DEN = 2
ADD_FRAC_NUM = 4        # second tranche: ADD_FRAC_NUM / ADD_FRAC_DEN of the
ADD_FRAC_DEN = 5        # cash left after the first tranche (=> ~90% total)
ADD_DELAY_BARS = 10     # bars after the first fill before the second tranche
IMPACT_CAP_BPS = 10     # scale the order down so its pool impact stays <= this
COOLDOWN_BARS = 20      # bars to stay out after an exit (or a rejected buy)
COOLDOWN_LOSS_BARS = 60  # bars to stay out after an exit under water
DD_GUARD_BPS = 800      # flatten and halt if equity falls this far under its peak
WINDOW_BARS = 1440      # both windows are 1,440 one-minute bars (the rules)
END_GUARD_BARS = 30     # no new entries in the last this many bars
MAX_SLIPPAGE_BPS = 300  # bound on fill distance from the latest close

BPS = 10_000


class Bot:
    def __init__(self):
        self.closes = []            # every CBBTC close seen, in order
        self.ema_fast = None
        self.ema_slow = None
        self.slow_hist = []         # the slow EMA after every bar, in order
        self.peak_equity = None     # highest equity_quote_units seen
        self.halted = False         # drawdown guard tripped: stay out for good
        self.pending = None         # "buy" / "sell": an order sent last batch
        self.pending_qty = 0        # the position qty when that order was sent
        self.tranches = 0           # filled tranches in the current position
        self.cooldown_until = -1    # seq before which no new entry is allowed
        self.peak_value = None      # peak marked value of the open position
        self.entry_seq = None       # seq at which the open position was decided

    # -- indicator update, once per batch that carries a CBBTC bar --
    def _on_bar(self, close):
        self.closes.append(close)
        kf = 2.0 / (FAST_SPAN + 1)
        ks = 2.0 / (SLOW_SPAN + 1)
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
        else:
            self.ema_fast += (close - self.ema_fast) * kf
            self.ema_slow += (close - self.ema_slow) * ks
        self.slow_hist.append(self.ema_slow)

    def _bull(self):
        if self.ema_fast is None or len(self.closes) <= max(SLOW_SPAN, MOM_LOOKBACK):
            return False
        if self.ema_fast <= self.ema_slow:
            return False
        if (self.ema_fast - self.ema_slow) * BPS < SEP_BPS * self.ema_slow:
            return False
        close = self.closes[-1]
        if close <= self.ema_slow:
            return False
        if len(self.slow_hist) <= SLOPE_LAG:
            return False
        lagged = self.slow_hist[-1 - SLOPE_LAG]
        if (self.ema_slow - lagged) * BPS < SLOPE_BPS * lagged:
            return False
        then = self.closes[-1 - MOM_LOOKBACK]
        if (close - then) * BPS < MOM_BPS * then:
            return False
        if len(self.closes) > TOP_LOOKBACK:
            high = max(self.closes[-1 - TOP_LOOKBACK:-1])
            if (high - close) * BPS < TOP_GUARD_BPS * high:
                return False
        return True

    def _bear(self):
        return self.ema_fast is not None and self.ema_fast < self.ema_slow

    def on_batch(self, state, batch):
        seq = state.seq
        held = state.positions.get(SYMBOL)
        new_bar = any(b.get("symbol") == SYMBOL for b in batch["bars"])
        close = state.close(SYMBOL)
        if new_bar and close is not None:
            self._on_bar(close)

        # Equity peak and the drawdown guard run on every batch.
        if self.peak_equity is None or state.equity > self.peak_equity:
            self.peak_equity = state.equity
        dd_hit = (
            self.peak_equity is not None
            and state.equity * BPS <= self.peak_equity * (BPS - DD_GUARD_BPS)
        )
        if dd_hit:
            self.halted = True

        # Resolve the fate of the order sent on the previous batch: the
        # account in this batch already reflects its fill or rejection.
        qty_now = held.qty if held is not None else 0
        if self.pending == "buy":
            if qty_now > self.pending_qty:
                self.tranches += 1
                if self.entry_seq is None:
                    self.entry_seq = seq
            else:
                # A buy that added nothing was rejected: back off.
                self.cooldown_until = max(self.cooldown_until, seq + COOLDOWN_BARS)
            self.pending = None
        elif self.pending == "sell":
            # Filled (position gone) or rejected (still held; the exit
            # logic below simply fires again): either way, resolved.
            self.pending = None

        if held is None:
            self.peak_value = None
            self.entry_seq = None
            self.tranches = 0
        elif self.peak_value is None:
            self.peak_value = state.value(SYMBOL, held.qty)
        else:
            value = state.value(SYMBOL, held.qty)
            if value > self.peak_value:
                self.peak_value = value

        if state.warming_up or self.pending is not None:
            return []

        # --- exit an open position ---
        if held is not None:
            value = state.value(SYMBOL, held.qty)
            exit_now = self.halted or self._bear()
            if not exit_now and self.peak_value:
                exit_now = value * BPS <= self.peak_value * (BPS - TRAIL_BPS)
            if not exit_now:
                exit_now = value * BPS <= held.cost * (BPS - STOP_BPS)
            if exit_now:
                self.pending = "sell"
                self.pending_qty = held.qty
                cd = COOLDOWN_LOSS_BARS if value < held.cost else COOLDOWN_BARS
                self.cooldown_until = max(self.cooldown_until, seq + cd)
                return [{
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": held.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            # Second tranche: if the trend still confirms ADD_DELAY_BARS
            # after the first fill, average in toward the full size.
            if (self.tranches == 1 and self.entry_seq is not None
                    and seq - self.entry_seq >= ADD_DELAY_BARS
                    and seq < WINDOW_BARS - END_GUARD_BARS and self._bull()):
                order = self._buy_order(state, ADD_FRAC_NUM, ADD_FRAC_DEN)
                if order is not None:
                    self.pending = "buy"
                    self.pending_qty = held.qty
                    return [order]
            return []

        # --- enter a new position ---
        if self.halted or seq < self.cooldown_until:
            return []
        if seq >= WINDOW_BARS - END_GUARD_BARS:
            return []
        if close is None or not self._bull():
            return []
        order = self._buy_order(state, SIZE_FRAC_NUM, SIZE_FRAC_DEN)
        if order is None:
            return []
        self.pending = "buy"
        self.pending_qty = 0
        return [order]

    def _buy_order(self, state, num, den):
        """Size a buy at num/den of the cash on hand, capped by the room
        left under the position cap and scaled down so its constant-
        product impact against the latest depth stays under the cap."""
        spend = state.cash * num // den
        room = state.weight_room(SYMBOL)
        if room < spend:
            spend = room
        impact = state.impact_bps(SYMBOL, spend)
        if impact is not None and impact > IMPACT_CAP_BPS:
            spend = spend * IMPACT_CAP_BPS // impact
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if spend < min_notional or spend <= 0:
            return None
        return {
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": spend,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }


_BOT = Bot()


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
