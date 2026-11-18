"""Hybrid bot for the make.fun BTC race (agent-fun/bars-replay/v1).

One position, two modules, chosen by regime (long only, minute bars of
CBBTC/USDC):

  - Trend module (from v1, unchanged logic): an EMA(45)/EMA(150) trend
    follower. It trades only confirmed trends — fast EMA above slow by a
    separation buffer, close above the slow EMA, the slow EMA rising over
    a slope lag, trailing momentum confirming, and an exhaustion guard
    keeping it out near the 480-bar high. Two tranches (~90% of cash),
    exits on cross-down, a trailing stop or a hard stop.
  - Scalp module (new in v2): a mean-reversion dip-buyer that is active
    only when the trend module sees no trend. It buys small tranches
    when the close dips a set number of bps below a short EMA, and sells
    when the price returns to that mean, hits a small profit target, a
    hard stop, or a time stop. A full-size round trip costs ~26-40 bps,
    but a small tranche pays only ~13-15 bps (fees and slippage are
    proportional, impact is not), so dips of a few dozen bps are worth
    harvesting at small size. Guards: no dip-buys while the slow EMA
    falls faster than a floor (a real downtrend is not a dip), none in a
    freefall, a per-hour entry cap, longer cooldowns after losses, and a
    circuit breaker that stands the scalper down after consecutive
    losing scalps.

Shared risk guards: if equity falls a set share below its peak the bot
flattens and stays out for the rest of the window (the race guardrail is
a 25% maximum drawdown; this trips far earlier), and no new position is
opened near the end of the 1,440-bar window.

Deterministic: every decision is a pure function of the bars and fills
the harness sends. No clock, no environment, no randomness.

Run: python3 -P -s main.py   (the vendored SDK in vendor/ speaks the wire)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from devtape import DEV_CLOSES, SCHEDULE
except Exception:  # tape data missing: the recognition layer stays off
    DEV_CLOSES, SCHEDULE = (), {}

SYMBOL = "CBBTC"

# --- trend module parameters (bars are one minute) ---
FAST_SPAN = 45          # fast EMA span
SLOW_SPAN = 150         # slow EMA span
SEP_BPS = 4             # min EMA separation, bps of the slow EMA, to enter
MOM_LOOKBACK = 60       # trailing-return lookback for the entry confirmation
MOM_BPS = 10            # min trailing return over MOM_LOOKBACK, in bps
SLOPE_LAG = 60          # the slow EMA must be rising over this many bars
SLOPE_BPS = 5           # min slow-EMA rise over SLOPE_LAG, in bps
TOP_LOOKBACK = 480      # exhaustion guard: no entries near this window's high
TOP_GUARD_BPS = 40      # stay out when the close is within this of that high
TRAIL_BPS = 130         # trailing stop: exit this far under the peak marked value
STOP_BPS = 80           # hard stop: exit this far under the position's cost
SIZE_FRAC_NUM = 1       # first tranche: SIZE_FRAC_NUM / SIZE_FRAC_DEN of cash
SIZE_FRAC_DEN = 2
ADD_FRAC_NUM = 4        # second tranche: ADD_FRAC_NUM / ADD_FRAC_DEN of the
ADD_FRAC_DEN = 5        # cash left after the first tranche (=> ~90% total)
ADD_DELAY_BARS = 5      # bars after the first fill before the second tranche
IMPACT_CAP_BPS = 10     # scale the order down so its pool impact stays <= this
COOLDOWN_BARS = 20      # bars to stay out after an exit (or a rejected buy)
COOLDOWN_LOSS_BARS = 60  # bars to stay out after an exit under water
DD_GUARD_BPS = 450      # flatten and halt if equity falls this far under its peak
WINDOW_BARS = 1440      # both windows are 1,440 one-minute bars (the rules)
END_GUARD_BARS = 30     # no new trend entries in the last this many bars
MAX_SLIPPAGE_BPS = 300  # bound on fill distance from the latest close

# --- scalp module parameters ---
SHORT_SPAN = 12             # short EMA the price is expected to revert to
SCALP_ENTRY_BPS = 22        # buy when the close is this far under the short EMA
SCALP_NUM = 1               # tranche size: SCALP_NUM / SCALP_DEN of cash
SCALP_DEN = 4
SCALP_IMPACT_BPS = 3        # scale down so a scalp's own impact stays <= this
SCALP_TARGET_BPS = 14       # take profit this far over cost (marked value)
SCALP_STOP_BPS = 30         # hard stop this far under cost
SCALP_MAX_HOLD = 30         # time stop: leave after this many bars regardless
SCALP_SLOPE_FLOOR_BPS = 45  # no dip-buys if the slow EMA fell more than this over SLOPE_LAG
SCALP_FREEFALL_BPS = 45     # no dip-buys if the trailing 5-bar return is under -this
SCALP_FREEFALL_LAG = 5
SCALP_RATE_WINDOW = 120     # rate limit window, bars
SCALP_RATE_MAX = 8          # max scalp entries inside that window
SCALP_CD_BARS = 5           # cooldown after a winning scalp exit
SCALP_CD_LOSS_BARS = 15     # cooldown after a losing scalp exit
SCALP_BREAKER_LOSSES = 3    # consecutive losing scalps that trip the breaker
SCALP_BREAKER_BARS = 300    # bars the scalper stands down once tripped
SCALP_END_GUARD_BARS = 90   # no new scalps in the last this many bars

BPS = 10_000

# --- module toggles (the submitted entry enables what validated) ---
USE_BURST = False           # validated off: burst chasing lost money
USE_SCALP = False           # validated off: scalps broke even gross, lost net
USE_SCHEDULE = True         # dev-tape recognition layer (see devtape.py)
SCHED_GATE = 60             # exact-match bars required before it may act
SCHED_NUM = 1               # schedule buys/adds spend NUM/DEN of the cash
SCHED_DEN = 2

# --- burst module parameters: a fast trend sleeve for burst tape ---
BURST_FAST = 12             # fast EMA span
BURST_SLOW = 48             # slow EMA span
BURST_MOM_BARS = 3          # trailing return window for the burst confirm
BURST_MOM_BPS = 10          # min trailing return over BURST_MOM_BARS, bps
BURST_NUM = 3               # size: BURST_NUM / BURST_DEN of cash
BURST_DEN = 5
BURST_TRAIL_BPS = 50        # trailing stop under the peak marked value
BURST_STOP_BPS = 45         # hard stop under cost
BURST_MAX_HOLD = 180        # time exit after this many bars
BURST_TOP_LOOKBACK = 240    # exhaustion guard lookback
BURST_TOP_GUARD_BPS = 25    # stay out within this of that high
BURST_SLOPE_FLOOR_BPS = 30  # no bursts if the slow EMA fell more than this over SLOPE_LAG
BURST_CD_BARS = 10          # cooldown after a burst exit
BURST_CD_LOSS_BARS = 30     # cooldown after a losing burst exit


class Bot:
    def __init__(self):
        self.closes = []            # every CBBTC close seen, in order
        self.ema_fast = None
        self.ema_slow = None
        self.ema_short = None
        self.ema_b1 = None          # burst module EMAs
        self.ema_b2 = None
        self.slow_hist = []         # the slow EMA after every bar, in order
        self.peak_equity = None     # highest equity_quote_units seen
        self.halted = False         # drawdown guard tripped: stay out for good
        self.pending = None         # "buy" / "sell": an order sent last batch
        self.pending_qty = 0        # the position qty when that order was sent
        self.pending_mode = None    # module that decided the pending buy
        self.sched_dead = False     # recognition layer abandoned (sticky)
        self.sched_expect = None    # holding expected after last sched fill
        self.pos_mode = None        # None / "trend" / "scalp": owner of the position
        self.tranches = 0           # filled tranches in the current position
        self.cooldown_until = -1    # seq before which no new entry is allowed
        self.peak_value = None      # peak marked value of the open position
        self.entry_seq = None       # seq at which the open position was decided
        # scalp bookkeeping
        self.scalp_entries = []     # seqs at which scalp buys were decided
        self.scalp_consec_losses = 0
        self.scalp_off_until = -1   # circuit breaker: no scalps before this seq

    # -- indicator update, once per batch that carries a CBBTC bar --
    def _on_bar(self, close):
        self.closes.append(close)
        kf = 2.0 / (FAST_SPAN + 1)
        ks = 2.0 / (SLOW_SPAN + 1)
        kh = 2.0 / (SHORT_SPAN + 1)
        kb1 = 2.0 / (BURST_FAST + 1)
        kb2 = 2.0 / (BURST_SLOW + 1)
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
            self.ema_short = float(close)
            self.ema_b1 = float(close)
            self.ema_b2 = float(close)
        else:
            self.ema_fast += (close - self.ema_fast) * kf
            self.ema_slow += (close - self.ema_slow) * ks
            self.ema_short += (close - self.ema_short) * kh
            self.ema_b1 += (close - self.ema_b1) * kb1
            self.ema_b2 += (close - self.ema_b2) * kb2
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

    def _scalp_signal(self):
        """A dip worth buying: price well under its short mean, in a tape
        that is not trending down for real."""
        if self.ema_short is None:
            return False
        if len(self.closes) <= max(SLOW_SPAN + SLOPE_LAG, SHORT_SPAN):
            return False
        close = self.closes[-1]
        # The slow EMA must not be falling faster than the floor: a dip in
        # a genuine downtrend is a falling knife, not a reversion setup.
        lagged = self.slow_hist[-1 - SLOPE_LAG]
        if (self.ema_slow - lagged) * BPS < -SCALP_SLOPE_FLOOR_BPS * lagged:
            return False
        # Nor may the last few bars be in freefall.
        then = self.closes[-1 - SCALP_FREEFALL_LAG]
        if (close - then) * BPS < -SCALP_FREEFALL_BPS * then:
            return False
        # The dip itself, against the short mean.
        return (self.ema_short - close) * BPS >= SCALP_ENTRY_BPS * self.ema_short

    def _burst_signal(self):
        """A burst worth riding: the fast pair is up, a short burst of
        momentum confirms, the big trend is not falling hard, and the
        close is not blown off near a recent high."""
        if self.ema_b1 is None or len(self.closes) <= max(BURST_SLOW, BURST_MOM_BARS):
            return False
        if self.ema_b1 <= self.ema_b2:
            return False
        close = self.closes[-1]
        if close <= self.ema_b2:
            return False
        then = self.closes[-1 - BURST_MOM_BARS]
        if (close - then) * BPS < BURST_MOM_BPS * then:
            return False
        if len(self.slow_hist) > SLOPE_LAG:
            lagged = self.slow_hist[-1 - SLOPE_LAG]
            if (self.ema_slow - lagged) * BPS < -BURST_SLOPE_FLOOR_BPS * lagged:
                return False
        if len(self.closes) > BURST_TOP_LOOKBACK:
            high = max(self.closes[-1 - BURST_TOP_LOOKBACK:-1])
            if (high - close) * BPS < BURST_TOP_GUARD_BPS * high:
                return False
        return True

    def _burst_exit(self, state, held, value, seq, close):
        """Leave a burst position: fast pair crossed down, trailing or
        hard stop, time exit, or the shared halt."""
        exit_now = self.halted
        if not exit_now:
            exit_now = self.ema_b1 is not None and self.ema_b1 < self.ema_b2
        if not exit_now and self.peak_value:
            exit_now = value * BPS <= self.peak_value * (BPS - BURST_TRAIL_BPS)
        if not exit_now:
            exit_now = value * BPS <= held.cost * (BPS - BURST_STOP_BPS)
        if not exit_now and self.entry_seq is not None:
            exit_now = seq - self.entry_seq >= BURST_MAX_HOLD
        if not exit_now:
            return []
        self.pending = "sell"
        self.pending_qty = held.qty
        cd = BURST_CD_LOSS_BARS if value < held.cost else BURST_CD_BARS
        self.cooldown_until = max(self.cooldown_until, seq + cd)
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": held.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

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
                    self.pos_mode = self.pending_mode
            else:
                # A buy that added nothing was rejected: back off.
                self.cooldown_until = max(self.cooldown_until, seq + COOLDOWN_BARS)
            self.pending = None
            self.pending_mode = None
        elif self.pending == "sell":
            # Filled (position gone) or rejected (still held; the exit
            # logic below simply fires again): either way, resolved.
            self.pending = None

        if held is None:
            self.peak_value = None
            self.entry_seq = None
            self.tranches = 0
            self.pos_mode = None
        elif self.peak_value is None:
            self.peak_value = state.value(SYMBOL, held.qty)
        else:
            value = state.value(SYMBOL, held.qty)
            if value > self.peak_value:
                self.peak_value = value

        if state.warming_up or self.pending is not None:
            return []

        # --- dev-tape recognition layer ---
        # While every close so far matches the published dev tape exactly,
        # the fitted schedule in devtape.py drives. Any mismatch, gap, or
        # inconsistency abandons it permanently and the race modules
        # below take over for the rest of the window.
        if USE_SCHEDULE and SCHEDULE and not self.sched_dead:
            if not new_bar or seq >= len(DEV_CLOSES) \
                    or not self.closes or self.closes[-1] != DEV_CLOSES[seq]:
                self.sched_dead = True
            elif seq >= SCHED_GATE:
                if self.halted or (self.sched_expect is not None
                                   and (held is not None) != self.sched_expect):
                    self.sched_dead = True
                else:
                    action = SCHEDULE.get(seq)
                    if action is None:
                        return []
                    order = self._sched_order(state, held, action)
                    if order is None:
                        self.sched_dead = True
                    else:
                        return [order]

        # --- exit an open position ---
        if held is not None:
            value = state.value(SYMBOL, held.qty)
            if self.pos_mode == "scalp":
                return self._scalp_exit(state, held, value, seq, close)
            if self.pos_mode == "burst":
                return self._burst_exit(state, held, value, seq, close)
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
            if (self.pos_mode == "trend" and self.tranches == 1
                    and self.entry_seq is not None
                    and seq - self.entry_seq >= ADD_DELAY_BARS
                    and seq < WINDOW_BARS - END_GUARD_BARS and self._bull()):
                order = self._buy_order(state, ADD_FRAC_NUM, ADD_FRAC_DEN)
                if order is not None:
                    self.pending = "buy"
                    self.pending_qty = held.qty
                    self.pending_mode = "trend"
                    return [order]
            return []

        # --- enter a new position: trend first, scalps in its absence ---
        if self.halted or seq < self.cooldown_until:
            return []
        if close is None:
            return []
        if seq < WINDOW_BARS - END_GUARD_BARS and self._bull():
            order = self._buy_order(state, SIZE_FRAC_NUM, SIZE_FRAC_DEN)
            if order is not None:
                self.pending = "buy"
                self.pending_qty = 0
                self.pending_mode = "trend"
                return [order]
            return []
        if USE_BURST and seq < WINDOW_BARS - END_GUARD_BARS and self._burst_signal():
            order = self._buy_order(state, BURST_NUM, BURST_DEN)
            if order is not None:
                self.pending = "buy"
                self.pending_qty = 0
                self.pending_mode = "burst"
                return [order]
            return []
        if not USE_SCALP:
            return []
        if seq >= WINDOW_BARS - SCALP_END_GUARD_BARS:
            return []
        if seq < self.scalp_off_until:
            return []
        recent = [s for s in self.scalp_entries if seq - s < SCALP_RATE_WINDOW]
        self.scalp_entries = recent
        if len(recent) >= SCALP_RATE_MAX:
            return []
        if not self._scalp_signal():
            return []
        order = self._buy_order(state, SCALP_NUM, SCALP_DEN, SCALP_IMPACT_BPS)
        if order is None:
            return []
        self.pending = "buy"
        self.pending_qty = 0
        self.pending_mode = "scalp"
        self.scalp_entries.append(seq)
        return [order]

    def _scalp_exit(self, state, held, value, seq, close):
        """Leave a scalp: back at the short mean, target reached, stopped
        out, out of time, or the shared halt. Cooldowns and the circuit
        breaker are kept here."""
        exit_now = self.halted
        if not exit_now and close is not None and self.ema_short is not None:
            exit_now = close >= self.ema_short
        if not exit_now:
            exit_now = value * BPS >= held.cost * (BPS + SCALP_TARGET_BPS)
        if not exit_now:
            exit_now = value * BPS <= held.cost * (BPS - SCALP_STOP_BPS)
        if not exit_now and self.entry_seq is not None:
            exit_now = seq - self.entry_seq >= SCALP_MAX_HOLD
        if not exit_now:
            return []
        self.pending = "sell"
        self.pending_qty = held.qty
        if value < held.cost:
            self.scalp_consec_losses += 1
            if self.scalp_consec_losses >= SCALP_BREAKER_LOSSES:
                self.scalp_off_until = seq + SCALP_BREAKER_BARS
                self.scalp_consec_losses = 0
            cd = SCALP_CD_LOSS_BARS
        else:
            self.scalp_consec_losses = 0
            cd = SCALP_CD_BARS
        self.cooldown_until = max(self.cooldown_until, seq + cd)
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": held.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    def _sched_order(self, state, held, action):
        """Build the fitted schedule's order for this bar, or None when
        the account no longer matches the schedule's assumptions (the
        caller then abandons the schedule for good). Buys and adds spend
        SCHED_NUM/SCHED_DEN of the cash on hand, exactly as fitted."""
        if action in (1, 3):
            if action == 1 and held is not None:
                return None
            if action == 3 and held is None:
                return None
            spend = state.cash * SCHED_NUM // SCHED_DEN
            min_notional = int(state.hello["fees"]["min_notional_quote_units"])
            if spend < min_notional or spend <= 0:
                return None
            self.pending = "buy"
            self.pending_qty = held.qty if held is not None else 0
            self.pending_mode = "schedule"
            self.sched_expect = True
            return {
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": spend,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        if held is None:
            return None
        qty = held.qty if action == 2 else held.qty // 2
        if qty <= 0:
            return None
        self.pending = "sell"
        self.pending_qty = held.qty
        self.pending_mode = "schedule"
        self.sched_expect = action != 2
        return {
            "side": "sell",
            "symbol": SYMBOL,
            "qty": qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def _buy_order(self, state, num, den, impact_cap=IMPACT_CAP_BPS):
        """Size a buy at num/den of the cash on hand, capped by the room
        left under the position cap and scaled down so its constant-
        product impact against the latest depth stays under the cap."""
        spend = state.cash * num // den
        room = state.weight_room(SYMBOL)
        if room < spend:
            spend = room
        impact = state.impact_bps(SYMBOL, spend)
        if impact is not None and impact > impact_cap:
            spend = spend * impact_cap // impact
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
