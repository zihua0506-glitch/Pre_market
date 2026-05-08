# 天气预测市场策略详解

按 **时间尺度** × **市场类型** 展开,每条策略给出:可交易合约 / 数据源 / 信号机制 / 入场逻辑 / 退出逻辑 / 风险。

---

## 0. 市场全景 (Polymarket / Kalshi 在 2026-04 的实际分类)

### Kalshi (美国持牌,体量更大)
- **每日最高温 KXHIGH 系列**: NY (Central Park)、CHI (奥黑尔)、MIA、LAX、DEN 等主要城市,2°F bracket
- **每日最低温 KXLOW 系列**
- **每日降雨 / 降雪 Y/N**: 是否超过阈值(e.g. 0.01")
- **月度累计降雪 / 降雨**
- **飓风 / 命名风暴**: 在某日期之前是否形成、是否登陆、季节累计数量
- **极端事件**: 龙卷风月度数、地震月度数、野火数量
- **季节展望**: NOAA 温度展望区间

### Polymarket (链上,全球覆盖更广)
- 30+ 城市的每日高温合约(六大洲)
- 月度降雪 / 降雨
- 飓风登陆概率、命名风暴形成
- **年度排名**: "2026 年是有史以来第几热"
- 北极海冰范围、ENSO 状态、月度龙卷风数
- 地震频次、气温记录是否被打破

### 关键差异
| 维度 | Kalshi | Polymarket |
|---|---|---|
| 流动性 | 高(尤其美城市每日温度) | 中,主要在 macro 类合约 |
| 结算源 | NWS Daily Climate Report 明确指定站点 | 多数同样 NWS,但部分用 NASA/NOAA 年度 |
| Bracket 粒度 | 2°F | 通常 5°F 或更粗 |
| 套利窗口 | 流动性差异 → 同一城市同一日两边能差 1–3¢ | 同上 |

---

## 1. Daily 日内尺度 (0–24h) — 最丰富的策略空间

### 1.1 核心数据源

| 数据 | 频率 | 提前时长 | 用途 |
|---|---|---|---|
| **HRRR** (3km, NOAA) | 每小时一次 | 0–48h | 短临温度/降水主力 |
| **RAP** (13km) | 每小时 | 0–21h | HRRR 的母模型,可对比 |
| **NAM 3km** | 6h | 0–60h | 区域高分辨率 |
| **NBM** (National Blend) | 每小时 | 0–10d | NWS 官方 blend,接近预报员产品 |
| **GFS / GEFS** | 6h | 0–16d | 全球与集合 |
| **ECMWF HRES / EPS** | 12h | 0–15d | 全球最佳确定性,要付费或用 Open-Meteo 间接拿 |
| **METAR (ASOS)** | 1min–1h | 实时 | 机场实测 — Kalshi 多数城市合约就用 ASOS 站 |
| **PWS (Tempest/Davis/Ambient)** | 1min | 实时,**比 NWS API 更新快 5–30 分钟** | 数据领先,关键! |

### 1.2 策略 D1 — 早晨 bracket 重定价

**机制**: 多数 KXHIGH 合约在前一日已开盘,价格基于昨晚 GFS/ECMWF 的隔夜 run。今早 6:00 UTC HRRR 跑出新值 + 早间 METAR 显示 dewpoint / 云量与昨晚预测有偏离时,bracket 概率应当重定价,但 **市场反应通常滞后 1–3 小时**。

**入场**:
1. 6–9am ET 拉取最新 HRRR 0Z run 的当日 max temp 时间序列。
2. 计算每个 bracket 的命中概率(用集合 spread 或对历史 HRRR 误差 σ ≈ 1.5–2°F 做 Gaussian)。
3. 与 Kalshi 当前 bid/ask 对比,差 > 4¢ 且 bracket 流动性 OK 就进。

**退出**: 中午前后(11am ET) 或 NBM 12Z 更新发布时,价格通常已修正 → 平仓。如果当日实测温度提前进入 bracket 上沿,用 D5 策略加码或对冲。

**风险**: HRRR 在临近锋面/雷暴时误差可达 ±5°F,要对锋面日降权。

---

### 1.3 策略 D2 — 午后温度 lock-in

**机制**: 大部分城市的 **真实日最高温出现在 14:00–16:00 ET**。一旦 14:00 实测已经超过 bracket 下沿,bracket 及以下的所有合约都接近确定性事件;但散户经常持仓不动。

**入场**:
1. 跟 ASOS 1 分钟数据流(NOAA CO-OPS/ASOS API 或 Mesonet)。
2. 当 13:30–15:00 实测 T_obs ≥ bracket_low + α(α=0.5°F 安全 margin),且 HRRR 短临显示后续 2h 不会大幅下降 → bracket "YES" 应价 > 90¢。
3. 若市场仍在 80¢ 附近 → 买入。

**退出**: 等结算或跑到 95¢+ 平仓。

**关键细节**: 必须知道结算站点(KNYC=Central Park, KORD=O'Hare 等),这些站和市中心实测可能差 1–2°F。

---

### 1.4 策略 D3 — 午后降温/云量推入

**机制**: HRRR 显示午后云量上升或冷锋过境,会压低实际 max,这种情况盘面定价通常滞后,因为它们看的是 morning 的 forecast high。

**信号**: HRRR 14Z run 的 cloud cover (TCDC) 在午后比 6Z 增加 > 30%,或 dewpoint 突变。

**入场**: 卖出高 bracket 的 YES,买入低 bracket 的 YES。

---

### 1.5 策略 D4 — PWS 数据领先

**机制**: NWS API 的 ASOS 数据有 5–30 分钟滞后(取决于站点上报周期);PWS 网络(Weather Underground / WeatherFlow Tempest API / Ambient Weather Network) **每分钟推送**。在 max temp 临界点(下午 3–5pm),PWS 可以告诉你结算站会落在哪个 bracket。

**实施**:
1. 找结算站附近 5–20 公里内的 3–5 个 PWS,做线性回归得到 PWS → ASOS 的偏差函数 `T_ASOS = a + b * T_PWS`。
2. 实时计算预期 ASOS 温度,与 bracket 比较。
3. 偏差函数每天用过去 30 天数据 refit,避免季节漂移。

**这是天气小尺度最稳定的真正 alpha**,因为它是数据时间差,不是预测能力。

---

### 1.6 策略 D5 — Bracket 期权式组合

**机制**: 把同一日的 N 个 bracket 当作期权链 — 价格之和应严格 = $1。任何偏离就是 free arb。

**实施**:
1. 拉取同城同日所有 bracket 的 mid price,求和。
2. 若 sum < $1,买入全部 → 必赚差额(扣除手续费)。
3. 若 sum > $1,卖出全部 → 同上。

**坑**: 需要保证金能同时持有所有方向;Kalshi 用 ladder margin,组合保证金通常 OK。

---

### 1.7 策略 D6 — 海洋层 / 逆温结构性偏差

**机制**: 沿海城市(LAX, SFO, SAN, BOS)在夏季有海风 / 海洋层 (marine layer),结构性压低 max temp;山区城市(DEN, SLC) 受逆温影响。模型对这些边界层细节经常处理不够。

**信号**: 历史上 KLAX 的 ASOS 实测比 GFS 预报低 1.5–2.5°F(夏季 6–9 月),做 backtest 算出 city-by-city 的 systematic bias,然后下注偏差方向。

---

### 1.8 策略 D7 — 降雨 Y/N 当日合约

**机制**: 降雨是离散事件,bracket 是 binary。HRRR + MRMS (Multi-Radar Multi-Sensor) 实时雷达回波叠加可以判断未来 1–6h 是否会触发结算阈值(常用 0.01" trace)。

**入场**:
- 当 HRRR 1h 累计 QPF(降水量) > 0.005",且 MRMS 雷达显示反射率 > 25 dBZ 的回波正向结算站推进 → "YES" 概率 > 70%。
- 若市场仍在 50¢ 以下 → 买入。

**坑**: 0.01" trace 是非常容易触发的低门槛;真正难的是 "某城市某日 ≥ 0.5"" 这类。

---

### 1.9 策略 D8 — 末段衰减交易

**机制**: 在 EST 22:00 后,日内 max 已确定;远高于实测 high 的 bracket 应该接近 0¢。如果还有人挂买单(tail buyer)就卖给他们;反之,实测附近的 bracket 应接近 100¢,留着等结算。

---

## 2. Weekly 周尺度 (1–7 天)

### 2.1 核心数据源

| 数据 | 用途 |
|---|---|
| **GEFS** (31 ensemble, GFS) | 全球集合 0–16d |
| **ECMWF EPS** (51 ensemble) | 全球最佳集合,7 天内技巧领先 GEFS |
| **NAEFS** (NCEP + 加拿大集合 combined) | 北美 |
| **HWRF / HMON / HAFS** | 飓风专用 |
| **NHC Tropical Weather Outlook (TWO)** | 6h 一次,formation chance % |
| **CPC 6–10d / 8–14d 展望** | 文字 + 概率图,周尺度官方产品 |

### 2.2 策略 W1 — 飓风形成 7 天合约

**市场**: Kalshi/Polymarket "Will a named storm form by [date]?" / "First named storm in basin X"

**信号**:
- NHC TWO 中的 7-day formation chance 是关键先行指标。
- 当 invest 区域(e.g. "Invest 91L") 的 7-day 概率从 30% → 60% 跳升,市场常滞后 30–90 分钟。
- 多模型(GFS / ECMWF / ICON / UKMET)是否一致显示发展。

**入场**:
1. 抓 NHC ATCF / TWO RSS feed,每 6h(0Z, 6Z, 12Z, 18Z) 检查。
2. 大跳变(Δ ≥ 20%) 时立即查 Kalshi/Polymarket 价格。
3. 若市场价格 < NHC 隐含概率 - 5% → 买入 YES。

**退出**: 下一个 6h cycle 后通常已修正。若 NHC 把 invest 升级为 PTC (potential tropical cyclone) 即接近 100% 形成。

---

### 2.3 策略 W2 — Ensemble member counting

**机制**: GEFS 31 个成员中,有多少个落在合约阈值之上 → 直接给出粗估概率。ECMWF 51 成员同理。

**实施**:
- "本周末 NYC 高温 ≥ 85°F" 类合约。
- 拉 GEFS + ECMWF EPS 的所有成员 day-3/day-4/day-5 max temp。
- 计数命中比例,做 Gaussian dressing 平滑。
- 用过去 1 年的 reliability diagram 校准(集合往往低估极端,Platt scaling 校准)。

---

### 2.4 策略 W3 — Multi-model 一致性溢价

**机制**: 当 GFS / ECMWF / ICON 三家在某个事件上一致(比如都给出周末降雨),市场往往已经定价;但当三家**分歧大**时,市场偏向走中间值,而事件实际概率可能偏向 ECMWF(技巧最强)。

**入场**: 当 ECMWF 概率 > GFS 概率 5pp+,且市场更接近 GFS → 买 ECMWF 方向。

**Backtest 必做**: 算出每家模型在 day-3, day-5, day-7 的 brier skill score,加权时按 skill 反向加权。

---

### 2.5 策略 W4 — Tropical wave 早期 sniping

**机制**: 大西洋飓风季 (6–11 月) 经常有从 African Easterly Wave 起源的扰动,在 GFS/ECMWF 中提前 5–10 天就有信号但 NHC 还没标记。早期 ensemble 已有 30% 命中,但 Polymarket "下一个命名风暴"类合约定价仍按昨日 NHC TWO。

**信号**: GFS 850hPa 涡度场 + ECMWF tropical activity tracker (tracks where ensemble members spin up TC genesis)。

---

### 2.6 策略 W5 — 周累积降水

**市场**: "本周 Boston 累积降水 ≥ X""

**入场**:
- 累加 NBM 7-day QPF,作为期望;再加 GEFS spread 给区间。
- 与 bracket 隐含概率比对。
- 极端 bracket(>3"或<0.1") 散户参与少,定价粗,可挑捡。

---

### 2.7 策略 W6 — CPC 6–10d 展望发布日交易

**机制**: NWS Climate Prediction Center 每天下午发布 6–10d 和 8–14d 展望,带 below/normal/above 概率。这是官方 blessed 产品,发布瞬间会引导散户行为。

**入场**: 在 CPC 发布前 30 分钟,根据自己跑的集合预先判断方向;发布后第一时间(< 5 分钟)在 Polymarket monthly 类合约上下注。

---

## 3. Monthly 月尺度 (1–30 天)

### 3.1 核心数据源

| 数据 | 频率 | 用途 |
|---|---|---|
| **CFSv2** (40-member, 9 月先行) | 每日,4 runs | NOAA 季节模型 |
| **ECMWF SEAS5** (51 ensemble) | 月度 | 全球最佳季节模型 |
| **NMME** (CFSv2 + CanCM4 + GEM + GFDL 多模型) | 月度 | NOAA 多模型集合,月度首选 |
| **CPC 月度展望** | 每月 15 号、20 号 | 官方产品 |
| **ENSO state** (Niño 3.4 SST) | 周/月 | 季节预报最大解释变量 |
| **MJO phase** (RMM index) | 每日 | 30–60 天热带振荡,显著影响北美 |
| **NAO / AO / PNA index** | 每日 | 中纬度遥相关 |
| **SSW (突发平流层增温)** | 每日 monitoring | 冬季冷波先行指标,~14 天 lag |

### 3.2 策略 M1 — Climatology 基准 vs 集合偏差

**机制**: 月度合约本质是 P(月度 anomaly > X)。Climatology 1991–2020 给出基线分布,CFSv2/NMME 给出本月预期。

**实施**:
1. 拉历史 30 年同月该城市的 max temp 平均 / 月降雪量分布。
2. 拉 CFSv2 当月 ensemble forecast,得到中心 + spread。
3. 用 climatology σ + ensemble shift,推算 bracket 概率。
4. 对比市场。

**例**: NYC 1 月平均降雪 7"(σ ≈ 5"),CFSv2 显示该 1 月偏暖 1.5°C → 降雪量 expected 下移 ~30%。若市场对 "≥10\"" 仍按历史频率定价 → 卖出 YES。

---

### 3.3 策略 M2 — ENSO-state 偏差

**机制**: ENSO (El Niño / La Niña / Neutral) 状态对北美月度异常有强 well-known 信号:

| ENSO | 北美冬季影响 |
|---|---|
| **El Niño** | 南美/南美洲偏湿,美国南部偏湿、北部偏暖 |
| **La Niña** | 美国南部干、东南偏暖、西北偏湿冷 |
| **Neutral** | 信号弱,主导项是其他遥相关 |

**实施**:
- NOAA CPC ENSO Diagnostic Discussion 每月 2 周四发布。
- 把当前 ENSO state 套入历史合成图(composite),得到地区偏差先验。
- 跟 Polymarket 月度温度/降水合约对比,挑系统性偏离。

**注意**: 散户和媒体倾向放大 El Niño/La Niña 效应,有时反而过度定价 → 反向也有机会。

---

### 3.4 策略 M3 — MJO 相位追踪

**机制**: Madden-Julian Oscillation 相位 1–8 对北美 10–30 天天气有显著调制(尤其冬季)。比如 phase 8/1 favors 西海岸大气河事件,phase 3/4 favors 东海岸偏暖。

**实施**:
- BoM RMM index 每日更新。
- 当 MJO 进入强相位(amplitude > 1.5) 且持续 > 5 天 → 启动该相位的历史合成预报。
- 对应月度天气合约下注。

---

### 3.5 策略 M4 — Mid-month 概率坍缩

**机制**: 月度累积合约(降雪/降水/龙卷风数)在月中已观测一半数据,剩余 15 天的不确定性大幅缩小,bracket 应严格 conditional on 已观测值重定价。

**实施**:
- e.g. 月度降雪 ≥ 12" 合约,月中已下 8" → 还需 4",剩 15 天 GEFS 显示仅 30% 概率达成 → 合约理应价 ~30¢。
- 但市场常在月初的价格附近 anchor,慢于真实贝叶斯更新。

**这是月度类合约最稳定的 setup**,几乎每月都有,只要会做条件概率重算。

---

### 3.6 策略 M5 — SSW (Stratospheric Sudden Warming) 触发交易

**机制**: 冬季(12–3 月) 极地涡旋崩溃事件(SSW) 通常会在 10–21 天后 trigger 北美东部冷波。预测 SSW 本身已有较高 skill (1 周提前)。

**实施**:
- 跟 GFS / ECMWF 60hPa 极地温度 anomaly。
- 一旦预测到 SSW,买入未来 2–4 周内东部低温合约 / 雪暴合约。
- 同时观察 polar vortex displacement vs split,后者影响更深远。

---

### 3.7 策略 M6 — 月度龙卷风/severe weather 数

**市场**: "April US tornado count ≥ 200"

**信号**:
- SPC (Storm Prediction Center) day 4–8 outlook。
- 早春 SPC moderate/high risk 频次 → 月度计数前导。
- ENSO + MJO + 高空急流强度的组合特征。

**坑**: 龙卷风 "count" 由 NWS 调查后 confirm,有 1–4 周报告滞后。要看 LSR (Local Storm Reports) preliminary 数。

---

### 3.8 策略 M7 — 飓风季节累计数

**市场**: "2026 Atlantic season named storms ≥ 20" (年度)

**信号**:
- NOAA 5 月 / 8 月飓风季展望发布日 → 立刻交易。
- CSU / TSR 等学术 group 同期出预测,差异处可挑。
- Caribbean SST + ACE (Accumulated Cyclone Energy) 实时跟踪。

---

## 4. Yearly / 季节尺度 (3–12 月)

### 4.1 主要市场

- "2026 是有史以来第几热的年份"
- "本年度 Atlantic season named storms / hurricanes / major hurricanes"
- "本年度 Pacific season..."
- "Arctic sea ice extent September minimum < X"
- "ENSO: 年底是否进入 El Niño/La Niña/Neutral"

### 4.2 策略 Y1 — 年度温度排名

**机制**: NASA GISS / NOAA NCEI 月度全球温度,12 个月累计后排名。**前 6 个月 + 后 6 个月预测** 已经能很高置信度判断最终排名(尤其 8 月之后基本锁定)。

**实施**:
- 每月初 NASA/NOAA 发布上月数据 → 累加 YTD。
- 用 ECMWF SEAS5 + ENSO state 推剩余月份。
- 比较 Polymarket bracket。

**典型 setup**: 8–9 月数据出来后,市场常在前几个排名上犹豫,但用 YTD + 历史尾月 σ 已可锁定 ±1 名。

---

### 4.3 策略 Y2 — Hurricane season 总数

**实施**:
- 学术预报: CSU (Klotzbach), TSR, NOAA(5 月+ 8 月)。
- ACE 实时累加(NHC 每个 storm 结束更新)。
- 8 月 1 日已经累计的 named storm 数 + 历史 8 月后均数 → 年终估值。

**关键日期**:
- 6 月 1 日(季节开始)
- 8 月 9 日(climatological peak 起点)
- 9 月 10 日(peak)
- 11 月 30 日(季节结束)

---

### 4.4 策略 Y3 — 北极海冰最小值

**市场**: "Arctic sea ice September minimum < 4.5M km²"

**机制**: NSIDC 每日发布海冰范围;5–8 月已能 70% 解释 9 月最小值(用 melt onset + sea ice motion + 海洋热含量)。研究发表的 statistical model (e.g. SIPN — Sea Ice Prediction Network) 是公开 benchmark。

---

### 4.5 策略 Y4 — ENSO 状态切换

**市场**: "Will Niño 3.4 be in El Niño territory by [month]?"

**机制**: NMME 多模型集合月度 ENSO forecast 是黄金标准。Spring barrier(3–5 月) 之前预测困难,夏季后可信度大幅上升。

**实施**:
- IRI ENSO Quick Look(月度)
- NOAA CPC ENSO Discussion(月初第 2 周四)
- 关键阈值: SST anomaly ≥ +0.5°C 连续 5 个月才算 El Niño event。

---

## 5. 通用框架与风险

### 5.1 概率校准 (Calibration)

集合预报往往 overdispersed 或 underdispersed。必须做:
1. 收集历史 forecast vs observed,画 reliability diagram。
2. 用 isotonic regression 或 Beta-binomial reweight 矫正。
3. 校准后才用于 Kelly 仓位。

### 5.2 仓位管理

- **Fractional Kelly (0.25–0.5×)** 控制 drawdown。
- 单合约上限 = NAV × 5%。
- 同源信号(e.g. 同一日多个城市同一 ensemble) 视为一个仓位计算。

### 5.3 结算风险

- **数据来源**: 一定要看清结算条款 — 哪个站、哪个 timezone(local / UTC)、是否包括飞机滑行 ASOS 还是 climatology 站。
- **缺测 / 报告延迟**: NWS Daily Climate Report 偶尔晚 24h+,合约 hold 时间可能拖长。
- **Disqualified contracts**: 极少数情况合约因数据问题作废,资金原路退回但锁定时间长。

### 5.4 流动性风险

- Kalshi 冷门城市的 KXHIGH 单 bracket 深度可能 < $500;大单进就移盘。
- Polymarket macro 类年度合约流动性 OK,但日度类深度差。
- **建议**: maker 挂单为主,taker 仅在确定 edge > spread 时用。

### 5.5 模型成本 / 延迟

- HRRR / GFS / GEFS 都 free,NOMADS / AWS Open Data 有 mirror。
- ECMWF Open Data(从 2022 年起免费提供 IFS 部分数据) 适合个人。
- 自己跑 ECMWF Seasonal 的话需要付费 license 或借 Copernicus C3S API。

---

## 6. 实施路线图建议

| Week | 任务 |
|---|---|
| 1 | Kalshi/Polymarket API 接入,把 KXHIGH 5 城市 + 月度 4 合约 universe 拉出来 |
| 2 | HRRR/GFS/GEFS pipeline (NOMADS + xarray) |
| 3 | 实现 D1 (早晨重定价) + D5 (bracket 求和套利) |
| 4 | PWS 偏差校正 (D4),Tempest/Ambient API |
| 5 | Backtest:用过去 6 个月 forecast + 价格,算 PnL,做 calibration |
| 6 | 加 W1 (飓风 7 天) + W2 (集合计数) |
| 7 | 月度合约 M4 (mid-month 重定价) |
| 8 | 上线小资金($500–2000) 跑 paper / 微仓 |
| 9+ | 加 ENSO/MJO/SSW 高级信号 |

---

## 7. 参考资料

- [Kalshi Weather Markets Help](https://help.kalshi.com/markets/popular-markets/weather-markets)
- [Kalshi Climate / Daily Temperature Hub](https://kalshi.com/category/climate/daily-temperature)
- [Polymarket Weather Predictions](https://polymarket.com/predictions/weather)
- [Polymarket Climate Hub](https://polymarket.com/predictions/climate)
- [NOAA CPC CFSv2 Seasonal Forecasts](https://www.cpc.ncep.noaa.gov/products/CFSv2/CFSv2seasonal.shtml)
- [NOAA HRRR (Rapid Refresh High-Res)](https://rapidrefresh.noaa.gov/hrrr/)
- [NHC Atlantic Tropical Weather Outlook](https://www.nhc.noaa.gov/gtwo.php)
- [ECMWF Open Data (free IFS subset)](https://www.ecmwf.int/en/forecasts/datasets/open-data)
- [Open-Meteo (GFS/ECMWF API,免费)](https://open-meteo.com/)
- [WeatherFlow Tempest API (PWS)](https://weatherflow.github.io/Tempest/api/)
- [Weather Underground PWS Network](https://www.wunderground.com/pws/overview)
- [NSIDC Sea Ice Index](https://nsidc.org/arcticseaicenews/)
- [polymarket-kalshi-weather-bot (开源参考实现)](https://github.com/suislanchez/polymarket-kalshi-weather-bot)
