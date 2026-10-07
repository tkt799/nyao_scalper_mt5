#!/usr/bin/env python3
"""
run_matrix.py — run the shipped profiles over the research datasets and produce
(a) an overall result per (profile, dataset) and (b) "loop" (rolling-window)
results, i.e. the EA restarted on each successive week/month of the same tape.

Usage:
  python run_matrix.py --mode profiles   # 5 profiles x 3 datasets
  python run_matrix.py --mode rolling    # rolling weekly + monthly windows
  python run_matrix.py --mode variants   # sensitivity / what-if switches
"""
import argparse
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nyao_engine import PROFILE_FILES, load_profile, run_backtest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))
os.makedirs(RESULTS, exist_ok=True)

DATASETS = ["xauusd_m1_2026H2", "xauusd_m1_ftmo", "xauusdm_m5_2026H2"]
PROFILES = list(PROFILE_FILES.keys())


def load_dataset(name):
    df = pd.read_csv(os.path.join(DATA, name + ".csv"), parse_dates=["datetime"])
    meta = json.load(open(os.path.join(DATA, name + ".json")))
    return df, meta


def run_one(df, meta, profile, overrides=None, label=None, tag=None):
    meta2 = dict(meta)
    if tag:
        meta2["trade_start"] = tag
    res, eng = run_backtest(None, None, profile, df=df, meta=meta2,
                            overrides=overrides, label=label or profile)
    return res


def summarize(res):
    s = dict(res.stats)
    s.pop("exit_reasons", None)
    return s


def dump(name, payload):
    path = os.path.join(RESULTS, name)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print("  -> saved", path)


def mode_profiles():
    out = {}
    for ds in DATASETS:
        df, meta = load_dataset(ds)
        out[ds] = {}
        for prof in PROFILES:
            t0 = time.time()
            res = run_one(df, meta, prof)
            out[ds][prof] = dict(stats=summarize(res), diag=res.diagnostics,
                                 exit_reasons=res.stats.get("exit_reasons", {}),
                                 seconds=round(time.time() - t0, 1))
            print(f"[{ds}] {prof:15s} net={res.stats['net_profit']:>9.2f} "
                  f"({res.stats['net_profit_pct']:>7.2f}%) trades={res.stats['trades']:>5d} "
                  f"PF={res.stats['profit_factor']} maxDD={res.stats['max_dd_pct']:>6.2f}%")
    dump("profiles_overall.json", out)


def _window_runs(df, meta, freq, profiles, tag_prefix):
    """Rolling windows: slice the tape, keep warm-up, run each window flat."""
    start = pd.Timestamp(meta["trade_start"])
    end = pd.Timestamp(meta["trade_end"])
    edges = pd.date_range(start=start, end=end, freq=freq)
    out = []
    for i in range(len(edges) - 1):
        w_start, w_end = edges[i], edges[i + 1]
        if i == len(edges) - 2:
            w_end = end
        sub = df[(df.datetime >= w_start - pd.Timedelta(days=10)) & (df.datetime < w_end)]
        if len(sub) < 2000:
            continue
        for prof in profiles:
            res = run_one(sub, meta, prof, tag=str(w_start))
            out.append(dict(window=f"{w_start.date()}..{w_end.date()}", profile=prof,
                            stats=summarize(res), diag=res.diagnostics,
                            exit_reasons=res.stats.get("exit_reasons", {})))
            print(f"[{tag_prefix}] {w_start.date()} {prof:15s} "
                  f"net={res.stats['net_profit']:>8.2f} trades={res.stats['trades']:>5d} "
                  f"maxDD={res.stats['max_dd_pct']:>6.2f}%")
    return out


def mode_rolling():
    payload = {}
    df, meta = load_dataset("xauusd_m1_2026H2")
    payload["weekly_primary"] = _window_runs(df, meta, "7D", PROFILES, "W")
    payload["monthly_primary"] = _window_runs(df, meta, "1MS", PROFILES, "M")
    df2, meta2 = load_dataset("xauusd_m1_ftmo")
    payload["monthly_ftmo"] = _window_runs(df2, meta2, "1MS", PROFILES, "M-ftmo")
    dump("rolling_windows.json", payload)


VARIANTS = {
    "NoHedgeChain": {"EnableHedgeChain": False},
    "NoLossMgmt": {"EnableLossManagement": False},
    "NoTrailing": {"EnableTrailing": False},
    "NoDampening": {"EnableSignalDampening": False},
    "NoSpreadFilter": {"MaxSpreadATRRatio": 5.0},
    "TightSpreadCap": {"MaxSpreadATRRatio": 0.10},
    "BasketStopOn": {"MaxBasketLossPct": 5.0, "MinEquityPercent": 90.0},
    "RiskRewardOn": {"EnableRiskReward": True, "RRAtrMultiplier": 2.0, "RiskRewardRatio": 1.5},
    "TrailFloorRaise": {"TrailingValueMultiplier": 2.0},
    "TPon_1R": {"EnableTakeProfit": True, "TPInputType": 0, "TPValue": 2.0},
    "NoReentry": {"EnableVirtualSLReentry": False},
    "NoPartialClose": {"EnablePartialClose": False},
    "NoDynamicLots": {"EnableDynamicLots": False},
    "BuyOnly": {"EnableSellOrders": False},
    "SellOnly": {"EnableBuyOrders": False},
}


def mode_variants(profile="default", dataset="xauusd_m1_2026H2"):
    df, meta = load_dataset(dataset)
    base = run_one(df, meta, profile)
    payload = {"base": dict(stats=summarize(base), diag=base.diagnostics,
                            exit_reasons=base.stats.get("exit_reasons", {})),
               "variants": {}}
    print(f"[base] {profile}: net={base.stats['net_profit']:.2f} "
          f"trades={base.stats['trades']} maxDD={base.stats['max_dd_pct']}%")
    for name, ov in VARIANTS.items():
        res = run_one(df, meta, profile, overrides=ov, label=name)
        payload["variants"][name] = dict(stats=summarize(res), diag=res.diagnostics,
                                         exit_reasons=res.stats.get("exit_reasons", {}),
                                         overrides=ov)
        print(f"  {name:18s} net={res.stats['net_profit']:>9.2f} "
              f"trades={res.stats['trades']:>5d} PF={res.stats['profit_factor']} "
              f"maxDD={res.stats['max_dd_pct']:>6.2f}%")
    dump("variants.json", payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="profiles",
                    choices=["profiles", "rolling", "variants"])
    ap.add_argument("--profile", default="default")
    ap.add_argument("--dataset", default="xauusd_m1_2026H2")
    a = ap.parse_args()
    if a.mode == "profiles":
        mode_profiles()
    elif a.mode == "rolling":
        mode_rolling()
    else:
        mode_variants(a.profile, a.dataset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
