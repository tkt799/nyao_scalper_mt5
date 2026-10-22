# Nyao Scalper v43.0 —— 完整运行逻辑解析

> 分析对象：`nyao_scalper.mq5`（v43.0，约 5870 行，单文件 MQL5 EA）
> 定位：**基于指标信号强度评分（0–10 分）的剥头皮 EA**，配合健康度持仓管理、滚动对冲链（马丁恢复）与多层资金防护。

---

## 0. 一句话总览

每个 Tick 走一遍「**防护检查 → 持仓管理 → 信号评分 → 入场**」流水线：

1. **信号侧**：用 EMA(5/12) 趋势、RSI(8) 动量、K 线实体/脉冲、ATR 波动环境、结构突破给当下方向打一个 0–10 的综合分，经平滑、阻尼、门槛升级后，买分与卖分**择大者**过阈值即入场（市价或回撤限价）。
2. **持仓侧**：每 Tick 对持仓做「健康度体检」——趋势/RSI/逆向波动/结构四项打分，驱动保本锁、信号衰减分批平仓、SL 收紧、健康度清仓+虚拟 SL 重进、自适应追踪止盈止损。
3. **恢复侧**：对亏损仓可启动**对冲链**（反向加码腿），用「覆盖→平旧腿→毕业追踪」或「滚动换腿」的方式恢复亏损（马丁性质，高风险）。
4. **防护侧**：权益峰值/目标/最低权益、回撤暂停（指数延长）、篮子止损、点差、新闻、交易时段、收市、杠杆变化、算法交易开关等多层闸门。

---

## 1. 代码地图与核心数据结构

| 区块 | 行号（约） | 作用 |
|---|---|---|
| 输入参数 | 55–248 | 13 个参数组（指标/权重/订单/限价/阻尼/亏损管理/对冲链/动态手数/权益/TP/SL/R:R/追踪/机器人） |
| 全局变量 | 249–425 | 状态标志、指标句柄、4 大结构体、持仓数组、每 K 计数器、信号缓存 |
| OnInit / InitializeEA | 455–670 | 参数校验 → 健康权归一化 → 创建指标 → 接管已有持仓 |
| OnTick | 699–860 | 主循环（见 §2） |
| OnTradeTransaction | 909–1000 | 成交事件驱动记账（限价成交注册 / 全平记账） |
| 信号引擎 | 1164–1806 | CheckForTradingSignal / BuySignal / SellSignal / ComputeRawScore / GetSignalStrength |
| 持仓管理 | 1445–2878 | ManagePositions / 健康度 / ManageLosingPositions / ManageTrailingTPSL |
| 持仓簿记 | 2880–3118 | Register/Remove/Sync/ProcessClosedPosition |
| 下单 | 3120–3250 | OpenPosition |
| 对冲链 | 3251–3765 | ComputeRecoveryLot / OpenChainHedge / ManageHedgeChains / Reseed / Graduate |
| 限价回撤单 | 3767–4008 | ComputeLimitEntryPrice / PlaceLimitEntry / ManagePendingOrders |
| 通用工具 | 4010–4705 | 平/改单、准入守卫、点差、手数、盈亏平衡、单位换算、SL/TP 解析 |
| 环境监控 | 4707–5630 | 新闻、时段、权益系列、收市、杠杆、报表、Algo 开关 |
| 辅助 | 5634–5867 | Discord、仪表盘 |

**四大核心结构体：**

- `SignalStrength`：一次评分的全部分解（趋势/动量/震荡/突破/波动/脉冲/速度/推理文本），最终 `finalScore ∈ [0,10]`。
- `PositionHealth`：持仓体检结果（`healthScore ∈ [0,1]` + 趋势/RSI/逆向 ATR/结构四个布尔或数值 + 原因文本）。
- `ManagedPosition`：EA 自己的持仓台账（入场分、分批级别、保本锁、利润抵扣、对冲链归属/层级/锚定亏损/周期、毕业标记、恢复锁利）。
- `TradeStats`：日报/全期交易统计。

指标句柄（InitializeEA 创建）：**EMA Fast(5)、EMA Slow(12)、RSI(8)、ATR(8)**，均在当前周期。

---

## 2. 生命周期

### 2.1 初始化（OnInit → InitializeEA）

```
OnInit
 ├─ 若 EA_PASSWORD 非空 → 弹密码对话框，验证前 OnTick 全部空转
 └─ 否则直接 InitializeEA
      ├─ 参数校验（手数>0、MaxLot≥BaseLot、至少开一个方向、时段格式）
      ├─ 健康度权重归一化（4 项权重强制和为 1）
      ├─ 创建 EMA/RSI/ATR 句柄
      ├─ 记录 initialBalance / peakEquity / lastPeakEquity / 初始杠杆
      └─ 扫描现有持仓并登记入 managedPositions[]
           （以当前信号分或最低阈值作为“初始分”基线，重建每 K 计数器与最近入场价）
```

### 2.2 每 Tick 主循环（OnTick 执行顺序）

```
OnTick
 ├─ [密码未过] → return
 ├─ 失效本 Tick 信号缓存（_buyStrengthValid/_sellStrengthValid）
 ├─ CheckAlgoTradingStatus()          // 平台“算法交易”按钮被关 → EA 自我停机
 ├─ CheckPeakEquity()                 // 刷新权益峰值；新峰值可解除暂停、重置触发计数
 ├─ CheckTargetEquity()               // 权益达目标 → 标记停机
 ├─ CheckMinTradeableEquity()         // 权益跌破绝对下限 → 标记停机
 ├─ CheckEquityDrawdawn()             // 峰值回撤超限 → 暂停（指数延长）或超次数永久停
 ├─ CheckBasketStop()                 // 浮亏篮子超 % → 平掉非链持仓 + 暂停
 ├─ [停机标记] → CloseAllPositions + DisableAlgoTrading → return
 ├─ CheckTradingHours() / CheckLeverageChange() / CheckMarketClose()
 ├─ 新 K 线分支：
 │    ├─ 把上一根收盘 K 的评分滚入历史（速度跟踪用）
 │    ├─ 重算刚收盘 K 的买卖分
 │    └─ 更新“连续交易 K 线”计数（用于门槛升级）
 ├─ [时段外/杠杆变/临近收市] → 只 ManagePositions → return
 ├─ CheckHighImpactNews()             // 高影响新闻窗口 → 暂停
 ├─ [暂停中] → 未到期只管理持仓；到期则恢复（可发 Discord）
 ├─ ManagePositions()                 // ★ 持仓管理（每 Tick）
 ├─ CheckForTradingSignal()           // ★ 入场信号（可被新 K 门控限制）
 ├─ CheckTradeReport()                // 定时报表
 └─ UpdateDashboard()                 // 图表面板
```

**设计要点**：任何“暂停/停机”状态下，**已有持仓仍被持续管理**（追踪、止损、对冲链照跑），只是不再开新仓——除非触发的是“完全停机”（目标权益/最低权益/触发次数超限），那会全平并关掉算法交易。

---

## 3. 信号引擎（核心中的核心）

### 3.1 单 K 原始评分 `ComputeRawScore(orderType, signalIndex)` → 0–10

对指定 K（0=当前未收盘，1+=已收盘）计算五个组件 + 一项惩罚：

| 组件 | 满分 | 逻辑 |
|---|---|---|
| **趋势 Trend** | 3 | EMA 快线在慢线上方（买）/下方（卖）+`TrendWeight(1.5)`；快线相对 `SlopeLookback`(3) 根前上行（买）/下行（卖）+`SlopeWeight(1.5)`；封顶 3 |
| **动量 Momentum** | 3 | ① RSI 在 50~80（买）/20~50（卖）区间 +`MomentumBaseWeight(1.0)`；② RSI>60（买）/ <40（卖）再 +`MomentumTriggerWeight(0.5)`；③ 当前实体 > 近 `DirectionalBodyLookback`(10) 根均实体 +`BodyMomentumWeight(1.5)`；然后乘以**脉冲增强** `(1 + ImpulseBoostWeight × rawImpulse)`，封顶 3 |
| **脉冲 Impulse** | （0–1，增强器） | `0.5×实体加速度 + 0.3×波幅加速度 + 0.2×同向连续性`，各项相对近 10 根均值、封顶 3 倍；连续性 = `ImpulseLookback`(3) 根内同向 K 占比 |
| **震荡环境 Chop** | 2 | `volRatio = ATR(8) / 近10根ATR均值`：>1.0 → 2 分；>0.8 → 1 分；否则 0 分（**不白送分**）。同时 `volRatio < MinVolRatioToTrade(0.6)` 直接 **return 0**（死市场过滤） |
| **峰值突破 Peak** | 1 | 当前收盘价突破近 5 根（第 1~5 根）最高（买）/最低（卖）极值 → `PeakScoreWeight(1.0)` |
| **波动 Volatility** | 1 | `volRatio > 1.2` → `VolatilityScoreHigh(1.0)`，否则 0 |
| **影线惩罚** | 负分 | 反向影线 / max(实体, 均实体×1.5) × `WickRejectionWeight(1.0)` 从总分扣减（买罚上影，卖罚下影；防追插针） |

```
rawScore = trend + momentum + chop + peak + volatility − penaltyWick   → clamp [0, 10]
```

### 3.2 平滑评分 `GetSignalStrength` → 最终 `finalScore`

为兼顾稳定与灵敏：

```
baseScore    = 近 N 根已收盘 K 的 rawScore 加权平均（N = SignalSmoothingCandles=2，
               权重线性递减：[1]根权重 N，[2]根权重 N-1…）
currentScore = 当前未收盘 K 的 rawScore
finalScore   = baseScore×(1−blend) + currentScore×blend     （blend = CurrentCandleBlend = 0.40）
```

- 每 Tick 结果缓存（买卖各一份），`OnTick` 开头失效，避免重复计算。
- **速度 Velocity**：`finalScore − 上一收盘K的finalScore`；归一化到 [0,1]：`(v + VelocityWindow) / (2×VelocityWindow)`（供面板/手数参考）。
- `reasoning` 字符串记录各组件分解，写日志用。

### 3.3 入场信号判定 `BuySignal()` / `SellSignal()`

对称结构，以买为例：

1. `CheckEntryConditions(BUY, Ask)` 硬性准入（不过 → 0 分，直接出局）。
2. 取平滑分 `strength.finalScore`。
3. **门槛升级**：若前几根 K 连续开过买单（`consecutiveBuyCandles`），阈值 `MinBuySignalScore(4.5)` 每根 +`ConsecutiveCandleThresholdBoost(1.0)`，最多 +`MaxConsecutiveCandleBoosts(3)` 次——防连续追单买在顶。
4. **信号阻尼**（EnableSignalDampening）：
   - 同向每有 1 个亏损持仓，分数 −`LosingPosScorePenalty(1.5)`；
   - 权益距峰值回撤 ≥ `DrawdownThresholdPct(3%)` 时，阈值 +`DrawdownScoreBoost(2.0)`。
5. `调整后分数 ≥ 调整后阈值` → 返回调整后分数，否则 0。

### 3.4 方向仲裁 `CheckForTradingSignal`

- **新 K 门控**（`EnableNewBarEntryOnly`，默认开）：每根新 K 只评估一次入场（持仓管理不受限）→ 信号不“重绘”，回测（开盘价模式）与实盘一致。
- `buyScore > sellScore` → 做多；`buyScore < sellScore` → 做空；相等不动作。
- 根据 `EnableLimitEntry` 决定市价 `OpenPosition` 还是**回撤限价** `PlaceLimitEntry`（虚拟 SL 重进永远市价，保证立刻恢复敞口）。

### 3.5 硬性准入 `CheckEntryConditions`（不过则信号=0）

1. **每 K 限单**：本 K 同向成交数 ≥ `MaxTradesPerCandle(1)` → 拒。
2. **同 K 反向互斥**：本 K 已有反向单 → 拒（一根 K 只走一个方向）。
3. **阻尼硬阻断**：同向亏损持仓数 ≥ `MaxLosingPositionsSameDir(2)` → 拒。
4. **冷却**：连续亏损 ≥ `ConsecutiveLossesBeforeCooldown(3)` 后，禁新仓 `ConsecutiveLossCooldownBars(3)` 根 K。
5. **重复信号距离过滤**：距最近一次同向入场 < `ZonePoints(500)×点值×Buy/SellDuplicateMultiplier(1.5)` → 拒（防同一价位反复开仓）。

---

## 4. 入场执行

### 4.1 `IsAllowedToOpenPosition` 终闸门

停机标记 / 暂停 / 时段外 / 杠杆变化 / 临近收市 / **亏损持仓数 ≥ MaxHoldingLossPositions(2)** / 总持仓数 ≥ `MaxOpenOrders(8)` / OrderSend 锁 / **点差过宽**（固定点数，或自动 = ATR 点数 × `MaxSpreadATRRatio(0.25)`）——任一命中即拒。

### 4.2 手数 `CalculateDynamicLotSize`（回撤恢复型加仓）

```
lot = BaseLotSize(0.01)
    + floor(权益距峰值回撤% / EquityDropPercent(5%)) × LotStepSize(0.01)   // 回撤越深手数越大
```

护栏：仅当 `signalScore ≥ MinSignalStrengthForLot(8.0)`（很强的信号才加码）、且不在冷却、且篮子未在失血时才计步；步数受 `MaxEquityDropLotSteps(2)` 封顶；最终夹在 `[BaseLotSize, MaxLotSize(0.05)]`，并做**保证金可负担性检查**（逐步试探至买得起的最小档）。
⚠️ 这是「回撤后加仓恢复」逻辑，本质带马丁色彩（默认参数下最多 0.01→0.03）。

### 4.3 SL / TP 解析（三选一，优先级从高到低）

1. **独立 R:R 模式**（`EnableRiskReward`）：SL = ATR×`RRAtrMultiplier(1.5)`（或手动距离），TP = SL×`RiskRewardRatio(1.5)`，覆盖手动 SL/TP。
2. 手动 SL（`EnableStopLoss`，默认 10% 权益）/ 手动 TP（默认关闭，$10）。
3. `ConvertToPoints` 支持 美元 / 权益百分比 / 点数 三种单位，按当前手数与 tickValue 换算。

### 4.4 限价回撤入场（`EnableLimitEntry`，默认关）

- 锚点模式：固定 ATR 深度（`LimitEntryATRFraction(0.25)×ATR`）/ 快 EMA / 近段摆动高低点 / SMART（取较近者）；结构锚点不深于 ATR 上限并遵守最小止损距离。
- 同时只挂 1 张；注释里藏入场评分（`NyaoLE|score`），成交后由 `OnTradeTransaction` 恢复并登记台账。
- `ManagePendingOrders` 每 Tick 维护：超过 `LimitEntryExpiryBars(1)` 根 K 未成交→撤；方向分跌破阈值（`LimitEntryCancelOnFlip`）→撤。

---

## 5. 持仓管理 `ManagePositions`（每 Tick，顺序固定）

```
ManagePositions
 ├─ SyncManagedPositions()        // 与经纪商对账（事件漏掉的平仓在此兜底）
 ├─ ManageHedgeChains()           // 对冲链优先（冻结链腿的常规管理）
 ├─ 遍历持仓 → ManageTrailingTPSL(ticket)   // 自适应追踪 TP/SL
 ├─ ManageLosingPositions()       // 健康度亏损管理
 └─ ManagePendingOrders()         // 限价单过期/翻转撤单
```

### 5.1 自适应追踪 TP/SL `ManageTrailingTPSL`

- 链中腿跳过（由链逻辑独占管理）。
- **自适应**：`scoreDelta = 当前信号分 − 入场信号分`，TP 与追踪距离各偏移 `delta × TrailingValueMultiplier(0.2)`——信号走强目标放大/追踪放宽，走弱反之。
- **追踪 SL**：浮利 ≥ 追踪距离后，SL 跟随市价，只朝有利方向移动；可选仅浮利仓才追（`TrailingSLOnProfitableOnly`）；`TrailingEnableBreakEvenLock` 保证 SL 至少在盈亏平衡价（含手续费/库存费）。
- **毕业对冲腿特例**：`hedgeGraduated` 腿用 `HedgeTrailATR(0.5)×ATR` 做追踪距离（大手数腿若用美元距离会贴脸即平）；`hedgeLockProfit > 0` 时 SL 强制托底在“恢复锁利价”之上（对冲覆盖后锁住的那部分利润）。
- 修改失败且浮利 ≥ 3×最小目标 → **紧急平仓**保利润。

### 5.2 亏损仓管理 `ManageLosingPositions`（健康度驱动）

先计算混频指标（EMA/RSI 用 `blend=0.4` 平滑，ATR 用收盘 K 保持稳定），对每个非链持仓：

**① 健康度体检 `EvaluatePositionHealth` → [0,1]**

| 项 | 权重(默认) | 评分 |
|---|---|---|
| 趋势 | 0.40 | EMA 仍顺向？分离度/（0.5×ATR）线性给分 × 斜率因子（走平/反向 ×0.7） |
| RSI | 0.25 | 买需 RSI≥40 / 卖需 ≤60，从阈值外 15 点线性爬升到满分 |
| 逆向波动 | 0.25 | 浮亏 ATR 倍数从 0→`MaxAdverseATR(1.5)` 线性衰减到 0 |
| 结构 | 0.10 | 价格是否跌破近 20 根摆动低点（买）/升破摆动高点（卖）→ 二值 |

新开仓有 `HealthGraceBars(2)` 根 K 宽限期（期间视为满分、跳过一切管理）。

**② 五级管理动作（按序）**

1. **保本锁**：浮利 > 点差成本 × `BreakEvenSpreadMultiplier(1.5)` → SL 移到入场价（只移一次）。
2. **信号衰减分批平仓**（`signalRatio = 当前分/入场分`）：
   - ≤75% → 平 25% 仓位；≤50% → 再平剩余的 50%；≤25% → 清仓，然后走虚拟 SL 重进。
3. **健康度 SL 收紧**：健康比 < `SLTightenMinHealthPct(0.5)` 时，SL 收到 `SLTightenATRMultiplier(2.0)×ATR`（尊重保本锁、只朝保护方向）。
4. **健康度清仓 + 虚拟 SL 重进**：`healthScore < MinHealthScore(0.40)` → 平仓；若同向信号仍 ≥ 入场分 × `ReentryMinSignalPct(0.75)`，立刻市价重进（以更优价格恢复敞口——“虚拟止损”思想）。
5. **连胜利润抵扣 SL**（`EnableProfitOffsetSL`）：持仓浮亏期间，其它单连赢 ≥ `ConsecutiveWinsRequired(3)` 且累计利润 ≥ `MinOffsetProfit($1)` → 按累计利润等额压缩该亏损单的最大风险（SL 向入场收紧）；累计 ≥ 原风险则直接推到保本。

### 5.3 平仓记账 `OnTradeTransaction` + `ProcessClosedPosition`（事件驱动 + 幂等）

- `DEAL_ENTRY_IN`：限价成交 → 登记台账、恢复入场分、更新每 K 计数（市价单已在 OpenPosition 内联登记）。
- 全平（`DEAL_ENTRY_OUT/INOUT` 且持仓已消失）→ `ProcessClosedPosition`（幂等，防事件与轮询双计）：
  - 更新**连亏计数**（≥3 → 冷却 N 根 K；盈利清零计数）；
  - 更新其余浮亏持仓的**连胜利润抵扣**累计（亏单被重置）；
  - 移出台账。

---

## 6. 对冲链 Hedge Chain（滚动马丁恢复，默认开⚠️）

**触发（Phase B）**：独立亏损持仓浮亏 ≥ `HedgeTriggerATR(1.5)×ATR`，且（可选）**反向信号分** ≥ `HedgeMinSignalScore(4.5)`（防插针假突破才加倍）；算出的恢复手数必须**严格大于**原腿（否则对冲无恢复力）且未超 `HedgeMaxLot(0.10)`，否则放弃对冲、交给常规亏损管理。

**开腿**：反向市价单（无 SL/TP 裸腿），登记 `chainId / hedgeLevel / chainAnchorLoss / cycleNum`；可选清掉根仓 SL（`HedgeClearRootSL`）。

**手数**（`HedgeAutoLot` 默认）：
`lot = p×旧腿手数 + p×旧腿亏损 / (HedgeRecoveryATR×ATR 对应的每手货币收益)`，其中 `p = HedgeRecoveryPct(110%)/100`——即价格朝恢复方向走 `HedgeRecoveryATR(1.0)×ATR` 时，新腿盈利能覆盖旧腿亏损的 110%（同时抵掉旧腿同向继续放血）；下限 = 旧腿 + 一步手数，上限 `HedgeMaxLot`。

**管理（Phase A，每 Tick）**：每条链找出「最老腿 + 最新对冲腿」四选一：

| 分支 | 条件 | 动作 |
|---|---|---|
| **COVERED** | 对冲腿盈利 ≥ 110% × 老腿亏损 | 平老腿（亏损被锁住），对冲腿**毕业**：转常规追踪，并把 SL 托底在恢复锁利价 |
| **ROLL** | 对冲腿亏损 且 老腿已恢复到 ≥ `HedgeRollMinProfit($0.5)` | 免费平老腿，反向开下一层对冲（手数更大），层内滚动 |
| **RESEED** | 想滚但层级到顶（`HedgeCycleLevels=2`）或手数触顶，且 `EnableHedgeCycleReset` 且周期数 < `HedgeMaxCycles(3)` | 平掉最深腿的一部分（`HedgeCyclePartialPct 50%`）后开新周期 |
| **EXHAUSTED** | 同上但禁止重播种/周期用尽 | **不强平**，整链释放回常规亏损管理（标记 `noRehedge` 不再对冲），重新被篮子止损覆盖 |
| **STOP** | 链总浮亏 ≤ −`ChainLossStopThreshold`（`HedgeMaxChainLossUSD/Pct` 中更紧者） | 全链平仓认赔 |

链腿在管理期内**独占**：常规追踪与健康度管理全部跳过（`chainId != 0`），篮子止损也**排除**链腿（链的暂时回撤不应触发篮子），由链自身的亏损上限兜底。

---

## 7. 资金与环境防护层（外围闸门）

| 模块 | 逻辑 | 动作 |
|---|---|---|
| **权益峰值** | 新峰值刷新 `peakEquity/lastPeakEquity`，可重置触发计数、解除暂停 | — |
| **目标权益** `TargetEquity` | 权益 ≥ 目标 | 全平 + 停机（关 Algo Trading） |
| **最低权益** `MinimumEquity(20)` | 权益 ≤ 下限 | 全平 + 停机 |
| **回撤保护** `MinEquityPercent(70)` | 权益 < 峰值×70%（或美元回撤上限取更紧） | 暂停 `PauseMinutes×1.5^(n−1)`（封顶 120 分钟），峰值基准改为当前余额；触发超 `MaxMinEquityTriggers` 次 → 停机 |
| **篮子止损** `MaxBasketLossPct(8%)` | 非链持仓合计浮亏 ≥ 权益 8% | 平非链全部 + 暂停 |
| **点差过滤** | 点数 > 上限（固定或 0.25×ATR） | 拒新仓 |
| **新闻过滤** | 经济日历高影响事件 ±30 分钟 | 暂停新仓（管理继续），Discord 通知 |
| **交易时段** | `TradingStartTime~EndTime` | 时段外只管理不开仓 |
| **临近收市** | 收市前 `MinutesBeforeClose(30)` 分钟 | 同上 |
| **杠杆变化** | 杠杆 ≠ 初始值 | 暂停开仓 |
| **Algo 开关监控** | 平台算法交易按钮被关 | EA 尝试自停 |

**暂停机制统一**：`isPaused + pauseStartTime + currentPauseDuration`，到期自动恢复；期间一切持仓管理照常。

---

## 8. 辅助系统

- **密码门**：`EA_PASSWORD` 非空时弹 MQL 对话框（默认空=禁用），验证前 OnTick 空转。
- **Discord 告警**：Webhook 推送暂停/恢复、篮子止损、权益事件、报表等（Embed 配色）。
- **定时报表**：每 `SendReportEveryHour` 小时统计当日/累计胜率、盈亏、极值并输出。
- **图表仪表盘**：分数分解、速度、持仓健康、暂停/时段状态等。
- **日志**：`LogPrint` 受 `EnableLogging` 总开关（避免刷屏拖慢）。

---

## 9. 端到端时序（典型一笔交易）

```
新K收盘 ──► 重算上K评分、更新连击计数
   │
Tick ──► 防护检查(权益/时段/新闻/点差…) ──► ManagePositions(链/追踪/健康度/挂单)
   │
   └─► 新K门控放行 ──► BuySignal vs SellSignal
          │  (硬准入 → 平滑分 → 门槛升级 → 阻尼/回撤门)
          ▼
     调整分 ≥ 阈值 且 双向择大 ──► IsAllowedToOpenPosition ──► 动态手数 + SL/TP
          ▼
     市价开仓（或挂回撤限价，成交后事件登记）
          ▼
     每Tick管理：
       · 信号衰减 → 分批平（75%→50%→25%）
       · 浮利超点差 → 保本锁
       · 健康度 < 0.5 → SL收紧；< 0.4 → 全平
       · 全平后信号仍强(≥75%) → 虚拟SL重进
       · 浮亏 ≥ 1.5 ATR(+反向信号确认) → 对冲链启动
            ├ 对冲盈利≥110%旧亏 → 平旧腿、对冲腿毕业追踪(SL托底恢复锁利)
            ├ 老腿恢复+对冲转亏 → 滚动换腿
            ├ 触顶 → 重播种/释放回常规管理
            └ 链亏触底线 → 全链平仓
       · 平仓事件 → 连亏冷却 / 连胜抵扣SL
```

---

## 10. 设计亮点与风险点

**亮点**

1. **评分制入场**而非金叉死叉：多维证据（趋势/动量/脉冲/环境/突破/影线）加权成 0–10 分，阈值可调、可解释（reasoning 全程留痕）。
2. **平滑+当前 K 混合**的信号分，兼顾稳定与灵敏；速度跟踪刻画信号动能。
3. **“论点存续”持仓管理**：入场分作为论点基准，信号衰减分批撤、健康度清仓，而不是死扛到固定 SL。
4. **虚拟 SL 重进**：清仓后若论点仍成立立即以更优价恢复，避免止损即反转的踏空。
5. **多层风控正交叠加**：单笔（SL/健康度）→ 组合（篮子止损）→ 账户（回撤暂停/目标停机）→ 环境（新闻/时段/点差/收市）。
6. **工程细节扎实**：幂等平仓记账、事件+轮询双保险、限价注释携带入场分、保证金可负担性试探、SL 合法性（stop/freeze level）校验、OrderSend 锁防重入。

**风险点（务必理解）**

1. **对冲链 = 滚动马丁**：亏损时反向加码，`HedgeMaxLot`/`HedgeMaxCycles`/链亏损上限是唯一的刹车，默认仍然高风险。
2. **动态手数随回撤放大**（回撤越深手数越大），虽有步数/分数/保证金护栏，本质仍是恢复型加仓。
3. **默认参数组合激进**：对冲链开启 + 动态手数开启 + SL=10% 权益 + 篮子 8%——实盘前应先用 `settings/` 里的 `safe.set` 等预设回测，并强烈考虑关闭对冲链。
4. **每 K 只评估一次入场**（默认）意味着信号确认到下单有 1 根 K 的时滞，剥头皮行情下入场价可能已走一段。
5. 回测请用「开盘价 / 1 分钟 OHLC」模式才与 `EnableNewBarEntryOnly` 的实盘行为一致；`EnableLogging` 建议保持关闭。

---

*注：本文基于源码静态分析整理，行号为 v43.0 版本近似位置。作者声明该 EA 仅供学习实验、无盈利保证（见文件头）。*
