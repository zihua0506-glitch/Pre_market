# 预测市场套利与建模 — Overview

本文梳理预测市场(Polymarket / Kalshi / Hyperliquid 等)上可行的套利与建模方向,按"市场类别 × 策略类型"展开,并给出落地难点与可尝试路径。

---

模型--预测值
组合执行 策略
速度/回测分析

## 1. 不同交易所之间的套利 (Cross-Venue Arbitrage)

核心逻辑:同一事件合约在 Polymarket、Kalshi、Hyperliquid (HYPE)、PredictIt 等不同场所上,因流动性、用户结构、定价机制(AMM vs CLOB)、监管套利成本(KYC、链上 gas、出入金)的差异,会出现 **隐含概率不一致**。买高卖低或反向对冲即可锁定利润。

### 1.1 加密货币类合约 (BTC/ETH 价格区间、清算事件等)

- **现状**: 价差确实存在,且主流币高频波动会反复制造小窗口。
- **难点 — 硬件 / 基础设施门槛过高**:
  - Polymarket 是链上撮合 (Polygon),Kalshi 是中心化撮合,Hyperliquid 是 L1 + 永续。三者**结算路径不同**,要做真正的对冲必须同时在三套基础设施上做低延迟订单管理。
  - 链上一笔确认延迟 ≥ 2s,期间价格已经回归。要做有效套利至少需要:私有 RPC + 自托管节点 + 与 CEX co-location 级别的网络。
  - Gas / 滑点 / Maker rebate 差异会吃掉 80% 以上的理论 spread。
- **结论**: **不建议优先做** — 单人/小团队在没有专线和私链节点的情况下,理论收益会被基础设施成本吃光。

### 1.2 天气 / 体育 / 其他长尾领域

- **特点**:
  - 价差小但**持续时间长**(几分钟到几小时,而不是几秒),对延迟不敏感。
  - 流动性低 → 大资金进不来 → 套利窗口反而稳定存在。
  - 同一比赛在 Kalshi 和 Polymarket 上常出现 1–3¢ 的稳定 mispricing,赛中波动期(进球、伤停、暂停)更明显。
- **可行路径**:
  1. **Pre-game 静态扫描**: 拉取两家 API 的同一赛事 moneyline / spread,折算到隐含概率,价差 > 手续费 + 滑点即下单。
  2. **In-play 动态套利**: 比赛中事件驱动(得分、罚牌)会让一方反应慢 ~10–30s,这是窗口。学术研究显示 in-play 套利平均存在 ~13s,占比赛时间的 4–5%。
  3. **跨市场对冲**: 把传统 sportsbook (DraftKings/FanDuel) 当 Layer 0,prediction market 当 Layer 1 来对冲也算一种延伸。
- **结论**: **空间小但稳定**,可作为底仓策略;关键工程不在低延迟,而在 **同时维护多账户 + 准确的费用建模 + 自动化下单**。

### 1.3 工程优先级建议

| 维度 | 加密 | 天气 | 体育 |
|---|---|---|---|
| 理论 edge | 中 | 小 | 中 |
| 实施门槛 | 极高 | 低 | 中 |
| 资金容量 | 大 | 小 | 中 |
| **建议** | 暂缓 | 先上 | 优先 |

---

## 2. 预测模型 (Forecasting Alpha)

不是套利,而是**比市场更准**地预测事件结果,在 mispriced 的合约上下注获取 EV。

### 2.1 加密货币 — 已尝试且效果不佳

- **试过**: 5min / 15min K 线建模,机器学习 (LGBM/XGBoost),趋势跟踪,扫尾盘 (close auction-like flows)。
- **为什么不 work**:
  - Polymarket / Kalshi 的 BTC 区间合约**本身就是从 CEX 现货价推导**的,合约价格 ≈ ∫(现货价格分布)。你的模型要赢,必须**比现货市场本身更准**地预测短期价格 — 而 5–15min 是机构 HFT 的主战场,零售方向几乎没有 alpha。
  - 即便模型有 edge,合约的 bid-ask + 提现摩擦经常 > edge 本身，还可能出现一些极端反转事件（比如机构控制价格等行为），长期来看是负期望的行为。
- **结论**: 短周期方向性预测**放弃**。如果还想做,只能转向 **波动率/分布形状** 类合约(touch / range / 二元),用 IV 和 realized vol 之间的 mismatch 做。

### 2.2 天气 — 大尺度 OK,小尺度需要找新切入点

- **已 work 的大尺度方向**:
  - 跟踪官方气象站 + 跑 GFS 31-member ensemble / ECMWF,用集成成员落入合约 bracket 的比例当概率,Kelly 下注。
  - HRRR 在 0–24h 短临精度领先,GFS/ECMWF 适合 next-day directional bias。
  - 这类 setup 在 Kalshi `KXHIGH` 系列(每日最高温) 上有成熟开源实现可参考。
- **小尺度怎么做** — 可探索方向:
  1. **本地传感器纠偏**: NOAA 官方站 vs 你自建/购置的 personal weather station (PWS, e.g. Davis/Ambient/Tempest) 的 bias 是稳定的。结算用的是 NOAA 站,但 PWS 网络(Weather Underground / WeatherFlow Tempest)的实时数据**比 NOAA 公开 API 提前 5–30 分钟**,可在临近结算时用领先信号 nudge 仓位。
  2. **Nowcasting 模型**: 短临(0–3h) 用雷达回波 + ML(类似 Google MetNet, DeepMind GraphCast)做插值,在结算前几小时拿微弱 edge — 这块开源工具链(`pysteps`, `MetNet-3`)已经够好。
  3. **Microclimate 套利**: 当合约绑定特定机场气象站(如 KORD 芝加哥奥黑尔)时,用周边 50km 内多个 PWS 加权回归到该站的历史偏差,做更准的 point estimate。
  4. **极端事件 underpricing**: 散户在 tail bracket(如极冷/极热 2°F 区间) 的下注稀薄,如果 ensemble spread 显示 >5% 概率,常能找到 mispriced 合约。
- **结论**: 小尺度 alpha 主要靠 **数据领先性 + 本地化偏差校正**,不是建更复杂的模型。
- **详细策略**: 见 [weather_strategies.md](weather_strategies.md) — 按 daily / weekly / monthly / yearly 时间尺度展开 20+ 条具体策略,含数据源、入场退出逻辑、风控。

### 2.3 体育 — 多种方向

#### (a) 跟单 (Copy Trading) — 详细实现

**思路**: 找到长期跑赢市场的 sharp 账户,实时复制其下单。Polymarket 链上数据**完全公开**,这是它相比 CEX 的独特优势。

**实现步骤**:

1. **筛选 sharp 钱包**:
   - 抓 Polymarket 的 GraphQL API (`https://gamma-api.polymarket.com`) 或 The Graph 子图,拉取所有体育合约近 6–12 个月的成交记录。
   - 按钱包聚合,计算每个钱包的:
     - 总 PnL、ROI、胜率
     - **CLV (Closing Line Value)**: 该钱包入场价 vs 合约结算前最后成交价的差值,正 CLV 是长期盈利的真正先行指标 — 比胜率更可信。
     - 持仓时间分布(快进快出 vs 长持)、单边/对冲行为
   - 过滤条件示例:近 200 笔成交,ROI > 5%,平均 CLV > 1¢,无明显 wash trading(同钱包对敲)。
2. **实时监听**:
   - 订阅 Polygon 链上事件 (`OrderFilled`),或 WebSocket `wss://ws-subscriptions-clob.polymarket.com`。
   - 当目标钱包成交时,抓出 `(market_id, side, price, size)`。
3. **复制下单逻辑**:
   - **不要无脑全抄**: sharp 经常因为信息不对称(伤病、内幕)而下注;在事件未公开时复制是 +EV,在事件已公开后跟单是 -EV(已被反映进价格)。所以**只复制原账户成交价 ±1¢ 内的当前盘口**。
   - 仓位缩放:按你账户净值比例缩,叠加 Kelly 上限(`f* = (bp-q)/b`,`b` 用 sharp 历史 ROI 估)。
   - 跟随退出:目标钱包平仓时,你也平仓(或设独立止盈)。
4. **多账户聚合**:
   - 单一 sharp 不稳定,挑 5–20 个不相关的 sharp 做加权(按 Sharpe / CLV),信号为多个 sharp 投票一致时才执行。
5. **风控**:
   - 单合约仓位上限、日 drawdown stop、流动性过滤(orderbook depth < $X 不跟)。

**坑**:
- Kalshi 是 KYC 中心化,没有公开链上数据,跟单只能在 Polymarket 做。
- 可能跟单到机构 market maker — 它的 PnL 来自做市价差,你跟单只会被吃手续费。要从 fill pattern 区分(MM 是双边持续报价,sharp 是单边大单)。

#### (b) 其他建模方向

**已尝试 — Elo 评分 + 机器学习 (NBA / NCAA)**:
- 模型相比 trivial baseline(直接跟盘口/胜率) 有**统计意义上的提升**,胜率/log-loss 优于基线。
- 但在转化为**实际下注策略**时未跑通 — 模型给出的概率 edge 在扣除手续费/滑点/CLV 衰减后无法稳定盈利,主要瓶颈在**下注规则**,不在预测精度本身。
- **可进一步研究的方向**(下面 1–2 条都是针对这个瓶颈的):
  - 改用 **CLV 作为目标函数** 训练 / 验证模型,而不是用比赛结果 — 因为最终能否盈利取决于"是否击败 closing line",而不是预测对错。
  - **下注尺寸优化**: Kelly / fractional Kelly + edge 校准 (Platt scaling / isotonic) — 模型经常过度自信,直接 Kelly 会被放大错误。
  - **市场选择**: 不要在所有比赛都下,只在 **edge × 流动性 × 历史 calibration** 三者都满足的子集下注;NBA regular season 里 80% 的比赛市场已经很有效,真正 mispriced 的可能只有 5–10%。
  - **从 moneyline 转向 props / 二级市场**: team-level 市场效率太高,把同一套 Elo 特征改造去做球员 props (PRA over/under)、节间盘、衍生盘,流动性差但定价粗糙。

1. **Elo / Glicko-2 评分系统**:
   - 经典且强基线。Glicko-2 比 Elo 多一个不确定度维度,小样本(球员/新队伍)更稳。
   - 对网球、CS:GO、足球都有公开 benchmark,胜率预测 accuracy 普遍 65–70%。
   - 用法:Glicko 输出概率 vs 市场隐含概率,差 > 阈值就下注 — **注意**:这是"已尝试方向"踩到的坑,真正的难点在阈值和仓位,不在 Glicko 本身。

2. **Ensemble / Stacking 模型**:
   - Random Forest + CatBoost 软投票在已发表的论文中能到 90%+ 准确率(特定数据集);实战要打折,但 stacking 几个弱学习器(Elo + xG-based + injury feature) 比单一模型稳。
   - 与上面"已尝试"对应:即便 stacking 让 accuracy 再涨 1–2%,如果下注策略不变,PnL 改善有限 — 优先级低于改下注规则。

3. **Player Props 建模**:
   - team-level 市场被打得很有效,**球员维度 (props)** 流动性差、定价粗糙,是散户/小团队的真正 edge 区。
   - e.g. NBA 球员篮板/助攻 over-under,用 game pace + 对位 + 出场分钟数预测,可对比 Kalshi/Polymarket 的 props 报价。

4. **Closing Line Value (CLV) 框架**:
   - **不以胜率为目标,以击败 closing line 为目标**。如果你的入场价始终优于最终结算前价格,长期一定盈利。
   - 实践:跟踪几家 sportsbook 的 line movement,对比你模型给的概率 — 如果你领先 line 30 分钟,这就是 alpha。

5. **Sharp Money / Reverse Line Movement (RLM)**:
   - 当 ticket% 偏向一边但 handle% 偏向另一边(大资金反向) → sharp 在动。
   - 数据源:VSiN、Outlier.bet、Pikkit 等聚合公盘 ticket / handle 数据。
   - 信号:RLM 出现时,跟随大资金方,在 prediction market 下注,等市场修正。

6. **In-play 动量 / 反转模型**:
   - 比赛中得分、控球率、xG 实时输入 → 实时更新胜率 → 与盘口比较。
   - 关键是 **数据延迟** — Sportradar/Betradar 官方数据有授权门槛,可用 ESPN GameCast / 个人爬虫但慢 5–30s。

7. **市场做市 (Market Making)**:
   - 不预测方向,在低流动性合约上双边挂单赚 spread。
   - 需要库存管理 + 自动撤单 + 关键时刻(末节、加时)收紧报价。
   - Kalshi 90% 体量来自 NFL/NBA/MLB,长尾合约(MMA、tennis future)做市竞争小。

8. **杠杆 + 高确信度**:
   - OG App 等新平台支持高流动性合约 10x 杠杆,资金效率高,但本质是放大模型 edge,不是独立策略。

---

## 3. 落地优先级建议

按 **(实施成本) × (预期 Sharpe) × (容量)** 评估:

| 优先级 | 方向 | 周期 |
|---|---|---|
| P0 | 体育跨场所 pre-game 静态套利 (Kalshi ↔ Polymarket) | 1–2 周 MVP |
| P0 | Polymarket 体育 sharp wallet copy trading 框架 | 2–4 周 |
| P1 | 天气 nowcasting + PWS 数据领先策略 | 4–6 周(需调数据源) |
| P1 | NBA/NFL **下注策略迭代** (CLV 目标 + 仓位校准 + 市场筛选,基于已有 Elo+ML 模型) | 2–3 周 |
| P1 | NBA/NCAA 模型迁移到 player props / 节间盘等低效率市场 | 4–6 周 |
| P2 | 体育 in-play 动态套利(需要低延迟数据) | 投资基础设施后做 |
| P3 | 加密短周期建模 | 暂缓 |
| P3 | 加密三场所链上套利 | 除非有专线/co-lo,否则不做 |

---

## 4. 数据 / 工具栈备忘

- **Polymarket**: Gamma API, CLOB WebSocket, The Graph 子图,Polygon RPC
- **Kalshi**: 官方 REST + WebSocket API(需 KYC US 账户)
- **Hyperliquid**: SDK + WebSocket
- **天气**: NOAA / NWS, Open-Meteo (GFS ensemble), HRRR via NOMADS, PWS via Weather Underground / Tempest API
- **体育数据**: ESPN(免费但延迟)、Sportradar(付费)、nflfastR / nbastatR / FBRef(历史)
- **盘口聚合**: OddsJam, Sharp.app, BettoredgeBookies
- **CLV / sharp tracker**: Pikkit, Outlier.bet, VSiN

---

## 5. 已实现 — ZSPD 天气合约系统

> 当前运行的完整实现，对应代码在 `strategies/modify_str.py`、`ml/`、`scripts/`。

### 5.1 系统概览

针对 Polymarket 上海浦东（ZSPD）每日最高/最低温合约，融合两类信号：

| 信号 | 来源 | 逻辑 |
|------|------|------|
| **Tier 1 确定性** | WU 实时观测 | 物理锁定：obs_tmax ≥ T−0.5 → ge_T 必然结算 YES，立即下单 |
| **Tier 2 模型** | LightGBM 分位数预测 | `edge = model_prob − market_price`，edge 足够大时入场 |

**模型版本：**

| 版本 | 特征 | 方法 | 说明 |
|------|------|------|------|
| v3 | 35列（WU-only） | Walk-forward CV | 推荐基础版 |
| v4 | v3 + 月度校准 | 推断时刚体平移 | 修正春/冬季节性高估（+0.5–1.4°C） |
| v5 | 63列（WU+ERA5） | Walk-forward CV | 生产版，含太阳辐射/云量 |

**信号过滤关键规则：**
- `ge YES` 需 `edge ≥ 0.30`（tmax 模型对 ge 合约系统性高估 15–26%）
- `slot < 8h` 禁用 tmax 模型信号（无日内观测，冷启动偏高）
- 确定性信号 YES：`edge > 0.02`；确定性 NO：`YES市价 > 0.01`

**回测结果参考（2026-03-13 → 2026-04-28，base_size=$1）：**
- 总 PnL：+$259，45天，日均 Sharpe > 2.0
- alpha 主要来源：确定性 NO（eq/le 合约 YES 市价 > 1¢ 时买 NO）

---

### 5.2 每日操作

**开盘前（07:30 CST）**

```bash
python -m core.fetch_weather --forecast-only   # 拉取今日 GFS 预报（v5 必需）
python -m core.fetch_tokens                    # 刷新今日合约 Token ID
```

**交易时段（08:00–23:59 CST）**

```bash
python -m strategies.modify_str --dry-run --version v5   # 测试信号（不下单）
python -m strategies.modify_str --version v5             # Live 模式
```

> WU 温度**自动更新**：每槽先查 DB，缺失则自动调用 `refresh_today_wu()` 重试 3 次，全部失败才提示手动输入。

**收市后（00:10 CST 次日）**

```bash
python -m scripts.daily_retrain --version v5   # 完整重训练流水线（约 8-12 分钟）
```

流水线执行：① 拉取 WU 数据 → ② 重建 `daily_labels.db` → ③ 重建 `intraday_features.parquet` → ④ 重训练 v5 → ⑤ 更新 v4 季节性校准系数

**Windows 自动化（Task Scheduler，16:10 UTC = 00:10 CST）：**

```powershell
$action  = New-ScheduledTaskAction `
    -Execute "C:\Zihua\Project\Pre_market\.venv\Scripts\python.exe" `
    -Argument "-m scripts.daily_retrain --version v5" `
    -WorkingDirectory "C:\Zihua\Project\Pre_market"
$trigger = New-ScheduledTaskTrigger -Daily -At "16:10"
Register-ScheduledTask -TaskName "ZSPD_Daily_Retrain" -Action $action -Trigger $trigger -RunLevel Highest
```

---

### 5.3 手动补操作

```bash
# 手动输入温度（WU 拉取失败时）
python -m ml.pipeline --temp 24.5 14:30

# 单步重建
python -m ml.data.build_labels                 # 仅重建标签
python -m ml.data.build_intraday_features      # 仅重建特征
python -m ml.models.train --version v5         # 仅重训练

# 回测验证
python -m ml.backtest.backtest --version v5 --start 2026-03-13 --end 2026-05-01
```

---

## 参考资料

- [Statistical Arbitrage in the Sport Market (Harbourfront Quant)](https://harbourfrontquant.substack.com/p/statistical-arbitrage-in-the-sport)
- [Exploiting Arbitrage Opportunities in Live Sports Betting (Princeton thesis)](https://theses-dissertations.princeton.edu/entities/publication/437f7731-67ad-4af7-a1e6-648c1d003d46)
- [Can You Arbitrage Live Betting With Prediction Market Prices? — BettorEdge](https://www.bettoredge.com/post/arbitrage-live-betting-prediction-market-prices)
- [Market Making on Prediction Markets: Complete 2026 Guide](https://newyorkcityservers.com/blog/prediction-market-making-guide)
- [Polymarket vs Kalshi Explained: Liquidity, Regulation, Strategies (QuantVPS)](https://www.quantvps.com/blog/polymarket-vs-kalshi-explained)
- [polymarket-kalshi-weather-bot (GitHub, GFS ensemble + BTC microstructure)](https://github.com/suislanchez/polymarket-kalshi-weather-bot)
- [Kalshi-Weather-Forecasting-Financial-Trading (GitHub)](https://github.com/akshatgurbuxani/Kalshi-Weather-Forecasting-Financial-Trading)
- [Kalshi Weather Markets Help](https://help.kalshi.com/markets/popular-markets/weather-markets)
- [A study of forecasting tennis matches via the Glicko model (PLOS One)](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0266838)
- [Sports Ratings Guide: Elo, Glicko, RPI (Pro Sport Stance)](https://prosportstance.com/sports-ratings-guide-elo-glicko-rpi-and-strength-of-schedule/)
- [Closing Line Value Explained (TheLines)](https://www.thelines.com/betting/closing-line-value/)
- [How to Use Closing Line Movement to Identify Sharp Activity](https://www.underdogchance.com/how-to-use-closing-line-movement-to-identify-sharp-betting-activity/)
- [Prediction Markets 2025 (MONOLITH on Medium)](https://medium.com/@monolith.vc/prediction-markets-2025-polymarket-kalshi-and-the-next-big-rotation-c00f1ba35d13)
- [Jump Trading Stakes Bets on Prediction Market Boom](https://www.blockhead.co/2026/02/10/jump-trading-stakes-bets-on-prediction-market-boom-with-kalshi-polymarket-deals/)
