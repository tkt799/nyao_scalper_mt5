# research/ — Nyao Scalper 离线回测与优化工作台

这个目录是对 `nyao_scalper.mq5`（v43.0）的**独立复算**：用真实券商 XAUUSD M1/M5 数据，
把 EA 的决策逻辑逐行搬成 Python 引擎，跑全量回测、逐月循环、参数消融与走步优化。

**结论请直接看 [`BACKTEST_REPORT_zh.md`](BACKTEST_REPORT_zh.md)。**

## 为什么要自己写引擎

本环境没有 Windows / MT5，跑不了 MetaEditor 与策略测试器；离线引擎也让我们能做
MT5 优化器不方便做的事情（逐月循环重启、成本敏感性网格、参数消融、状态透视）。
**上线前请务必在 MT5 策略测试器（每笔 tick 真实 tick）复跑同一份参数。**

## 目录

| 路径 | 说明 |
|---|---|
| `BACKTEST_REPORT_zh.md` | 主报告（评价 / 循环回测 / 最佳参数 / 改进项） |
| `recommended_settings/tuned-lowrisk-nomartingale.set` | 推荐参数（走步优化 + 样本外验证） |
| `data/` | 归一化行情 `*.csv` + 券商参数 `*.json`（**CSV 不入库**，见下） |
| `data_and_tools/` | 引擎、CLI、矩阵/消融/优化脚本 |
| `results/` | 所有原始数字（JSON/CSV）与图表 |

## 行情数据（需要自行下载，不入 git）

三份**公开**的券商数据（脚本头注释里也有来源）：

| 用途 | 仓库 | 文件 |
|---|---|---|
| 主 M1 | https://github.com/vudo805/XAUUSD-M1 | `data/XAUUSD_M1_2026.csv` |
| FTMO M1 | https://github.com/Gianniskas/mt5-m1-bars-us500-eurusd-xauusd | `XAUUSD_m1.csv`, `spreads_summary.csv`, `symbol_specs.csv` |
| M5（自带点差列） | https://github.com/Anilk2978/xauusd-csv | `XAUUSDm_M5_*.csv` |

按 `prepare_data.py` 期望的目录摆放（本工作台里的实际路径）：

```
/tmp/raw/
├── XAUUSD_M1_2026.csv                     # vudo805
├── ftmo/XAUUSD_m1.csv
├── ftmo/spreads_summary.csv
├── ftmo/symbol_specs.csv
└── m5/xauusd-csv-main/XAUUSDm_M5_202312010705_202608211705.csv
```

> 原始 CSV 体积大（约 50 MB）且属券商行情数据，因此**不纳入版本库**；
> `prepare_data.py` 会生成 `data/` 下的三个归一化数据集（含点差模型与合约参数）。

## 复现步骤

```bash
python3 -m venv venv && ./venv/bin/pip install numpy pandas matplotlib

cd data_and_tools
python prepare_data.py --raw /tmp/raw --out ../data      # 1. 归一化行情 + 券商参数
python backtest.py --profile safe --data xauusd_m1_2026H2 # 2. 单次回测
python run_matrix.py --mode profiles                      # 3. 5 档案 × 3 数据
python run_matrix.py --mode rolling                       # 4. 滚动窗口
python run_matrix.py --mode variants                      # 5. 参数消融
python optimize.py                                        # 6. 走步优化（约 10 分钟）
python collect_results.py && python loop_windows.py        # 7. 报告用的全部数字
python make_figures.py                                    # 8. 图表
```

### 常用 CLI 示例

```bash
# 关掉对冲链（本报告的第一结论）
python backtest.py --profile default --overrides EnableHedgeChain=false

# 模拟更差的成交条件：点差 ×1.5、止损额外滑点 0.05 美元/盎司
python backtest.py --profile safe --spread-mult 1.5 --slippage 0.05

# 直接用推荐参数文件覆盖
python backtest.py --profile default \
  --overrides-file ../recommended_settings/tuned-lowrisk-nomartingale.set --csv trades.csv
```

## 引擎的已知近似（读结果时必须知道）

1. bar 内路径 = `开 → (低→高 或 高→低) → 收`，每根 bar 4 个管理点；同 bar 内 SL/TP 同时触及时按先止损处理。
2. 成交价 = bar 价格 ± 半点差；无佣金（FTMO 金属免佣）；无隔夜利息。
3. 新闻 / 交易时段 / 收盘前 / 杠杆守卫 = 关闭（离线无日历，等价于 MT5 测试器行为）。
4. 持仓管理每 bar 4 次（实盘每 tick）。这对依赖"极小盈利保本单"的配置最敏感。

更多细节见 `data_and_tools/nyao_engine.py` 顶部的 docstring。
