#!/usr/bin/env python3
"""
backtest.py — one-shot CLI around the offline Nyao Scalper engine.

Examples
--------
  python backtest.py --profile safe --data xauusd_m1_2026H2
  python backtest.py --profile default --overrides EnableHedgeChain=false
  python backtest.py --set ../../settings/balanced.set --data xauusdm_m5_2026H2
  python backtest.py --profile default --overrides-file ../../research/recommended_settings/tuned-lowrisk-nomartingale.set
  python backtest.py --profile default --spread-mult 1.5 --slippage 0.05 --csv trades.csv
"""
import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nyao_engine import load_profile, load_set_file, run_backtest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))


def parse_overrides(pairs):
    out = {}
    for kv in pairs or []:
        k, _, v = kv.partition("=")
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = float(v) if "." in v else int(v)
            except ValueError:
                out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="xauusd_m1_2026H2", help="dataset name in research/data")
    ap.add_argument("--profile", default="default", help="shipped profile name")
    ap.add_argument("--set", dest="set_file", default=None, help="load a .set file instead")
    ap.add_argument("--overrides", nargs="*", default=[], help="Key=Value pairs")
    ap.add_argument("--overrides-file", default=None, help="a .set file with override values")
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=100.0)
    ap.add_argument("--spread-mult", type=float, default=1.0)
    ap.add_argument("--slippage", type=float, default=0.0, help="adverse price offset on stop fills")
    ap.add_argument("--path", choices=["lh", "hl"], default="lh")
    ap.add_argument("--csv", default=None, help="write the trade list here")
    a = ap.parse_args()

    df = pd.read_csv(os.path.join(DATA, a.data + ".csv"), parse_dates=["datetime"])
    meta = json.load(open(os.path.join(DATA, a.data + ".json")))

    ov = parse_overrides(a.overrides)
    if a.overrides_file:
        ov.update(load_set_file(a.overrides_file))
    if a.set_file:
        # full parameter set: profile values are irrelevant, only the file matters
        base = load_profile(a.profile)
        params = base
        ov.update(load_set_file(a.set_file))

    res, eng = run_backtest(None, None, a.profile, df=df, meta=meta, overrides=ov,
                            initial_balance=a.balance, leverage=a.leverage,
                            slippage=a.slippage, spread_mult=a.spread_mult, path_mode=a.path)
    print(json.dumps(res.stats, indent=2, default=str))
    print("diagnostics:", json.dumps({k: v for k, v in res.diagnostics.items() if v},
                                     indent=2, default=str))
    if a.csv:
        pd.DataFrame([t.__dict__ for t in res.trades]).to_csv(a.csv, index=False)
        print("trades ->", os.path.abspath(a.csv))


if __name__ == "__main__":
    main()
