#!/usr/bin/env python3
"""
nyao_engine.py — an offline re-implementation of Nyao Scalper v43.0's decision
logic, used to back-test the EA on real broker M1/M5 data.

Why an offline engine instead of the MT5 Strategy Tester?
  * This sandbox has no Windows / MetaTrader 5 and outbound network is limited
    to a few hosts, so metaeditor64.exe / the Strategy Tester cannot run here.
  * The engine mirrors the MQL5 source for every code path that can change a
    back-test result: signal scoring, entry gating, dynamic lots, trailing /
    break-even / SL tightening, health-based exits, virtual-SL re-entry, the
    hedge chain, the basket stop and the equity guards.

Documented approximations:
  * Intrabar path: each bar is walked Open -> (Low -> High | High -> Low) ->
    Close; management runs on 4 "ticks" per bar, broker SL/TP are checked on
    that path (SL first = conservative when both sit inside one bar).
  * Fills at bar price +/- half the bar's spread; commission = 0 (the FTMO feed
    charges none on metals); no swap (scalps are intraday, and swap is a few
    cents per 0.01 lot).
  * News / trading-hours / market-close / leverage guards are inert — no
    calendar or session data offline (same as the MT5 tester, where
    CalendarValueHistory returns nothing).
  * Position management runs 4x per bar instead of every tick.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Parameters                                                                   #
# --------------------------------------------------------------------------- #

CODE_DEFAULTS = {
    "DirectionalBodyLookback": 10, "EMAFastPeriod": 5, "EMASlowPeriod": 12,
    "SlopeLookback": 3, "RSIPeriod": 8, "ATRPeriod": 8, "ATRAvgLookback": 10,
    "MinVolRatioToTrade": 0.6, "ImpulseLookback": 3, "ImpulseBoostWeight": 1.0,
    "SignalSmoothingCandles": 2, "CurrentCandleBlend": 0.4, "VelocityWindow": 2.0,
    "RSIOverbought": 80, "RSIOversold": 20, "RSIMomentumBuy": 60, "RSIMomentumSell": 40,
    "TrendWeight": 1.5, "SlopeWeight": 1.5, "MomentumBaseWeight": 1.0,
    "MomentumTriggerWeight": 0.5, "BodyMomentumWeight": 1.5,
    "ChopScoreHigh": 2.0, "ChopScoreMed": 1.0, "ChopScoreLow": 0.0,
    "VolatilityScoreHigh": 1.0, "VolatilityScoreLow": 0.0,
    "PeakScoreWeight": 1.0, "WickRejectionWeight": 1.0, "MinBodyRatio": 1.5,
    "EnableBuyOrders": True, "EnableSellOrders": True, "EnableNewBarEntryOnly": True,
    "EnableMaxSpreadFilter": True, "MaxSpreadPoints": 0.0, "MaxSpreadATRRatio": 0.25,
    "BaseLotSize": 0.01, "MaxOpenOrders": 8, "MaxTradesPerCandle": 1,
    "ConsecutiveCandleThresholdBoost": 1.0, "MaxConsecutiveCandleBoosts": 3,
    "ZonePoints": 500.0, "BuyDuplicateMultiplier": 1.5, "SellDuplicateMultiplier": 1.5,
    "MinBreakEvenProfit": 0.5, "ProfitThresholdMultiplier": 1.5,
    "LossThresholdMultiplier": 2.0, "MinBuySignalScore": 4.5, "MinSellSignalScore": 4.5,
    "EnableLimitEntry": False, "LimitEntryAnchor": 0, "LimitEntryATRFraction": 0.25,
    "LimitEntryExpiryBars": 1, "LimitEntryCancelOnFlip": True,
    "EnableSignalDampening": True, "MaxLosingPositionsSameDir": 2,
    "LosingPosScorePenalty": 1.5, "DrawdownThresholdPct": 3.0, "DrawdownScoreBoost": 2.0,
    "ConsecutiveLossesBeforeCooldown": 3, "ConsecutiveLossCooldownBars": 3,
    "EnableLossManagement": True, "MaxHoldingLossPositions": 2, "MinHealthScore": 0.4,
    "MaxAdverseATR": 1.5, "HealthTrendWeight": 0.4, "HealthRSIWeight": 0.25,
    "HealthATRWeight": 0.25, "HealthSwingWeight": 0.1, "HealthRSIBuyMin": 40.0,
    "HealthRSISellMax": 60.0, "HealthSwingLookback": 20, "HealthGraceBars": 2,
    "EnablePartialClose": True, "PartialClose75Pct": 0.25, "PartialClose50Pct": 0.5,
    "PartialClose25Pct": 1.0, "EnableHealthSLTightening": True,
    "SLTightenATRMultiplier": 2.0, "SLTightenMinHealthPct": 0.5,
    "EnableBreakEvenOnSpread": True, "BreakEvenSpreadMultiplier": 1.5,
    "EnableVirtualSLReentry": True, "ReentryRespectsNewBarGate": False,
    "ReentryMinSignalPct": 0.75, "EnableProfitOffsetSL": True,
    "ConsecutiveWinsRequired": 3, "MinOffsetProfit": 1.0,
    "EnableHedgeChain": True, "HedgeTriggerATR": 1.5, "HedgeRequireSignal": True,
    "HedgeMinSignalScore": 4.5, "HedgeAutoLot": True, "HedgeRecoveryATR": 1.0,
    "HedgeLotMultiplier": 2.0, "HedgeMaxLot": 0.1, "HedgeRecoveryPct": 110.0,
    "HedgeRollMinProfit": 0.5, "HedgeCycleLevels": 2, "EnableHedgeCycleReset": False,
    "HedgeCyclePartialPct": 50.0, "HedgeMaxCycles": 3, "HedgeMaxChainLossUSD": 0.0,
    "HedgeMaxChainLossPct": 0.0, "HedgeClearRootSL": True, "HedgeTrailATR": 0.5,
    "EnableDynamicLots": True, "EquityDropPercent": 5.0, "MaxEquityDropLotSteps": 2,
    "MinSignalStrengthForLot": 8.0, "LotStepSize": 0.01, "MaxLotSize": 0.05,
    "EnableBasketStop": True, "MaxBasketLossPct": 8.0, "MinEquityPercent": 70.0,
    "MaxDrawdownFromPeak": 0.0, "PauseMinutes": 5, "PauseMinutesMultiplier": 1.5,
    "MaxPauseMinutes": 120, "MaxMinEquityTriggers": 0, "ResetOnNewPeak": True,
    "TargetEquity": 0.0, "MinimumEquity": 20.0,
    "EnableTakeProfit": False, "TPInputType": 0, "TPValue": 10.0,
    "EnableStopLoss": True, "SLInputType": 1, "SLValue": 10.0,
    "EnableRiskReward": False, "RRRiskMode": 1, "RRRiskInputType": 2,
    "RRRiskValue": 200.0, "RRAtrMultiplier": 1.5, "RiskRewardRatio": 1.5,
    "EnableTrailing": True, "TrailingEnableBreakEvenLock": True,
    "TrailingSLOnProfitableOnly": True, "EnableAdaptiveTP": True, "EnableAdaptiveSL": True,
    "TSInputType": 0, "TrailingDistanceValue": 0.2, "TrailingValueMultiplier": 0.2,
    "MagicNumber": 6926268,
}

INPUT_DOLLAR, INPUT_PERCENT, INPUT_POINTS = 0, 1, 2
RR_RISK_MANUAL, RR_RISK_ATR = 0, 1


def load_set_file(path: str) -> dict:
    out = {}
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(";") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if val.lower() in ("true", "false"):
                out[key] = val.lower() == "true"
            else:
                try:
                    out[key] = float(val) if "." in val else int(val)
                except ValueError:
                    out[key] = val
    return out


class P:
    """Parameter accessor: profile values on top of the EA's code defaults."""

    def __init__(self, overrides: dict | None = None):
        self._d = dict(CODE_DEFAULTS)
        self._d.update(overrides or {})

    def __getattr__(self, item):
        d = object.__getattribute__(self, "_d")
        if item in d:
            return d[item]
        raise AttributeError(item)


PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "settings")
PROFILE_FILES = {
    "aggressive": "aggressive.set",
    "safe-aggressive": "safe-aggressive.set",
    "default": "default.set",
    "balanced": "balanced.set",
    "safe": "safe.set",
}


def load_profile(name: str, overrides: dict | None = None) -> P:
    vals = load_set_file(os.path.join(PROFILE_DIR, PROFILE_FILES[name]))
    if overrides:
        vals.update(overrides)
    return P(vals)


# --------------------------------------------------------------------------- #
# Result containers                                                            #
# --------------------------------------------------------------------------- #

@dataclass
class Trade:
    ticket: int
    direction: str
    lot: float
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    profit: float
    reason: str
    entry_score: float
    bars_held: int


@dataclass
class Result:
    profile: str
    data: str
    start: str
    end: str
    initial_balance: float
    final_balance: float
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #

class NyaoEngine:
    def __init__(self, df, meta: dict, params: P, initial_balance: float = 1000.0,
                 leverage: float = 100.0, label: str = "run", slippage: float = 0.0,
                 spread_mult: float = 1.0, path_mode: str = "lh"):
        self.p = params
        self.meta = meta
        self.label = label
        self.balance0 = float(initial_balance)
        self.balance = float(initial_balance)
        self.leverage = float(leverage)

        self.point = float(meta["point"])
        self.digits = int(meta["digits"])
        self.contract = float(meta.get("contract_size", 100.0))
        self.tick_size = self.point
        self.tick_value = self.contract * self.point
        self.vol_min = float(meta.get("volume_min", 0.01))
        self.vol_max = float(meta.get("volume_max", 100.0))
        self.vol_step = float(meta.get("volume_step", 0.01))

        # research knobs (not EA inputs): adverse price offset on stop fills and a
        # multiplier on the modelled spread, used for cost-sensitivity tests.
        self.slippage = float(slippage)
        self.path_mode = path_mode
        self.spread_mult = float(spread_mult)
        self.dt = list(df["datetime"])
        self.o = [float(x) for x in df["open"]]
        self.h = [float(x) for x in df["high"]]
        self.l = [float(x) for x in df["low"]]
        self.c = [float(x) for x in df["close"]]
        self.sp = [float(x) * float(spread_mult) for x in df["spread"]]
        self.n = len(self.c)
        self.dt_epoch = [int(t.timestamp()) for t in self.dt]

        self.trade_start_idx = 0
        if meta.get("trade_start"):
            ts = str(meta["trade_start"])
            self.trade_start_idx = next(
                (i for i, t in enumerate(self.dt) if str(t) >= ts), 0)

        self._precompute_indicators()

        self.positions = []
        self.trades = []
        self.next_ticket = 1
        self.peak_equity = self.balance
        self.last_peak_equity = self.balance
        self.min_equity_triggers = 0
        self.min_equity_triggers_exceeded = False
        self.minimum_equity_reached = False
        self.target_equity_reached = False
        self.is_paused = False
        self.pause_until = 0.0
        self.pause_duration_min = 0
        self.consecutive_loss_count = 0
        self.cooldown_until_epoch = 0
        self.buys_on_bar = 0
        self.sells_on_bar = 0
        self.consec_buy_candles = 0
        self.consec_sell_candles = 0
        self.last_buy_price = 0.0
        self.last_sell_price = 0.0
        self.entry_bar_idx = -1
        self.period_seconds = 60
        self.max_positions_open = 0
        self.pos_open_sum = 0.0
        self.bars_counted = 0
        self.cur_bar = 0
        self.cur_price = self.cur_bid = self.cur_ask = 0.0
        self.cur_spread = 0.0
        self.cur_o = self.cur_h = self.cur_l = 0.0
        self._score_cache = {}
        self._cache_key = None
        self.diag = {
            "signals_buy": 0, "signals_sell": 0, "orders_opened": 0,
            "blocked_spread": 0, "blocked_score": 0, "blocked_dampen": 0,
            "blocked_max_loss_pos": 0, "blocked_max_orders": 0, "blocked_duplicate": 0,
            "blocked_cooldown": 0, "blocked_paused": 0, "blocked_percandle": 0,
            "blocked_opposite_on_bar": 0, "dead_market_zones": 0,
            "exits_sl": 0, "exits_health": 0, "exits_basket": 0, "exits_decay": 0,
            "exits_hedge": 0, "reentries": 0, "hedge_starts": 0, "hedge_rolls": 0,
            "hedge_covers": 0, "hedge_reseeds": 0, "hedge_releases": 0,
            "spread_cost_paid": 0.0, "equity_pauses": 0, "basket_pauses": 0,
            "partial_closes": 0, "max_lot_used": 0.0,
        }
        self.equity_curve = []

    # ------------------------------------------------------------------ #
    # Indicators                                                           #
    # ------------------------------------------------------------------ #
    def _precompute_indicators(self):
        p = self.p
        n = self.n
        pf, ps, pr, pa = (max(1, int(p.EMAFastPeriod)), max(1, int(p.EMASlowPeriod)),
                          max(2, int(p.RSIPeriod)), max(1, int(p.ATRPeriod)))
        af, as_ = 2.0 / (pf + 1), 2.0 / (ps + 1)
        ema_f, ema_s, rsi, atr = [0.0] * n, [0.0] * n, [50.0] * n, [0.0] * n
        avg_gain, avg_loss = [0.0] * n, [0.0] * n
        body = [abs(self.c[i] - self.o[i]) for i in range(n)]
        rng = [self.h[i] - self.l[i] for i in range(n)]
        pref_body, pref_range = [0.0] * (n + 1), [0.0] * (n + 1)
        c, h, l = self.c, self.h, self.l

        ema_f[0] = ema_s[0] = c[0]
        gain_sum = loss_sum = tr_sum = 0.0
        for i in range(1, n):
            ema_f[i] = af * c[i] + (1 - af) * ema_f[i - 1]
            ema_s[i] = as_ * c[i] + (1 - as_) * ema_s[i - 1]
            chg = c[i] - c[i - 1]
            up = chg if chg > 0 else 0.0
            dn = -chg if chg < 0 else 0.0
            if i <= pr:
                gain_sum += up
                loss_sum += dn
                ag, al = gain_sum / pr, loss_sum / pr
            else:
                ag = (avg_gain[i - 1] * (pr - 1) + up) / pr
                al = (avg_loss[i - 1] * (pr - 1) + dn) / pr
            avg_gain[i], avg_loss[i] = ag, al
            rsi[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
            tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
            if i <= pa:
                tr_sum += tr
                atr[i] = tr_sum / pa
            else:
                atr[i] = (atr[i - 1] * (pa - 1) + tr) / pa
        for i in range(n):
            pref_body[i + 1] = pref_body[i] + body[i]
            pref_range[i + 1] = pref_range[i] + rng[i]
        pref_atr = [0.0] * (n + 1)
        for i in range(n):
            pref_atr[i + 1] = pref_atr[i] + atr[i]

        self.ema_f, self.ema_s, self.rsi, self.atr = ema_f, ema_s, rsi, atr
        self.avg_gain, self.avg_loss = avg_gain, avg_loss
        self.pref_body, self.pref_range, self.pref_atr = pref_body, pref_range, pref_atr
        self._alpha_f, self._alpha_s = af, as_
        self._rsi_p, self._atr_p = pr, pa
        self.needed = max(int(p.ImpulseLookback), int(p.DirectionalBodyLookback),
                          int(p.ATRAvgLookback)) + 5
        self.slope_bars = max(1, int(p.SlopeLookback))
        self.min_history = self.needed + max(int(p.SignalSmoothingCandles), 10) + self.slope_bars + 3

    # --- indicator values at (bar b, index offset k) ---------------------- #
    def _ema_f(self, b, k):
        if k == 0:
            return self._alpha_f * self.cur_price + (1 - self._alpha_f) * self.ema_f[b - 1]
        return self.ema_f[b - k]

    def _ema_s(self, b, k):
        if k == 0:
            return self._alpha_s * self.cur_price + (1 - self._alpha_s) * self.ema_s[b - 1]
        return self.ema_s[b - k]

    def _rsi(self, b, k):
        if k != 0:
            return self.rsi[b - k]
        pr = self._rsi_p
        chg = self.cur_price - self.c[b - 1]
        up = chg if chg > 0 else 0.0
        dn = -chg if chg < 0 else 0.0
        ag = (self.avg_gain[b - 1] * (pr - 1) + up) / pr
        al = (self.avg_loss[b - 1] * (pr - 1) + dn) / pr
        return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    def _atr(self, b, k):
        if k != 0:
            return self.atr[b - k]
        pa = self._atr_p
        tr = abs(self.cur_price - self.c[b - 1])
        return (self.atr[b - 1] * (pa - 1) + tr) / pa

    # ------------------------------------------------------------------ #
    # ComputeRawScore()                                                    #
    # ------------------------------------------------------------------ #
    def raw_score(self, is_buy: bool, b: int, k: int) -> float:
        p = self.p
        trend = 0.0
        emaFast = self._ema_f(b, k)
        emaSlow = self._ema_s(b, k)
        emaFastPrev = self._ema_f(b, k + self.slope_bars)
        if (emaFast > emaSlow) if is_buy else (emaFast < emaSlow):
            trend += p.TrendWeight
        if (emaFast > emaFastPrev) if is_buy else (emaFast < emaFastPrev):
            trend += p.SlopeWeight
        if trend > 3.0:
            trend = 3.0

        if k == 0:
            o0, h0, l0, c0 = self.cur_o, self.cur_h, self.cur_l, self.cur_price
        else:
            i = b - k
            o0, h0, l0, c0 = self.o[i], self.h[i], self.l[i], self.c[i]
        currentBody = abs(c0 - o0)
        look = int(p.DirectionalBodyLookback)
        sumBody = self.pref_body[b - k] - self.pref_body[b - k - look]
        avgBody = sumBody / look if look else currentBody

        rsi = self._rsi(b, k)
        mom = 0.0
        if is_buy:
            if 50 < rsi < p.RSIOverbought:
                mom += p.MomentumBaseWeight
            if rsi > p.RSIMomentumBuy:
                mom += p.MomentumTriggerWeight
        else:
            if p.RSIOversold < rsi < 50:
                mom += p.MomentumBaseWeight
            if rsi < p.RSIMomentumSell:
                mom += p.MomentumTriggerWeight
        if currentBody > avgBody:
            mom += p.BodyMomentumWeight

        bodyAccel = min(3.0, currentBody / avgBody) if avgBody > 0 else 0.0
        curRange = h0 - l0
        sumRange = self.pref_range[b - k] - self.pref_range[b - k - look]
        avgRange = sumRange / look if look else curRange
        rangeAccel = min(3.0, curRange / avgRange) if avgRange > 0 else 0.0

        look_imp = int(p.ImpulseLookback)
        sameDir = 0
        for i in range(look_imp):
            if k + i == 0:
                bc, bo = self.cur_price, self.cur_o
            else:
                j = b - k - i
                bc, bo = self.c[j], self.o[j]
            if (is_buy and bc > bo) or ((not is_buy) and bc < bo):
                sameDir += 1
            else:
                break
        continuity = min(1.0, sameDir / look_imp) if look_imp else 0.0
        impulse = (0.5 * bodyAccel + 0.3 * rangeAccel + 0.2 * continuity) / 2.0
        impulse = min(1.0, max(0.0, impulse))
        mom = mom * (1.0 + p.ImpulseBoostWeight * impulse)
        if mom > 3.0:
            mom = 3.0

        lookA = int(p.ATRAvgLookback)
        if k == 0:
            curATR = self._atr(b, 0)
            avgATR = (curATR + (self.pref_atr[b] - self.pref_atr[b - lookA + 1])) / lookA
        else:
            curATR = self.atr[b - k]
            avgATR = (self.pref_atr[b - k + 1] - self.pref_atr[b - k - lookA + 1]) / lookA
        volRatio = curATR / avgATR if avgATR > 0 else 0.0
        if p.MinVolRatioToTrade > 0 and 0 < volRatio < p.MinVolRatioToTrade:
            return 0.0

        if volRatio > 1.0:
            chop = min(p.ChopScoreHigh, 2.0)
        elif volRatio > 0.8:
            chop = min(p.ChopScoreMed, 2.0)
        else:
            chop = min(p.ChopScoreLow, 2.0)
        vol = p.VolatilityScoreHigh if volRatio > 1.2 else p.VolatilityScoreLow

        j1 = b - k - 1
        localExtreme = self.h[j1] if is_buy else self.l[j1]
        for i in range(2, 6):
            j = b - k - i
            localExtreme = max(localExtreme, self.h[j]) if is_buy else min(localExtreme, self.l[j])
        peak = p.PeakScoreWeight if ((c0 > localExtreme) if is_buy else (c0 < localExtreme)) else 0.0

        maxOC, minOC = max(o0, c0), min(o0, c0)
        upperWick, lowerWick = h0 - maxOC, minOC - l0
        safeBody = max(currentBody, avgBody * p.MinBodyRatio)
        penalty = ((upperWick if is_buy else lowerWick) / safeBody) * p.WickRejectionWeight \
            if safeBody > 0 else 0.0

        score = trend + mom + chop + peak + vol - penalty
        return min(10.0, max(0.0, score))

    def prepare_bar(self, b: int):
        """Closed-bar weighted average (the part of GetSignalStrength that is
        constant while bar b forms) for both directions."""
        p = self.p
        N = int(min(10, max(1, p.SignalSmoothingCandles)))
        self._N = N
        self._blend = min(1.0, max(0.0, p.CurrentCandleBlend))
        base = {}
        for is_buy in (True, False):
            ws = wt = 0.0
            for i in range(1, N + 1):
                w = float(N - i + 1)
                ws += self.raw_score(is_buy, b, i) * w
                wt += w
            base[is_buy] = ws / wt if wt else 0.0
        self._base = base
        self._score_cache = {}

    def score_now(self, is_buy: bool) -> float:
        """GetSignalStrength() with the live forming bar (cached per price step)."""
        cached = self._score_cache.get(is_buy)
        if cached is not None:
            return cached
        cur = self.raw_score(is_buy, self.cur_bar, 0)
        base = self._base[is_buy]
        s = base * (1 - self._blend) + cur * self._blend
        s = min(10.0, max(0.0, s))
        self._score_cache[is_buy] = s
        return s

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #
    def convert_to_points(self, input_type: int, value: float, lot: float) -> float:
        if input_type == INPUT_POINTS:
            return value
        if lot <= 0 or self.tick_value <= 0 or self.tick_size <= 0 or self.point <= 0:
            return 0.0
        value_per_point = (self.tick_value * lot) / (self.tick_size / self.point)
        if value_per_point <= 0:
            return 0.0
        if input_type == INPUT_DOLLAR:
            return value / value_per_point
        if input_type == INPUT_PERCENT:
            return (self.equity() * value / 100.0) / value_per_point
        return 0.0

    def sl_points(self, lot: float) -> float:
        p = self.p
        if p.EnableRiskReward:
            if p.RRRiskMode == RR_RISK_ATR:
                atr = self.atr[max(0, self.cur_bar - 1)]
                return (atr / self.point) * p.RRAtrMultiplier if atr > 0 else 0.0
            return self.convert_to_points(int(p.RRRiskInputType), p.RRRiskValue, lot)
        return self.convert_to_points(int(p.SLInputType), p.SLValue, lot) if p.EnableStopLoss else 0.0

    def tp_points(self, lot: float) -> float:
        p = self.p
        if p.EnableRiskReward:
            slp = self.sl_points(lot)
            return slp * p.RiskRewardRatio if slp > 0 and p.RiskRewardRatio > 0 else 0.0
        return self.convert_to_points(int(p.TPInputType), p.TPValue, lot) if p.EnableTakeProfit else 0.0

    def normalize_volume(self, v: float) -> float:
        v = max(self.vol_min, min(self.vol_max, v))
        step = self.vol_step if self.vol_step > 0 else 0.01
        return round(round(v / step) * step, 8)

    def equity(self) -> float:
        return self.balance + sum(pos["pl"] for pos in self.positions)

    def floating_pl(self, skip_chains: bool = False) -> float:
        return sum(pos["pl"] for pos in self.positions
                   if not (skip_chains and pos["chain_id"]))

    def count_losing(self) -> int:
        return sum(1 for pos in self.positions if pos["pl"] < 0)

    def losing_same_dir(self, is_buy: bool) -> int:
        return sum(1 for pos in self.positions if pos["is_buy"] == is_buy and pos["pl"] < 0)

    def _tstr(self, i: int) -> str:
        return str(self.dt[i])

    # ------------------------------------------------------------------ #
    # Position accounting                                                  #
    # ------------------------------------------------------------------ #
    def _open_position(self, is_buy: bool, price: float, lot: float, score: float,
                       reason: str = "signal", chain: dict | None = None,
                       naked: bool = False):
        pos = dict(ticket=self.next_ticket, is_buy=is_buy, lot=lot, entry=price,
                   entry_bar=self.cur_bar, entry_time=self._tstr(self.cur_bar),
                   score0=score, sl=0.0, tp=0.0, be_locked=False, partial_level=0,
                   pl=0.0, offset_wins=0, offset_accum=0.0, offset_orig_sl=0.0,
                   chain_id=0, hedge_level=0, chain_anchor_loss=0.0, cycle_num=0,
                   no_rehedge=False, hedge_graduated=False, hedge_lock_profit=0.0,
                   open_reason=reason)
        self.next_ticket += 1
        slp = 0.0 if naked else self.sl_points(lot)
        tpp = 0.0 if naked else self.tp_points(lot)
        if slp > 0:
            pos["sl"] = price - slp * self.point if is_buy else price + slp * self.point
        if tpp > 0:
            pos["tp"] = price + tpp * self.point if is_buy else price - tpp * self.point
        pos["offset_orig_sl"] = pos["sl"]
        if chain:
            pos.update(chain)
        self.positions.append(pos)
        self.diag["orders_opened"] += 1
        self.diag["max_lot_used"] = max(self.diag["max_lot_used"], lot)
        if is_buy:
            self.buys_on_bar += 1
            self.last_buy_price = price
        else:
            self.sells_on_bar += 1
            self.last_sell_price = price
        return pos

    def _close_position(self, pos, price: float, reason: str):
        gross = (price - pos["entry"]) * self.contract * pos["lot"] if pos["is_buy"] \
            else (pos["entry"] - price) * self.contract * pos["lot"]
        self.balance += gross
        self.diag["spread_cost_paid"] += pos["lot"] * self.contract * self.cur_spread
        self.trades.append(Trade(
            ticket=pos["ticket"], direction="BUY" if pos["is_buy"] else "SELL", lot=pos["lot"],
            entry_time=pos["entry_time"], entry_price=round(pos["entry"], 5),
            exit_time=self._tstr(self.cur_bar), exit_price=round(price, 5),
            profit=round(gross, 4), reason=reason, entry_score=round(pos["score0"], 2),
            bars_held=self.cur_bar - pos["entry_bar"]))
        self.positions.remove(pos)
        p = self.p
        if p.EnableSignalDampening:
            if gross < 0:
                self.consecutive_loss_count += 1
                cl = int(p.ConsecutiveLossesBeforeCooldown)
                if cl > 0 and self.consecutive_loss_count >= cl:
                    self.cooldown_until_epoch = self.dt_epoch[self.cur_bar] + \
                        int(p.ConsecutiveLossCooldownBars) * self.period_seconds
            else:
                self.consecutive_loss_count = 0
        if p.EnableProfitOffsetSL:
            for other in self.positions:
                if other["pl"] >= 0:
                    continue
                if gross > 0:
                    other["offset_wins"] += 1
                    other["offset_accum"] += gross
                else:
                    other["offset_wins"] = 0
                    other["offset_accum"] = 0.0

    def _partial_close(self, pos, volume: float, reason: str) -> bool:
        if volume < self.vol_min or (pos["lot"] - volume) < self.vol_min:
            return False
        price = self.cur_bid if pos["is_buy"] else self.cur_ask
        gross = (price - pos["entry"]) * self.contract * volume if pos["is_buy"] \
            else (pos["entry"] - price) * self.contract * volume
        self.balance += gross
        self.diag["spread_cost_paid"] += volume * self.contract * self.cur_spread
        pos["lot"] = round(pos["lot"] - volume, 8)
        self.trades.append(Trade(
            ticket=pos["ticket"], direction="BUY" if pos["is_buy"] else "SELL", lot=volume,
            entry_time=pos["entry_time"], entry_price=round(pos["entry"], 5),
            exit_time=self._tstr(self.cur_bar), exit_price=round(price, 5),
            profit=round(gross, 4), reason=reason, entry_score=round(pos["score0"], 2),
            bars_held=self.cur_bar - pos["entry_bar"]))
        self.diag["partial_closes"] += 1
        return True

    # ------------------------------------------------------------------ #
    # Entry gates                                                          #
    # ------------------------------------------------------------------ #
    def _entry_conditions(self, is_buy: bool, price: float) -> bool:
        p = self.p
        same_on_bar = self.buys_on_bar if is_buy else self.sells_on_bar
        opp_on_bar = self.sells_on_bar if is_buy else self.buys_on_bar
        if p.MaxTradesPerCandle > 0 and same_on_bar >= p.MaxTradesPerCandle:
            self.diag["blocked_percandle"] += 1
            return False
        if opp_on_bar > 0:
            self.diag["blocked_opposite_on_bar"] += 1
            return False
        if p.EnableSignalDampening:
            if self.losing_same_dir(is_buy) >= p.MaxLosingPositionsSameDir:
                self.diag["blocked_dampen"] += 1
                return False
            if self.cooldown_until_epoch > 0:
                if self.dt_epoch[self.cur_bar] < self.cooldown_until_epoch:
                    self.diag["blocked_cooldown"] += 1
                    return False
                self.cooldown_until_epoch = 0
        live = [pos for pos in self.positions if pos["is_buy"] == is_buy]
        if live:
            newest = max(live, key=lambda x: x["ticket"])
            mult = p.BuyDuplicateMultiplier if is_buy else p.SellDuplicateMultiplier
            min_dist = p.ZonePoints * self.point * mult
            if abs(price - newest["entry"]) < min_dist:
                self.diag["blocked_duplicate"] += 1
                return False
        return True

    def _spread_too_wide(self) -> bool:
        p = self.p
        if not p.EnableMaxSpreadFilter:
            return False
        spread_points = self.cur_spread / self.point
        cap = p.MaxSpreadPoints
        if cap <= 0:
            atr_points = self._atr(self.cur_bar, 0) / self.point if self.point > 0 else 0.0
            cap = atr_points * p.MaxSpreadATRRatio
            if cap <= 0:
                return False
        if spread_points > cap:
            self.diag["blocked_spread"] += 1
            return True
        return False

    def _allowed_to_open(self) -> bool:
        p = self.p
        if self.target_equity_reached or self.minimum_equity_reached or \
                self.min_equity_triggers_exceeded:
            return False
        if self.is_paused:
            self.diag["blocked_paused"] += 1
            return False
        if self.count_losing() >= p.MaxHoldingLossPositions:
            self.diag["blocked_max_loss_pos"] += 1
            return False
        if len(self.positions) >= p.MaxOpenOrders:
            self.diag["blocked_max_orders"] += 1
            return False
        if self._spread_too_wide():
            return False
        return True

    def _signal_for_entry(self, is_buy: bool) -> float:
        p = self.p
        price = (self.cur_price + self.cur_spread / 2.0) if is_buy \
            else (self.cur_price - self.cur_spread / 2.0)
        if not self._entry_conditions(is_buy, price):
            return 0.0
        score = self.score_now(is_buy)
        thr = p.MinBuySignalScore if is_buy else p.MinSellSignalScore
        consec = self.consec_buy_candles if is_buy else self.consec_sell_candles
        if consec > 0 and p.ConsecutiveCandleThresholdBoost > 0:
            bc = min(consec, int(p.MaxConsecutiveCandleBoosts)) \
                if p.MaxConsecutiveCandleBoosts > 0 else consec
            thr += bc * p.ConsecutiveCandleThresholdBoost
        if p.EnableSignalDampening:
            losers = self.losing_same_dir(is_buy)
            if losers > 0:
                score -= losers * p.LosingPosScorePenalty
            if self.peak_equity > 0:
                dd = (self.peak_equity - self.equity()) / self.peak_equity * 100.0
                if dd >= p.DrawdownThresholdPct:
                    thr += p.DrawdownScoreBoost
        if score >= thr:
            self.diag["signals_buy" if is_buy else "signals_sell"] += 1
            return score
        self.diag["blocked_score"] += 1
        return 0.0

    # ------------------------------------------------------------------ #
    # Lot sizing                                                           #
    # ------------------------------------------------------------------ #
    def dynamic_lot(self, signal_score: float) -> float:
        p = self.p
        if not p.EnableDynamicLots:
            return self.normalize_volume(p.BaseLotSize)
        lot = float(p.BaseLotSize)
        eq = self.equity()
        drop_pct = ((self.peak_equity - eq) / self.peak_equity * 100.0) if self.peak_equity > 0 else 0.0
        in_cooldown = self.cooldown_until_epoch > 0 and self.dt_epoch[self.cur_bar] < self.cooldown_until_epoch
        bleeding = p.EnableBasketStop and self.floating_pl() < 0
        steps = 0
        if drop_pct > 0 and p.EquityDropPercent > 0 and signal_score >= p.MinSignalStrengthForLot \
                and not in_cooldown and not bleeding:
            steps = int(drop_pct / p.EquityDropPercent)
            if p.MaxEquityDropLotSteps > 0:
                steps = min(steps, int(p.MaxEquityDropLotSteps))
        lot = max(p.BaseLotSize, min(p.MaxLotSize, lot + steps * p.LotStepSize))
        lot = self.normalize_volume(lot)
        used = sum(pp["lot"] * self.contract * self.cur_price / self.leverage for pp in self.positions)
        free = eq - used
        if lot * self.contract * self.cur_price / self.leverage > free:
            affordable = self.vol_min
            test = self.vol_min
            while test <= lot and test * self.contract * self.cur_price / self.leverage <= free:
                affordable = test
                test += self.vol_step
            lot = self.normalize_volume(affordable)
        return lot

    # ------------------------------------------------------------------ #
    # Equity guards / basket stop                                          #
    # ------------------------------------------------------------------ #
    def _check_equity_guards(self):
        p = self.p
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        self.last_peak_equity = max(self.last_peak_equity, eq)
        if p.ResetOnNewPeak and eq >= self.last_peak_equity:
            self.min_equity_triggers = 0
        if p.TargetEquity > 0 and eq >= p.TargetEquity:
            self.target_equity_reached = True
        if p.MinimumEquity > 0 and eq <= p.MinimumEquity:
            self.minimum_equity_reached = True
        if self.target_equity_reached or self.minimum_equity_reached or \
                self.min_equity_triggers_exceeded:
            for pos in list(self.positions):
                self._close_position(pos, self.cur_bid if pos["is_buy"] else self.cur_ask,
                                     "equity_stop")
            return
        allowed = self.last_peak_equity * (100.0 - p.MinEquityPercent) / 100.0
        if p.MaxDrawdownFromPeak > 0:
            allowed = min(allowed, p.MaxDrawdownFromPeak)
        min_allowed = self.last_peak_equity - allowed
        if eq < min_allowed and not self.is_paused:
            self.min_equity_triggers += 1
            if p.MaxMinEquityTriggers > 0 and self.min_equity_triggers > p.MaxMinEquityTriggers:
                self.min_equity_triggers_exceeded = True
                for pos in list(self.positions):
                    self._close_position(pos, self.cur_bid if pos["is_buy"] else self.cur_ask,
                                         "equity_stop")
                return
            self.is_paused = True
            dur = p.PauseMinutes * (p.PauseMinutesMultiplier ** max(0, self.min_equity_triggers - 1))
            cap = p.MaxPauseMinutes if p.MaxPauseMinutes > 0 else 10 ** 9
            self.pause_duration_min = int(min(dur, cap))
            self.pause_until = self.dt_epoch[self.cur_bar] + self.pause_duration_min * 60
            self.diag["equity_pauses"] += 1
            self.last_peak_equity = self.balance

    def _check_basket_stop(self):
        p = self.p
        if not p.EnableBasketStop or p.MaxBasketLossPct <= 0:
            return
        eq = self.equity()
        if eq <= 0:
            return
        floating = self.floating_pl(skip_chains=p.EnableHedgeChain)
        if floating >= 0 or (-floating / eq) * 100.0 < p.MaxBasketLossPct:
            return
        closed = 0
        for pos in list(self.positions):
            if p.EnableHedgeChain and pos["chain_id"]:
                continue
            self._close_position(pos, self.cur_bid if pos["is_buy"] else self.cur_ask, "basket_stop")
            closed += 1
        if closed:
            self.diag["basket_pauses"] += 1
        if not self.is_paused:
            self.is_paused = True
            cap = p.MaxPauseMinutes if p.MaxPauseMinutes > 0 else 10 ** 9
            self.pause_duration_min = int(min(p.PauseMinutes, cap))
            self.pause_until = self.dt_epoch[self.cur_bar] + self.pause_duration_min * 60

    # ------------------------------------------------------------------ #
    # Position health                                                      #
    # ------------------------------------------------------------------ #
    def health(self, pos) -> float:
        p = self.p
        b = self.cur_bar
        is_buy = pos["is_buy"]
        blend = self._blend
        ema_f = self.ema_f[b - 1] * (1 - blend) + self._ema_f(b, 0) * blend
        ema_s = self.ema_s[b - 1] * (1 - blend) + self._ema_s(b, 0) * blend
        ema_f_prev = self.ema_f[b - 2] if b >= 2 else self.ema_f[b - 1]
        rsi = self.rsi[b - 1] * (1 - blend) + self._rsi(b, 0) * blend
        atr = self.atr[b - 1]
        trend_score = 0.0
        if (ema_f > ema_s) if is_buy else (ema_f < ema_s):
            sep = abs(ema_f - ema_s)
            sep_score = min(1.0, sep / (atr * 0.5)) if atr > 0 else 1.0
            slope_factor = 1.0
            if is_buy and ema_f <= ema_f_prev:
                slope_factor = 0.7
            if (not is_buy) and ema_f >= ema_f_prev:
                slope_factor = 0.7
            trend_score = sep_score * slope_factor
        if is_buy:
            floor_ = p.HealthRSIBuyMin - 15.0
            if rsi >= p.HealthRSIBuyMin:
                rsi_score = 1.0
            elif rsi > floor_:
                rsi_score = (rsi - floor_) / (p.HealthRSIBuyMin - floor_)
            else:
                rsi_score = 0.0
        else:
            ceil_ = p.HealthRSISellMax + 15.0
            if rsi <= p.HealthRSISellMax:
                rsi_score = 1.0
            elif rsi < ceil_:
                rsi_score = (ceil_ - rsi) / (ceil_ - p.HealthRSISellMax)
            else:
                rsi_score = 0.0
        price = self.cur_bid if is_buy else self.cur_ask
        adverse = (pos["entry"] - price) if is_buy else (price - pos["entry"])
        atr_score = 1.0
        if atr > 0 and adverse > 0:
            atr_score = max(0.0, 1.0 - (adverse / atr) / p.MaxAdverseATR)
        swing_look = max(5, int(p.HealthSwingLookback))
        swing_score = 1.0
        start, end = 1, min(1 + swing_look, b)
        if end - start >= 2:
            if is_buy:
                if price < min(self.l[b - j] for j in range(start, end)):
                    swing_score = 0.0
            else:
                if price > max(self.h[b - j] for j in range(start, end)):
                    swing_score = 0.0
        return (trend_score * self.w_trend + rsi_score * self.w_rsi +
                atr_score * self.w_atr + swing_score * self.w_swing)

    # ------------------------------------------------------------------ #
    # Trailing (ManageTrailingTPSL)                                        #
    # ------------------------------------------------------------------ #
    def _manage_trailing(self, pos):
        p = self.p
        if not p.EnableTrailing:
            return
        if p.EnableHedgeChain and pos["chain_id"]:
            return
        is_buy = pos["is_buy"]
        current_price = self.cur_bid if is_buy else self.cur_ask
        profit = pos["pl"]
        volume = pos["lot"]
        profit_threshold = p.MinBreakEvenProfit * p.ProfitThresholdMultiplier
        can_trail = (p.MinBreakEvenProfit <= 0 or not p.TrailingSLOnProfitableOnly
                     or profit >= profit_threshold)
        new_sl = pos["sl"]
        should = False
        if can_trail:
            if p.EnableAdaptiveSL and pos["score0"] > 0:
                cur_score = self.score_now(is_buy)
                sl_adj = (cur_score - pos["score0"]) * p.TrailingValueMultiplier
            else:
                sl_adj = 0.0
            floor_ = p.TrailingValueMultiplier * 0.1
            eff_dist = max(floor_, p.TrailingDistanceValue + sl_adj)
            use_hedge_trail = pos["hedge_graduated"] and p.HedgeTrailATR > 0
            hedge_atr = self.atr[max(0, self.cur_bar - 1)]
            if use_hedge_trail and hedge_atr > 0:
                dist_price = p.HedgeTrailATR * hedge_atr
                final_pts = dist_price / self.point
            else:
                final_pts = self.convert_to_points(int(p.TSInputType), eff_dist, volume)
                dist_price = final_pts * self.point
            be_price = self.break_even_price(pos)
            if is_buy:
                if (current_price - pos["entry"]) / self.point >= final_pts:
                    calc = current_price - dist_price
                    if p.TrailingEnableBreakEvenLock and calc < be_price:
                        calc = be_price
                    if (pos["sl"] == 0 or calc > pos["sl"]) and calc < self.cur_bid:
                        new_sl, should = calc, True
            else:
                if (pos["entry"] - current_price) / self.point >= final_pts:
                    calc = current_price + dist_price
                    if p.TrailingEnableBreakEvenLock and calc > be_price:
                        calc = be_price
                    if (pos["sl"] == 0 or calc < pos["sl"]) and calc > self.cur_ask:
                        new_sl, should = calc, True
        if pos["hedge_lock_profit"] > 0 and volume > 0:
            lock_dist = (pos["hedge_lock_profit"] / volume) * (self.tick_size / self.tick_value)
            if is_buy:
                lock_price = pos["entry"] + lock_dist
                if lock_price > new_sl and lock_price < self.cur_bid:
                    new_sl, should = lock_price, True
            else:
                lock_price = pos["entry"] - lock_dist
                if (new_sl == 0 or lock_price < new_sl) and lock_price > self.cur_ask:
                    new_sl, should = lock_price, True
        if should and abs(new_sl - pos["sl"]) >= self.point:
            pos["sl"] = round(new_sl, self.digits)

    def break_even_price(self, pos) -> float:
        p = self.p
        cost_in_price = 0.0
        min_profit_price = 0.0
        if self.tick_value != 0 and pos["lot"] != 0:
            min_profit_price = (p.MinBreakEvenProfit / pos["lot"]) * (self.tick_size / self.tick_value) \
                if p.MinBreakEvenProfit > 0 else 0.0
        if pos["is_buy"]:
            return pos["entry"] + self.cur_spread + cost_in_price + min_profit_price
        return pos["entry"] - self.cur_spread - cost_in_price - min_profit_price

    # ------------------------------------------------------------------ #
    # Loss management (ManageLosingPositions)                              #
    # ------------------------------------------------------------------ #
    def _manage_losing(self, pos):
        p = self.p
        if not p.EnableLossManagement:
            return
        if p.EnableHedgeChain and pos["chain_id"]:
            return
        if (self.cur_bar - pos["entry_bar"]) < int(p.HealthGraceBars):
            return
        health = self.health(pos)

        # 1. break-even once profit > spread cost * multiplier
        if p.EnableBreakEvenOnSpread and not pos["be_locked"]:
            trigger = self.cur_spread * pos["lot"] * self.contract * p.BreakEvenSpreadMultiplier
            if pos["pl"] > trigger:
                be = round(pos["entry"], self.digits)
                if pos["is_buy"]:
                    if be < self.cur_bid and (pos["sl"] == 0 or be > pos["sl"]):
                        pos["sl"], pos["be_locked"] = be, True
                else:
                    if be > self.cur_ask and (pos["sl"] == 0 or be < pos["sl"]):
                        pos["sl"], pos["be_locked"] = be, True

        # 2. scaled partial close on signal decay
        if p.EnablePartialClose and pos["score0"] > 0 and pos["partial_level"] < 3:
            cur_score = self.score_now(pos["is_buy"])
            ratio = cur_score / pos["score0"]
            lvl = pos["partial_level"]
            if lvl == 0 and ratio <= 0.75:
                if self._partial_close(pos, self.normalize_volume(pos["lot"] * p.PartialClose75Pct), "partial_75"):
                    pos["partial_level"] = 1
                    lvl = 1
            if lvl == 1 and ratio <= 0.50:
                if self._partial_close(pos, self.normalize_volume(pos["lot"] * p.PartialClose50Pct), "partial_50"):
                    pos["partial_level"] = 2
                    lvl = 2
            if lvl == 2 and ratio <= 0.25:
                pos["partial_level"] = 3
                self._close_position(pos, self.cur_bid if pos["is_buy"] else self.cur_ask, "signal_decay")
                self.diag["exits_decay"] += 1
                if p.EnableVirtualSLReentry:
                    self._try_reentry(pos["is_buy"], pos["score0"])
                return

        # 3. SL tightening on weak health
        if p.EnableHealthSLTightening and health < p.SLTightenMinHealthPct and self.atr[self.cur_bar - 1] > 0:
            hr = max(0.1, health / p.SLTightenMinHealthPct)
            dist = self.atr[self.cur_bar - 1] * p.SLTightenATRMultiplier * hr
            if pos["is_buy"]:
                new_sl = self.cur_bid - dist
                if pos["be_locked"] and new_sl < pos["entry"]:
                    new_sl = pos["entry"]
                if (pos["sl"] == 0 or new_sl > pos["sl"]) and new_sl < self.cur_bid:
                    pos["sl"] = round(new_sl, self.digits)
            else:
                new_sl = self.cur_ask + dist
                if pos["be_locked"] and new_sl > pos["entry"]:
                    new_sl = pos["entry"]
                if (pos["sl"] == 0 or new_sl < pos["sl"]) and new_sl > self.cur_ask:
                    pos["sl"] = round(new_sl, self.digits)

        # 4. profit-offset SL from consecutive wins closed elsewhere
        if p.EnableProfitOffsetSL and pos["pl"] < 0 and \
                pos["offset_wins"] >= p.ConsecutiveWinsRequired and \
                pos["offset_accum"] >= p.MinOffsetProfit and pos["offset_orig_sl"] > 0:
            vpp = (self.tick_value * pos["lot"]) / (self.tick_size / self.point)
            orig_risk = abs(pos["entry"] - pos["offset_orig_sl"]) / self.point * vpp
            target = orig_risk - pos["offset_accum"]
            if 0 < target < orig_risk and vpp > 0:
                dist = (target / vpp) * self.point
                if pos["is_buy"]:
                    new_sl = pos["entry"] - dist
                    if pos["be_locked"] and new_sl < pos["entry"]:
                        new_sl = pos["entry"]
                    if (pos["sl"] == 0 or new_sl > pos["sl"]) and new_sl < self.cur_bid:
                        pos["sl"] = round(new_sl, self.digits)
                else:
                    new_sl = pos["entry"] + dist
                    if pos["be_locked"] and new_sl > pos["entry"]:
                        new_sl = pos["entry"]
                    if (pos["sl"] == 0 or new_sl < pos["sl"]) and new_sl > self.cur_ask:
                        pos["sl"] = round(new_sl, self.digits)
            elif target <= 0:
                be = round(pos["entry"], self.digits)
                if pos["is_buy"] and be < self.cur_bid and (pos["sl"] == 0 or be > pos["sl"]):
                    pos["sl"], pos["be_locked"] = be, True
                elif (not pos["is_buy"]) and be > self.cur_ask and (pos["sl"] == 0 or be < pos["sl"]):
                    pos["sl"], pos["be_locked"] = be, True

        # 5. health close + virtual SL re-entry
        if health < p.MinHealthScore:
            self._close_position(pos, self.cur_bid if pos["is_buy"] else self.cur_ask, "health")
            self.diag["exits_health"] += 1
            if p.EnableVirtualSLReentry:
                self._try_reentry(pos["is_buy"], pos["score0"])

    def _try_reentry(self, is_buy: bool, initial_score: float):
        p = self.p
        if initial_score <= 0:
            return
        if p.EnableNewBarEntryOnly and p.ReentryRespectsNewBarGate and self.entry_bar_idx == self.cur_bar:
            return
        if self.target_equity_reached or self.minimum_equity_reached or self.min_equity_triggers_exceeded:
            return
        if self.is_paused:
            return
        if self.count_losing() >= p.MaxHoldingLossPositions or len(self.positions) >= p.MaxOpenOrders:
            return
        score = self.score_now(is_buy)
        if score < initial_score * p.ReentryMinSignalPct:
            return
        lot = self.dynamic_lot(score)
        self._open_position(is_buy, self.cur_ask if is_buy else self.cur_bid, lot, score, "reentry")
        self.diag["reentries"] += 1

    # ------------------------------------------------------------------ #
    # Hedge chain (ManageHedgeChains)                                      #
    # ------------------------------------------------------------------ #
    def _compute_hedge_lot(self, older_lot: float, older_loss: float, atr: float) -> float:
        p = self.p
        lot = 0.0
        if p.HedgeAutoLot:
            pc = p.HedgeRecoveryPct / 100.0 or 1.0
            target_price = p.HedgeRecoveryATR * atr
            money_per_lot = (target_price / self.tick_size) * self.tick_value if self.tick_size > 0 else 0.0
            if money_per_lot > 0 and target_price > 0:
                lot = pc * older_lot + pc * max(0.0, older_loss) / money_per_lot
        if lot <= 0:
            lot = older_lot * p.HedgeLotMultiplier
        lot = max(lot, older_lot + (self.vol_step if self.vol_step > 0 else 0.01))
        if p.HedgeMaxLot > 0:
            lot = min(lot, p.HedgeMaxLot)
        return self.normalize_volume(lot)

    def _open_hedge(self, root_pos, hedge_lot, level, anchor_loss, cycle_num, chain_id):
        is_buy = not root_pos["is_buy"]
        self._open_position(is_buy, self.cur_ask if is_buy else self.cur_bid, hedge_lot, 0.0,
                            "hedge", chain=dict(chain_id=chain_id, hedge_level=level,
                                                chain_anchor_loss=anchor_loss,
                                                cycle_num=cycle_num), naked=True)

    def _chain_stop_threshold(self) -> float:
        p = self.p
        usd = p.HedgeMaxChainLossUSD if p.HedgeMaxChainLossUSD > 0 else 0.0
        pct = self.equity() * p.HedgeMaxChainLossPct / 100.0 if p.HedgeMaxChainLossPct > 0 else 0.0
        return min(usd, pct) if (usd > 0 and pct > 0) else max(usd, pct)

    def _release_chain(self, chain_id):
        for pos in self.positions:
            if pos["chain_id"] == chain_id:
                pos.update(chain_id=0, hedge_level=0, chain_anchor_loss=0.0, cycle_num=0,
                           no_rehedge=True, hedge_graduated=True)
        self.diag["hedge_releases"] += 1

    def _manage_hedge_chains(self):
        p = self.p
        if not p.EnableHedgeChain:
            return
        atr = self.atr[self.cur_bar - 1]
        if atr <= 0:
            return
        chains = sorted({pos["chain_id"] for pos in self.positions if pos["chain_id"]})
        for cid in chains:
            legs = [pos for pos in self.positions if pos["chain_id"] == cid]
            if not legs:
                continue
            if len(legs) == 1:
                legs[0].update(chain_id=0, hedge_level=0, chain_anchor_loss=0.0, cycle_num=0,
                               hedge_graduated=True)
                continue
            older = min(legs, key=lambda x: x["hedge_level"])
            hedge = max(legs, key=lambda x: x["hedge_level"])
            total_pl = sum(x["pl"] for x in legs)
            anchor = next((x["chain_anchor_loss"] for x in legs if x["chain_anchor_loss"] > 0), 0.0)
            if older["pl"] < 0:
                need = (p.HedgeRecoveryPct / 100.0) * (-older["pl"])
                if hedge["pl"] >= need:
                    self._close_position(older, self.cur_bid if older["is_buy"] else self.cur_ask,
                                         "hedge_covered")
                    hedge.update(chain_id=0, hedge_level=0, chain_anchor_loss=0.0, cycle_num=0,
                                 hedge_graduated=True, hedge_lock_profit=need)
                    self.diag["hedge_covers"] += 1
                    continue
            if hedge["pl"] < 0 and older["pl"] >= p.HedgeRollMinProfit:
                level_ok = hedge["hedge_level"] < p.HedgeCycleLevels
                new_lot = self._compute_hedge_lot(hedge["lot"], -hedge["pl"], atr) if level_ok else 0.0
                if level_ok and new_lot > hedge["lot"]:
                    self._close_position(older, self.cur_bid if older["is_buy"] else self.cur_ask,
                                         "hedge_roll")
                    self._open_hedge(hedge, new_lot, hedge["hedge_level"] + 1, anchor,
                                     hedge["cycle_num"], cid)
                    self.diag["hedge_rolls"] += 1
                    continue
                cycles_left = (p.HedgeMaxCycles <= 0 or hedge["cycle_num"] + 1 < p.HedgeMaxCycles)
                if p.EnableHedgeCycleReset and cycles_left:
                    if self._reseed_chain(cid, older, hedge, hedge["lot"], hedge["cycle_num"], atr):
                        self.diag["hedge_reseeds"] += 1
                    else:
                        self._release_chain(cid)
                    continue
                self._release_chain(cid)
                continue
            thr = self._chain_stop_threshold()
            if thr > 0 and total_pl <= -thr:
                for leg in list(legs):
                    self._close_position(leg, self.cur_bid if leg["is_buy"] else self.cur_ask,
                                         "hedge_stop")
                self.diag["exits_hedge"] += 1
                continue

        # Phase B — start a chain on a qualifying loser
        for pos in list(self.positions):
            if pos["chain_id"] or pos["no_rehedge"] or pos["pl"] >= 0:
                continue
            adverse = (pos["entry"] - self.cur_bid) if pos["is_buy"] else (self.cur_ask - pos["entry"])
            if adverse <= 0 or (adverse / atr) < p.HedgeTriggerATR:
                continue
            if p.HedgeRequireSignal:
                if self.score_now(not pos["is_buy"]) < p.HedgeMinSignalScore:
                    continue
            anchor_loss = -pos["pl"]
            hedge_lot = self._compute_hedge_lot(pos["lot"], anchor_loss, atr)
            if hedge_lot <= pos["lot"]:
                continue
            pos.update(chain_id=pos["ticket"], hedge_level=0, chain_anchor_loss=anchor_loss,
                       cycle_num=0, hedge_graduated=False, hedge_lock_profit=0.0)
            if p.HedgeClearRootSL:
                pos["sl"] = 0.0
            self._open_hedge(pos, hedge_lot, 1, anchor_loss, 0, pos["ticket"])
            self.diag["hedge_starts"] += 1

    def _reseed_chain(self, cid, older, hedge, hedge_lot, cycle_num, atr) -> bool:
        p = self.p
        step = self.vol_step if self.vol_step > 0 else 0.01
        close_vol = math.floor((hedge_lot * p.HedgeCyclePartialPct / 100.0) / step) * step
        if hedge_lot - close_vol < self.vol_min:
            close_vol = math.floor((hedge_lot - self.vol_min) / step) * step
        if close_vol < self.vol_min or (hedge_lot - close_vol) < self.vol_min:
            return False
        if not self._partial_close(hedge, close_vol, "hedge_reseed_partial"):
            return False
        self._close_position(older, self.cur_bid if older["is_buy"] else self.cur_ask,
                             "hedge_reseed_close_older")
        new_anchor = max(0.01, -hedge["pl"]) if hedge["pl"] < 0 else 0.01
        hedge.update(chain_id=hedge["ticket"], hedge_level=0, chain_anchor_loss=new_anchor,
                     cycle_num=cycle_num + 1)
        h_lot = self._compute_hedge_lot(hedge["lot"], new_anchor, atr)
        if h_lot > hedge["lot"]:
            self._open_hedge(hedge, h_lot, 1, new_anchor, cycle_num + 1, hedge["ticket"])
        return True

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #
    def run(self) -> Result:
        p = self.p
        hw = (p.HealthTrendWeight, p.HealthRSIWeight, p.HealthATRWeight, p.HealthSwingWeight)
        tot = sum(hw)
        if tot > 0:
            self.w_trend, self.w_rsi, self.w_atr, self.w_swing = [w / tot for w in hw]
        else:
            self.w_trend = self.w_rsi = self.w_atr = self.w_swing = 0.25

        start = max(self.trade_start_idx, self.min_history)
        if self.n > 2:
            d = self.dt_epoch[1] - self.dt_epoch[0]
            if 0 < d < 3600:
                self.period_seconds = d

        for b in range(start, self.n):
            self.cur_bar = b
            self.cur_o = self.o[b]
            self.cur_spread = self.sp[b]
            # new-bar bookkeeping (OnTick's new-bar block)
            self.consec_buy_candles = self.consec_buy_candles + 1 if self.buys_on_bar > 0 else 0
            self.consec_sell_candles = self.consec_sell_candles + 1 if self.sells_on_bar > 0 else 0
            self.buys_on_bar = 0
            self.sells_on_bar = 0
            self.prepare_bar(b)

            if self.path_mode == "hl":     # robustness check: opposite extreme first
                if self.c[b] >= self.o[b]:
                    steps = (self.o[b], self.h[b], self.l[b], self.c[b])
                else:
                    steps = (self.o[b], self.l[b], self.h[b], self.c[b])
            else:
                if self.c[b] >= self.o[b]:
                    steps = (self.o[b], self.l[b], self.h[b], self.c[b])
                else:
                    steps = (self.o[b], self.h[b], self.l[b], self.c[b])
            for si, price in enumerate(steps):
                self.cur_price = price
                self.cur_bid = price - self.cur_spread / 2.0
                self.cur_ask = price + self.cur_spread / 2.0
                self.cur_h = max(self.cur_o, price)
                self.cur_l = min(self.cur_o, price)
                self._score_cache = {}
                self._step(si == 0)
            npos = len(self.positions)
            self.max_positions_open = max(self.max_positions_open, npos)
            self.pos_open_sum += npos
            self.bars_counted += 1
            self.equity_curve.append((self.dt_epoch[b], self.equity()))
        return self._finalize()

    def _step(self, is_first_substep: bool):
        if not is_first_substep and not self.positions:
            return
        # broker-side SL / TP on this price step
        for pos in list(self.positions):
            if pos["is_buy"]:
                if pos["sl"] > 0 and self.cur_bid <= pos["sl"]:
                    self._close_position(pos, pos["sl"] - self.slippage, "sl")
                    self.diag["exits_sl"] += 1
                elif pos["tp"] > 0 and self.cur_bid >= pos["tp"]:
                    self._close_position(pos, pos["tp"], "tp")
            else:
                if pos["sl"] > 0 and self.cur_ask >= pos["sl"]:
                    self._close_position(pos, pos["sl"] + self.slippage, "sl")
                    self.diag["exits_sl"] += 1
                elif pos["tp"] > 0 and self.cur_ask <= pos["tp"]:
                    self._close_position(pos, pos["tp"], "tp")
        for pos in self.positions:
            pos["pl"] = self._pl(pos)

        self._check_equity_guards()
        if self.target_equity_reached or self.minimum_equity_reached or self.min_equity_triggers_exceeded:
            return
        self._check_basket_stop()
        if self.is_paused and self.dt_epoch[self.cur_bar] >= self.pause_until:
            self.is_paused = False

        self._manage_hedge_chains()
        for pos in list(self.positions):
            self._manage_trailing(pos)
        for pos in list(self.positions):
            pos["pl"] = self._pl(pos)
            self._manage_losing(pos)

        if not is_first_substep:
            return
        self.entry_bar_idx = self.cur_bar
        if self.is_paused or self.target_equity_reached or self.minimum_equity_reached \
                or self.min_equity_triggers_exceeded:
            return
        buy_sig = self._signal_for_entry(True)
        sell_sig = self._signal_for_entry(False)
        if buy_sig > sell_sig and self.p.EnableBuyOrders:
            self._try_open(True, buy_sig)
        elif sell_sig > buy_sig and self.p.EnableSellOrders:
            self._try_open(False, sell_sig)

    def _pl(self, pos) -> float:
        if pos["is_buy"]:
            return (self.cur_bid - pos["entry"]) * self.contract * pos["lot"]
        return (pos["entry"] - self.cur_ask) * self.contract * pos["lot"]

    def _try_open(self, is_buy: bool, score: float):
        if not self._allowed_to_open():
            return
        lot = self.dynamic_lot(score)
        self._open_position(is_buy, self.cur_ask if is_buy else self.cur_bid, lot, score, "signal")

    # ------------------------------------------------------------------ #
    def _finalize(self) -> Result:
        for pos in list(self.positions):
            self._close_position(pos, self.cur_bid if pos["is_buy"] else self.cur_ask, "end_of_test")
        eq = [e for _, e in self.equity_curve]
        peak = self.balance0
        max_dd = max_dd_pct = 0.0
        for e in eq:
            peak = max(peak, e)
            dd = peak - e
            max_dd = max(max_dd, dd)
            max_dd_pct = max(max_dd_pct, dd / peak * 100.0 if peak > 0 else 0.0)
        wins = [t for t in self.trades if t.profit > 0]
        losses = [t for t in self.trades if t.profit <= 0]
        gross_win = sum(t.profit for t in wins)
        gross_loss = -sum(t.profit for t in losses)
        net = self.balance - self.balance0
        reasons = {}
        for t in self.trades:
            reasons[t.reason] = reasons.get(t.reason, 0) + 1
        stats = dict(
            net_profit=round(net, 2),
            net_profit_pct=round(net / self.balance0 * 100.0, 2),
            final_balance=round(self.balance, 2),
            trades=len(self.trades), wins=len(wins), losses=len(losses),
            win_rate=round(len(wins) / len(self.trades) * 100.0, 1) if self.trades else 0.0,
            gross_profit=round(gross_win, 2), gross_loss=round(gross_loss, 2),
            profit_factor=round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            avg_trade=round(net / len(self.trades), 3) if self.trades else 0.0,
            avg_win=round(gross_win / len(wins), 3) if wins else 0.0,
            avg_loss=round(-gross_loss / len(losses), 3) if losses else 0.0,
            max_dd_usd=round(max_dd, 2), max_dd_pct=round(max_dd_pct, 2),
            max_positions_open=self.max_positions_open,
            avg_positions_open=round(self.pos_open_sum / max(1, self.bars_counted), 2),
            exposure_pct=round(self.pos_open_sum / max(1, self.bars_counted) * 100.0, 1),
            min_equity=round(min(eq), 2) if eq else self.balance0,
            max_equity=round(max(eq), 2) if eq else self.balance0,
            exit_reasons=reasons,
        )
        return Result(profile=self.label, data=self.meta.get("name", "?"),
                      start=str(self.dt[self.trade_start_idx]), end=str(self.dt[-1]),
                      initial_balance=self.balance0, final_balance=round(self.balance, 2),
                      equity_curve=self.equity_curve, trades=self.trades,
                      stats=stats, diagnostics=dict(self.diag))


# --------------------------------------------------------------------------- #
def run_backtest(data_csv: str, meta_json: str, profile: str, initial_balance=1000.0,
                 leverage=100.0, overrides: dict | None = None, label: str | None = None,
                 df=None, meta=None, slippage: float = 0.0, spread_mult: float = 1.0,
                 path_mode: str = "lh"):
    import pandas as pd
    if df is None:
        df = pd.read_csv(data_csv, parse_dates=["datetime"])
    if meta is None:
        with open(meta_json) as fh:
            meta = json.load(fh)
    params = load_profile(profile, overrides)
    eng = NyaoEngine(df, meta, params, initial_balance=initial_balance,
                     leverage=leverage, label=label or profile, slippage=slippage,
                     spread_mult=spread_mult, path_mode=path_mode)
    return eng.run(), eng
