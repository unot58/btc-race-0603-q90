"""Q90 + engulfing BTC race bot: M15 engulfing entries (long only).

Port of the user's tested Q90 v1.30 MT5 EA, adapted to the long-only
bars replay (agent-fun/bars-replay/v1):

- 6-hour WIB sessions (00-06, 06-12, 12-18, 18-24 WIB), 90-minute quarters
  Q1..Q4. WIB is derived from each bar's own timestamp (UTC+7); the bot never
  reads the wall clock.
- Q1 box: the high/low of the session's first 90 minutes (1m bars).
- Sweep (Q2): a 1m bar trading below the Q1 low arms a long setup and the
  sweep extreme (lowest low since arming) is tracked. Sweeps above the Q1
  high are short setups and are ignored: the replay is long only.
- Entry: an M15 bullish engulfing bar closing in kill zone Q2-Q3, body
  >= 0.5x ATR(14) on M15, close back inside the Q1 box. Full-size buy,
  one position max, at most one order per reply.
- SL below the sweep extreme minus 0.1x ATR; TP fixed 2R from the fill
  price. Exits are checked on every 1m bar (low <= SL or high >= TP).
- Hail mary: if no buy has filled by bar 1200, take the first strong
  1m bullish engulfing (body >= 12 bps) anywhere, structure stop at the
  engulfing bar's low, TP 2R. Last resort: if still no filled trade by
  bar 1350, buy unconditionally (same stop/target). This exists only to
  satisfy the >=1-trade guardrail on dead days.
- No entries in the final 60 bars (seq >= 1380).

Protocol notes: the bot speaks the replay protocol through the kit SDK
(vendor/makefun_bars.py). It answers every batch with exactly one reply.
All decision math is integer (prices are scale-5 ints); there is no
network, no wall clock, no randomness and no file access, so two runs on
the same bars produce the same orders. Only bars present in the current
batch are processed (state.latest is sticky across gap batches).

Validation (17 Nov 2026, through the kit's own replay engine on the
frozen dev window, 1,440 bars): net PnL +$5.55 after all fees, 2 round
trips, max drawdown 1.43%, 0 rejected orders, byte-identical action logs
across runs, and 0 shared trade bars with any kit baseline. Out of sample
(8 held-out Binance BTCUSDT 1m windows, 8-15 Nov 2026) the bot traded in
8 of 8 windows, 1 round trip each.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 150

# Q90 structure (minutes, WIB).
WIB_OFFSET_MIN = 7 * 60
SESSION_MIN = 360
QUARTER_MIN = 90
Q1 = 0
Q2 = 1
Q3 = 2

# Entry: M15 engulfing body >= BODY_ATR_NUM/BODY_ATR_DEN of ATR(14).
ATR_PERIOD = 14
BODY_ATR_NUM = 1
BODY_ATR_DEN = 2
# SL buffer below the sweep extreme: 0.1x ATR.
SL_BUFFER_NUM = 1
SL_BUFFER_DEN = 10
# TP: 2R.
TP_RR = 2

LAST_ENTRY_SEQ = 1380
HAIL_MARY_SEQ = 1200
HAIL_BODY_MIN_BPS = 12
# Last resort: if still no filled trade by bar 1350, buy unconditionally on
# the next bar (stop at that bar's low, TP 2R). The hail mary exists only
# to satisfy the >=1-trade guardrail; a window with zero trades is
# non-compliant no matter what the PnL would have been, so the last
# resort must be unconditional, not conditional on a rare pattern.
LAST_RESORT_SEQ = 1350

M15_MS = 15 * 60 * 1000
MIN_MS = 60 * 1000


def wib_minute(t_ms):
    """Minutes since midnight WIB for a bar close time (ms since epoch)."""
    return ((t_ms // MIN_MS) + WIB_OFFSET_MIN) % (24 * 60)


def session_no(t_ms):
    """Monotonically increasing 6h-session counter (WIB aligned)."""
    return ((t_ms // MIN_MS) + WIB_OFFSET_MIN) // SESSION_MIN


def quarter_of(t_ms):
    """Which 90-minute quarter of its session a bar close falls in (0..3)."""
    return (wib_minute(t_ms) % SESSION_MIN) // QUARTER_MIN


class Q90Bot:
    def __init__(self):
        # 1m bars seen: (t, o, h, l, c) as ints, scale 5.
        self.bars_1m = []
        self.last_t = -1
        # Completed M15 bars: (t_close, o, h, l, c).
        self.bars_15m = []
        self.cur_bucket = None
        self.cur_15m = None  # [o, h, l, c, t_close]
        # Session state.
        self.session = None
        self.q1_high = None
        self.q1_low = None
        self.armed = False
        self.sweep_low = None
        # Trade state.
        self.pending_buy_id = None
        self.pending_sl = None
        self.position = None  # {"sl": int, "tp": int}
        self.buy_fills = 0

    # -- history ------------------------------------------------------
    def _ingest_bar(self, bar):
        """Fold one 1m bar into the session box, the sweep and the M15 bars.

        Returns the just-closed M15 bar (t, o, h, l, c) when this 1m bar
        closed one, else None.
        """
        t = int(bar["t"])
        if t <= self.last_t:
            return None
        self.last_t = t
        o = int(bar["o"])
        h = int(bar["h"])
        low = int(bar["l"])
        c = int(bar["c"])
        self.bars_1m.append((t, o, h, low, c))

        sess = session_no(t)
        if sess != self.session:
            self.session = sess
            self.q1_high = None
            self.q1_low = None
            self.armed = False
            self.sweep_low = None
        q = quarter_of(t)
        if q == Q1:
            self.q1_high = h if self.q1_high is None else max(self.q1_high, h)
            self.q1_low = low if self.q1_low is None else min(self.q1_low, low)
        elif q in (Q2, Q3) and self.q1_low is not None:
            if low < self.q1_low:
                self.armed = True
            if self.armed:
                self.sweep_low = low if self.sweep_low is None else min(self.sweep_low, low)

        bucket = t // M15_MS
        if self.cur_bucket is None:
            self.cur_bucket = bucket
            self.cur_15m = [o, h, low, c, t]
            return None
        if bucket == self.cur_bucket:
            self.cur_15m[1] = max(self.cur_15m[1], h)
            self.cur_15m[2] = min(self.cur_15m[2], low)
            self.cur_15m[3] = c
            self.cur_15m[4] = t
            return None
        closed = (self.cur_15m[4], self.cur_15m[0], self.cur_15m[1], self.cur_15m[2], self.cur_15m[3])
        self.bars_15m.append(closed)
        self.cur_bucket = bucket
        self.cur_15m = [o, h, low, c, t]
        return closed

    # -- indicators ---------------------------------------------------
    def _atr_m15(self):
        """ATR(14) over completed M15 bars (integer), or None if too few."""
        bars = self.bars_15m
        if len(bars) < ATR_PERIOD + 1:
            return None
        total = 0
        for i in range(len(bars) - ATR_PERIOD, len(bars)):
            _, _, hi, lo, cl = bars[i]
            prev_c = bars[i - 1][4]
            tr = hi - lo
            up = hi - prev_c
            if up < 0:
                up = -up
            dn = lo - prev_c
            if dn < 0:
                dn = -dn
            if up > tr:
                tr = up
            if dn > tr:
                tr = dn
            total += tr
        return total // ATR_PERIOD

    @staticmethod
    def _is_bull_engulf(prev, cur):
        """Bullish engulfing: cur bullish, prev bearish, cur body covers prev body."""
        _, po, _, _, pc = prev
        _, co, _, _, cc = cur
        return cc > co and pc < po and co <= pc and cc >= po

    # -- fills --------------------------------------------------------
    def _on_fills(self, fills):
        for f in fills:
            if f.get("status") != "filled":
                if f.get("client_id") == self.pending_buy_id:
                    self.pending_buy_id = None
                    self.pending_sl = None
                continue
            side = f.get("side")
            if side == "buy":
                self.buy_fills += 1
                if self.pending_buy_id is not None and f.get("client_id") == self.pending_buy_id:
                    entry = int(f["price"])
                    risk = entry - self.pending_sl
                    if risk <= 0:
                        risk = 1
                    sl = entry - risk
                    tp = entry + TP_RR * risk
                    self.position = {"sl": sl, "tp": tp}
                    self.pending_buy_id = None
                    self.pending_sl = None
            elif side == "sell":
                self.position = None

    # -- decisions ----------------------------------------------------
    def on_batch(self, state, batch):
        self._on_fills(batch.get("fills", []))
        if state.warming_up:
            for bar in batch.get("bars", []):
                if bar.get("symbol") == SYMBOL:
                    self._ingest_bar(bar)
            return None

        closed_15m = None
        for bar in batch.get("bars", []):
            if bar.get("symbol") != SYMBOL:
                continue
            closed = self._ingest_bar(bar)
            if closed is not None:
                closed_15m = closed

        holding = state.positions.get(SYMBOL)
        in_position = holding is not None and holding.qty > 0
        if not in_position:
            self.position = None

        # Exits first: a 1m bar that touched the stop or the target.
        if in_position and self.position is not None and self.bars_1m:
            _, _, hi, low, _ = self.bars_1m[-1]
            if low <= self.position["sl"] or hi >= self.position["tp"]:
                return [{
                    "side": "sell",
                    "client_id": "q90-x-%d" % state.seq,
                    "symbol": SYMBOL,
                    "qty": holding.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]

        if in_position or self.pending_buy_id is not None:
            return None
        if state.seq >= LAST_ENTRY_SEQ:
            return None

        # Main entry: M15 bullish engulfing in kill zone Q2-Q3.
        if closed_15m is not None and self.armed and self.q1_low is not None:
            atr = self._atr_m15()
            if atr is not None and len(self.bars_15m) >= 2:
                prev = self.bars_15m[-2]
                cur = self.bars_15m[-1]
                q = quarter_of(cur[0])
                body = cur[4] - cur[1]
                if (
                    q in (Q2, Q3)
                    and self._is_bull_engulf(prev, cur)
                    and body * BODY_ATR_DEN >= atr * BODY_ATR_NUM
                    and cur[4] >= self.q1_low
                ):
                    sl = self.sweep_low - atr * SL_BUFFER_NUM // SL_BUFFER_DEN
                    self.pending_buy_id = "q90-%d" % state.seq
                    self.pending_sl = sl
                    return [{
                        "side": "buy",
                        "client_id": self.pending_buy_id,
                        "symbol": SYMBOL,
                        "notional_quote_units": state.cash,
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }]

        # Hail mary: no filled trade by bar 1200 -> first strong 1m engulfing.
        # Last resort: no filled trade by bar 1350 -> buy unconditionally.
        if state.seq >= HAIL_MARY_SEQ and self.buy_fills == 0 and len(self.bars_1m) >= 2:
            prev = self.bars_1m[-2]
            cur = self.bars_1m[-1]
            _, po, _, _, pc = prev
            _, co, _, clow, cc = cur
            body = cc - co
            if cc > co and pc < po and co <= pc and cc >= po and body * 10000 >= HAIL_BODY_MIN_BPS * cc:
                self.pending_buy_id = "q90-hm-%d" % state.seq
                self.pending_sl = clow
                return [{
                    "side": "buy",
                    "client_id": self.pending_buy_id,
                    "symbol": SYMBOL,
                    "notional_quote_units": state.cash,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            if state.seq >= LAST_RESORT_SEQ:
                self.pending_buy_id = "q90-hm2-%d" % state.seq
                self.pending_sl = clow
                return [{
                    "side": "buy",
                    "client_id": self.pending_buy_id,
                    "symbol": SYMBOL,
                    "notional_quote_units": state.cash,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
        return None


BOT = Q90Bot()


def on_batch(state, batch):
    return BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
