# ZSPD Polymarket 每日操作指南

---

## 每日流程

### 开盘前（建议 07:30 CST）

```bash
# 1. 拉取今日 GFS 预报（v5 必需，提供太阳辐射/云量特征）
python -m core.fetch_weather --forecast-only

# 2. 刷新今日 Polymarket Token 元数据（获取今日合约 ID）
python -m core.fetch_tokens
```

### 交易时段（08:00–23:59 CST）

```bash
# Dry-run（测试信号，不真实下单）
python -m strategies.modify_str --dry-run --version v5

# Live 模式
python -m strategies.modify_str --version v5
```

**程序运行期间的自动更新情况：**

| 数据 | 是否自动更新 | 说明 |
|------|------------|------|
| WU 实时温度 | ✅ 每槽自动拉取 | 每 30 分钟自动调用 `refresh_today_wu()`；DB 有则直接读，没有则重试 3 次，再失败则提示手动输入 |
| ERA5/GFS 预报 | ❌ 仅启动时加载一次 | 每天变化不大，启动前运行 `fetch_weather` 即可 |
| 历史 lag 特征 | ❌ 仅启动时加载一次 | 正确行为：当天的历史 lag 不会改变 |
| Token 元数据 | ❌ 仅启动时加载一次 | 启动前运行 `fetch_tokens` 即可 |

> WU 温度拉取失败时会提示手动输入：输入当前摄氏度（如 `24.5`）回车即可，留空则跳过该槽。

### 收市后（建议 00:10 CST 次日）

```bash
# 完整重训练流水线（约 8-12 分钟）
python -m scripts.daily_retrain --version v5
```

流水线自动执行：
1. 拉取今日完整 WU 数据（等待至 ≥20 条读数）
2. 重建 `daily_labels.db`（含今日结算温度）
3. 重建 `intraday_features.parquet`
4. 重训练 v5 模型（walk-forward CV，约 6-8 分钟）
5. 更新 v4 季节性校准系数

---

## 参数速查

```bash
# 策略参数（默认值即可，一般不需要改）
python -m strategies.modify_str --version v5 \
    --min-edge 0.05 \    # 模型信号最小边际（默认 0.05）
    --size 1.0 \          # 每笔基础仓位 $（默认 1.0）
    --max-per-slot 10     # 每 30 分钟最多信号数（默认 10）

# 重训练参数
python -m scripts.daily_retrain --version v5 \
    --no-fetch            # 跳过 WU 拉取（已是最新）
    --no-train            # 只重建数据，不训练
    --backtest            # 训练后运行近 30 天回测验证
    --date 2026-05-06     # 补训某历史日期
```

---

## 手动补数据

```bash
# WU 数据缺失时手动输入温度
python -m ml.pipeline --temp 24.5           # 当前时间槽
python -m ml.pipeline --temp 24.5 14:30     # 指定时间

# 单独重建某个步骤
python -m ml.data.build_labels              # 仅重建标签
python -m ml.data.build_intraday_features   # 仅重建特征
python -m ml.models.train --version v5      # 仅重训练
```

---

## Windows 自动化（Task Scheduler）

每日 00:10 CST（= 16:10 UTC）自动触发重训练：

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Zihua\Project\Pre_market\.venv\Scripts\python.exe" `
    -Argument "-m scripts.daily_retrain --version v5" `
    -WorkingDirectory "C:\Zihua\Project\Pre_market"

$trigger = New-ScheduledTaskTrigger -Daily -At "16:10"

Register-ScheduledTask -TaskName "ZSPD_Daily_Retrain" `
    -Action $action -Trigger $trigger -RunLevel Highest
```
