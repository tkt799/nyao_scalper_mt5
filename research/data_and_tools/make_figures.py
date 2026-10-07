import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from nyao_engine import run_backtest, PROFILE_FILES
from collect_results import TUNED

RES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
FIG = os.path.join(RES, 'results', 'figures')
os.makedirs(FIG, exist_ok=True)
df = pd.read_csv(os.path.join(RES, 'data', 'xauusd_m1_2026H2.csv'), parse_dates=['datetime'])
meta = json.load(open(os.path.join(RES, 'data', 'xauusd_m1_2026H2.json')))

CACHE = os.path.join(RES, 'results', 'equity_curves_cache.csv')
curves = {}
if os.path.exists(CACHE):
    allc = pd.read_csv(CACHE, parse_dates=['ts'])
    for k, g in allc.groupby('name'):
        curves[k] = g.reset_index(drop=True)
else:
    frames = []
    for prof in list(PROFILE_FILES) + ['TUNED']:
        ov = TUNED if prof == 'TUNED' else None
        r, _ = run_backtest(None, None, 'default' if prof == 'TUNED' else prof,
                            df=df, meta=meta, overrides=ov)
        c = pd.DataFrame(r.equity_curve, columns=['ts', 'equity'])
        c['ts'] = pd.to_datetime(c.ts, unit='s'); c['name'] = prof
        c['net'] = r.stats['net_profit']; c['dd'] = r.stats['max_dd_pct']
        frames.append(c)
        print(prof, round(r.stats['net_profit'], 1), r.stats['max_dd_pct'])
    allc = pd.concat(frames)
    allc.to_csv(CACHE, index=False)
    for k, g in allc.groupby('name'):
        curves[k] = g.reset_index(drop=True)

colors = {'aggressive': '#8b0000', 'safe-aggressive': '#d97706', 'default': '#b45309',
          'balanced': '#2563eb', 'safe': '#059669', 'TUNED': '#111827'}
fig, ax = plt.subplots(figsize=(11, 5.2), dpi=130)
for k, cv in curves.items():
    ax.plot(cv.ts, cv.equity, label=f"{k}  ({cv.equity.iloc[-1]-cv.equity.iloc[0]:+,.0f} $)", color=colors[k],
            lw=1.7 if k == 'TUNED' else 1.1)
ax.set_yscale('log')
ax.set_title("XAUUSD M1  2026-05-18 - 2026-08-17 · start $1,000 · base lot 0.01 (log scale)")
ax.set_ylabel("equity, USD (log)"); ax.grid(alpha=.25, which='both'); ax.legend(fontsize=8)
plt.tight_layout(); plt.savefig(os.path.join(FIG, 'equity_profiles_primary.png')); plt.close()

fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), dpi=130)
for ax, (label, prof, ov) in zip(axes, [('safe (as shipped)', 'safe', None),
                                        ('TUNED (hedge chain off)', 'default', TUNED)]):
    grid = []
    for sm in (0.5, 1.0, 1.5, 2.0):
        row = []
        for slip in (0.0, 0.05, 0.10):
            r, _ = run_backtest(None, None, prof, df=df, meta=meta, overrides=ov,
                                spread_mult=sm, slippage=slip)
            row.append(round(r.stats['net_profit']))
        grid.append(row)
    ax.imshow(grid, cmap='RdYlGn', aspect='auto')
    ax.set_xticks(range(3)); ax.set_xticklabels(['slip 0', 'slip .05', 'slip .10'], fontsize=8)
    ax.set_yticks(range(4)); ax.set_yticklabels(['spread x0.5', 'x1.0', 'x1.5', 'x2.0'], fontsize=8)
    for i in range(4):
        for j in range(3):
            ax.text(j, i, f"{grid[i][j]:+,}", ha='center', va='center', fontsize=8)
    ax.set_title(label, fontsize=9)
plt.suptitle("net profit ($) after 3 months - cost sensitivity (XAUUSD M1 primary tape)", fontsize=10)
plt.tight_layout(); plt.savefig(os.path.join(FIG, 'cost_sensitivity.png')); plt.close()
print('figures written')
