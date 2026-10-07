#!/usr/bin/env python3
"""Properly sliced loop ("循环") windows: each window is run in isolation on a
$1000 account, then the windows are chained to show the compounded path."""
import json, os, sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nyao_engine import PROFILE_FILES, run_backtest
from collect_results import TUNED, TUNED_BASE

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))

EDGES = {
    "M1-primary": ("xauusd_m1_2026H2", ["2026-05-18", "2026-06-17", "2026-07-17", "2026-08-17"]),
    "M1-FTMO":    ("xauusd_m1_ftmo",   ["2026-02-16", "2026-03-19", "2026-04-17", "2026-05-16"]),
    "M5-vudo":    ("xauusdm_m5_2026H2", ["2026-05-18", "2026-06-19", "2026-07-20", "2026-08-22"]),
}


def main():
    out = {}
    for name, (ds, edges) in EDGES.items():
        df = pd.read_csv(os.path.join(DATA, ds + ".csv"), parse_dates=["datetime"])
        meta = json.load(open(os.path.join(DATA, ds + ".json")))
        out[name] = {}
        for i in range(len(edges) - 1):
            w0, w1 = edges[i], edges[i + 1]
            sub = df[(df.datetime >= pd.Timestamp(w0) - pd.Timedelta(days=12)) &
                     (df.datetime < pd.Timestamp(w1))]
            key = f"{w0}..{w1}"
            out[name][key] = {}
            for prof in list(PROFILE_FILES) + ["TUNED"]:
                m = dict(meta); m["trade_start"] = w0
                ov = TUNED if prof == "TUNED" else None
                base = TUNED_BASE if prof == "TUNED" else prof
                res, _ = run_backtest(None, None, base, df=sub, meta=m, overrides=ov)
                s = res.stats
                out[name][key][prof] = dict(net=round(s["net_profit"], 2), trades=s["trades"],
                                            dd=s["max_dd_pct"], pf=s["profit_factor"],
                                            win_rate=s["win_rate"], final=s["final_balance"])
                print(f"[{name}] {key} {prof:16s} net={s['net_profit']:>9.1f} "
                      f"trades={s['trades']:>5d} DD={s['max_dd_pct']:>5.1f}% PF={s['profit_factor']}")
    json.dump(out, open(os.path.join(RESULTS, "loop_windows.json"), "w"), indent=2)
    print("saved loop_windows.json")


if __name__ == "__main__":
    main()
