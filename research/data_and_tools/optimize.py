#!/usr/bin/env python3
"""
optimize.py — staged parameter search + walk-forward validation for Nyao Scalper.

Protocol (no look-ahead: parameters are only ever chosen on the TRAIN slice and
then measured - untouched - on the TEST slice):

  TRAIN  : 2026-05-18 .. 2026-07-01   (primary M1 tape)
  TEST   : 2026-07-01 .. 2026-08-17   (primary M1 tape, out-of-sample)
  HOLDOUT: FTMO M1 tape 2026-02-16 .. 2026-05-16 (different broker, different regime)

Stages
  S1  structural switches        (hedge chain, real TP, independent R:R, ...)
  S2  coarse grid  MinSignalScore x MaxSpreadATRRatio x MinVolRatioToTrade
  S3  refinement   trailing floor / break-even / health / SL / smoothing
  S4  OOS validation of the top candidates on TEST + HOLDOUT

Results -> research/results/optimization.json
"""
import itertools
import json
import os
import sys
import time
from multiprocessing import Pool

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nyao_engine import run_backtest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))

TRAIN = ("2026-05-18", "2026-07-01")
TEST = ("2026-07-01", "2026-08-17")
FTMO = ("2026-02-16", "2026-05-16")

BASE_PROFILE = "default"
INITIAL_BALANCE = 1000.0

_DF = {}
_META = {}


def _get(ds):
    if ds not in _DF:
        _DF[ds] = pd.read_csv(os.path.join(DATA, ds + ".csv"), parse_dates=["datetime"])
        _META[ds] = json.load(open(os.path.join(DATA, ds + ".json")))
    return _DF[ds], _META[ds]


def evaluate(overrides, window, ds="xauusd_m1_2026H2", profile=BASE_PROFILE,
             return_res=False):
    df, meta = _get(ds)
    start, end = window
    sub = df[(df.datetime >= pd.Timestamp(start) - pd.Timedelta(days=12)) &
             (df.datetime < pd.Timestamp(end))]
    m = dict(meta)
    m["trade_start"] = start
    res, _ = run_backtest(None, None, profile, df=sub, meta=m, overrides=overrides,
                          label="opt")
    out = dict(net=res.stats["net_profit"], pf=res.stats["profit_factor"],
               trades=res.stats["trades"], dd=res.stats["max_dd_pct"],
               win_rate=res.stats["win_rate"], avg=res.stats["avg_trade"],
               dd_usd=res.stats["max_dd_usd"],
               reasons=res.stats.get("exit_reasons", {}),
               diag={k: v for k, v in res.diagnostics.items() if v})
    return (out, res) if return_res else out


def score(m):
    """Objective: prefer net profit with a hard penalty for drawdown and a
    minimum sample size."""
    if m["trades"] < 120:
        return -1e9
    return m["net"] - 2.5 * m["dd_usd"]


def _worker(job):
    idx, ov, window, ds, profile = job
    try:
        m = evaluate(ov, window, ds=ds, profile=profile)
    except Exception as exc:                                  # pragma: no cover
        m = dict(net=0, pf=None, trades=0, dd=0, dd_usd=0, error=str(exc),
                 win_rate=0, avg=0, reasons={}, diag={})
    return idx, m


def run_grid(candidates, window, ds="xauusd_m1_2026H2", profile=BASE_PROFILE,
             workers=2, label=""):
    jobs = [(i, ov, window, ds, profile) for i, ov in enumerate(candidates)]
    out = [None] * len(candidates)
    t0 = time.time()
    with Pool(workers) as pool:
        for idx, m in pool.imap_unordered(_worker, jobs, chunksize=1):
            out[idx] = m
    for i, (ov, m) in enumerate(zip(candidates, out)):
        pass
    ranked = sorted(zip(candidates, out), key=lambda x: score(x[1]), reverse=True)
    if label:
        print(f"  [{label}] {len(candidates)} runs in {time.time()-t0:.0f}s ; best: "
              f"net={ranked[0][1]['net']:.0f} dd={ranked[0][1]['dd']:.1f}% "
              f"trades={ranked[0][1]['trades']}")
    return ranked


# --------------------------------------------------------------------------- #
def main():
    workers = 2
    all_results = {"protocol": dict(train=TRAIN, test=TEST, holdout_ftmo=FTMO,
                                    base_profile=BASE_PROFILE,
                                    initial_balance=INITIAL_BALANCE)}

    # ---------------- S1: structural switches ---------------------------------
    s1 = [{}]
    for name, ov in {
        "no_hedge": {"EnableHedgeChain": False},
        "no_hedge_tp5": {"EnableHedgeChain": False, "EnableTakeProfit": True,
                         "TPInputType": 0, "TPValue": 5.0},
        "no_hedge_tp3": {"EnableHedgeChain": False, "EnableTakeProfit": True,
                         "TPInputType": 0, "TPValue": 3.0},
        "no_hedge_rr1.5": {"EnableHedgeChain": False, "EnableRiskReward": True,
                           "RRRiskMode": 1, "RRAtrMultiplier": 2.0, "RiskRewardRatio": 1.5},
        "no_hedge_rr1": {"EnableHedgeChain": False, "EnableRiskReward": True,
                         "RRRiskMode": 1, "RRAtrMultiplier": 2.0, "RiskRewardRatio": 1.0},
        "no_hedge_no_trailcollapse": {"EnableHedgeChain": False,
                                      "TrailingValueMultiplier": 1.0},
        "hedge_on_but_capped": {"EnableHedgeChain": True, "HedgeMaxCycles": 1,
                                "EnableHedgeCycleReset": False, "HedgeMaxLot": 0.03},
    }.items():
        s1.append(ov)
    r1 = run_grid(s1, TRAIN, workers=workers, label="S1")
    best_structural = r1[0][0]
    all_results["S1_structural"] = [
        dict(overrides=ov, metrics=m) for ov, m in r1]
    print("\nS1 best overrides:", best_structural, json.dumps(r1[0][1], default=str)[:220])

    # ---------------- S2: coarse numeric grid ---------------------------------
    grid_ms = [3.5, 4.5, 5.5, 6.5]
    grid_sp = [0.10, 0.25, 0.50]
    grid_vol = [0.60, 0.85, 1.00]
    s2 = []
    for ms, sp, vol in itertools.product(grid_ms, grid_sp, grid_vol):
        ov = dict(best_structural)
        ov.update({"MinBuySignalScore": ms, "MinSellSignalScore": ms,
                   "MaxSpreadATRRatio": sp, "MinVolRatioToTrade": vol})
        s2.append(ov)
    r2 = run_grid(s2, TRAIN, workers=workers, label="S2")
    best2 = r2[0][0]
    all_results["S2_grid"] = [dict(overrides=ov, metrics=m) for ov, m in r2]
    print("\nS2 best:", {k: v for k, v in best2.items()}, json.dumps(r2[0][1], default=str)[:220])

    # ---------------- S3: refinement ------------------------------------------
    s3 = []
    for trail_floor, be, pthresh, health, sl, smooth, blend in itertools.product(
            [0.2, 0.6, 1.5],            # TrailingValueMultiplier (0.2 = shipped, collapses)
            [0.3, 0.75, 1.5],           # MinBreakEvenProfit
            [1.0, 2.0],                 # ProfitThresholdMultiplier
            [0.30, 0.40, 0.50],         # MinHealthScore
            [0.5, 1.0],                 # SLValue (% of equity)
            [1, 3],                     # SignalSmoothingCandles
            [0.2, 0.5]):                # CurrentCandleBlend
        ov = dict(best2)
        ov.update({"TrailingValueMultiplier": trail_floor, "MinBreakEvenProfit": be,
                   "ProfitThresholdMultiplier": pthresh, "MinHealthScore": health,
                   "SLValue": sl, "SignalSmoothingCandles": smooth,
                   "CurrentCandleBlend": blend})
        s3.append(ov)
    # trim the grid to a manageable size with a fixed random-ish stride
    stride = max(1, len(s3) // 96)
    s3 = s3[::stride]
    r3 = run_grid(s3, TRAIN, workers=workers, label="S3")
    all_results["S3_refine"] = [dict(overrides=ov, metrics=m) for ov, m in r3]
    best3 = r3[0][0]
    print("\nS3 best:", json.dumps(best3, default=str))
    print("S3 best metrics:", json.dumps(r3[0][1], default=str)[:240])

    # ---------------- S4: out-of-sample validation ----------------------------
    top = [r3[0][0], r2[0][0], r1[0][0], {}]
    seen, uniq = set(), []
    for ov in top:
        key = json.dumps(ov, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(ov)
    val = []
    for ov in uniq:
        m_test = evaluate(ov, TEST)
        m_hold = evaluate(ov, FTMO, ds="xauusd_m1_ftmo")
        m_m5 = evaluate(ov, ("2026-05-18", "2026-08-22"), ds="xauusdm_m5_2026H2")
        m_train = evaluate(ov, TRAIN)
        val.append(dict(overrides=ov, train=m_train, test=m_test, holdout_ftmo=m_hold,
                        holdout_m5=m_m5))
        print(f"  TEST  net={m_test['net']:>8.0f} dd={m_test['dd']:>5.1f}% "
              f"trades={m_test['trades']:>5d} | FTMO net={m_hold['net']:>8.0f} "
              f"dd={m_hold['dd']:>5.1f}% | M5 net={m_m5['net']:>8.0f}")
    all_results["S4_validation"] = val

    with open(os.path.join(RESULTS, "optimization.json"), "w") as fh:
        json.dump(all_results, fh, indent=2, default=str)
    print("\nsaved", os.path.join(RESULTS, "optimization.json"))


if __name__ == "__main__":
    main()
