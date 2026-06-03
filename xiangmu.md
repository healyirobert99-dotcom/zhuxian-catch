# 项目封版交接文档

封版日期：2026-06-02

项目路径：

```text
/Users/Zhuanz/Documents/量化选股
```

## 1. 当前项目定位

本项目当前定位为：

**A 股主线识别与研究闭环系统。**

系统只做三件事：

1. 判断市场环境是否支持主线扩散；
2. 识别行业主线生命周期；
3. 跟踪早期主线、确认主线、退潮风险和主线载体。

系统明确不做：

- 不猜底；
- 不择时；
- 不选股；
- 不输出买入、卖出、建仓、加仓、减仓、清仓等交易指令；
- 不把个股分当成买入排序。

当前核心目标：

> 经过一段时间的观察和复盘，找到市场环境良好时，出现有延续性的主线板块。

## 2. 历史路线说明

项目曾经研究过：

- A+ 共振策略；
- Minervini / VCP；
- Livermore 突破；
- 双大师交叉策略；
- 低波动收缩后突破；
- 两段式试仓、加仓、止损；
- 个股事件收益回测。

这些内容已经降级为历史研究档案，不再作为当前主系统方向。

当前封版方向是：

```text
市场环境识别
→ 主线生命周期识别
→ 行业 + 概念双维度主线识别
→ 早期主线优先复核
→ 主线载体分层
→ 次日复核闭环
```

## 3. 当前核心模块

### 3.1 日报生成

文件：

```text
scripts/generate_daily_review.py
```

作用：

- 从本地 SQLite 读取 A 股日线；
- 从本地 SQLite 读取东方财富概念板块日线；
- 计算市场环境；
- 计算行业生命周期；
- 计算概念主题生命周期；
- 生成《A 股主线研究日报 V0.3》Markdown；
- 保存行业生命周期快照；
- 保存市场环境快照；
- 支持 T-1 / T-3 / T-5 复核。

生成指定日期：

```bash
python3 scripts/generate_daily_review.py --trade-date 20260601
```

生成最近 10 个交易日：

```bash
python3 scripts/generate_daily_review.py --recent-days 10 --end-date 20260601
```

### 3.2 HTML 渲染

文件：

```text
scripts/render_daily_review_html.py
```

作用：

- 将 Markdown 日报渲染为简约 HTML；
- 保持正文轻量，附录折叠；
- 同名输出 `.html` 文件。

命令：

```bash
python3 scripts/render_daily_review_html.py --trade-date 20260601
```

### 3.3 每日自动化入口

文件：

```text
scripts/run_daily_review_job.py
```

作用：

- 读取 `.env` 或环境变量中的 `TUSHARE_TOKEN`；
- 从 Tushare 获取最近交易日；
- 严格补齐当日日线、估值快照、主要指数和东方财富概念板块；
- 数据完整才生成 Markdown + HTML；
- 数据不完整则停止，不输出缺失日报。

命令：

```bash
python3 scripts/run_daily_review_job.py
```

严格校验项：

- `stock_daily` 当日行数不少于 5000；
- `stock_daily_basic` 当日行数不少于 5000；
- `stock_daily_basic` 不得明显少于日线；
- OHLCV 不得缺失；
- 上证指数、沪深300、中证500、创业板指必须有当日指数数据和足够 MA60 历史。
- `concept_daily` 当日概念板块数据不少于 100 行；
- 概念板块 `pct_change/up_num/down_num` 不得缺失。

### 3.4 早期主线历史验证

文件：

```text
scripts/validate_mainline_early_detection.py
```

作用：

- 使用五年历史数据验证早期主线识别；
- 不验证个股收益；
- 不验证交易买点；
- 用行业 beta 近似验证行业方向是否有延续性；
- 按年度分片运行，避免一次性全量数据过慢。

命令：

```bash
python3 scripts/validate_mainline_early_detection.py --start 2021-01-04 --end 2026-06-01
```

输出：

```text
reports/mainline_early_detection_validation_5y/
```

核心结果：

```text
early_mainline:
  40日跑赢胜率 63.85%
  40日平均超额 2.29%

early_core_env45:
  样本数 2318
  20日跑赢胜率 62.06%
  20日平均超额 1.52%
  40日跑赢胜率 67.59%
  40日平均超额 2.77%
  60日跑赢胜率 71.38%
  60日平均超额 3.68%
```

结论：

```text
企稳重估、重新升温、C级结构修复 + 环境分 >= 45
是当前日报最值得优先复核的早期主线信号。
```

### 3.5 2024 至今行业盲测复盘

文件：

```text
scripts/analyze_2024_mainline_blind_replay.py
```

作用：

- 用封版早期主线规则回放 2024 至今行业信号；
- 不按未来收益倒推规则；
- 评估信号后 20/40/60 日峰值空间和距峰值时间；
- 用传统行业近似观察商业航天、半导体、创新药等主题。

输出：

```text
reports/mainline_blind_replay_2024_now/blind_replay_report_2024_now.md
reports/mainline_blind_replay_2024_now/blind_replay_signals_2024_now.csv
reports/mainline_blind_replay_2024_now/theme_replay_2024_now.csv
reports/mainline_blind_replay_2024_now/major_captures_2024_now.csv
```

核心结论：

```text
行业维度能捕捉部分大级别行业 beta，但对商业航天、AI、创新药、半导体等概念型主线只能近似验证。
```

### 3.6 概念板块盲测复盘

文件：

```text
scripts/analyze_2024_concept_blind_replay.py
```

作用：

- 使用东方财富概念板块代替传统行业；
- 对商业航天、低空经济、创新药、半导体、AI主题做直接盲测；
- 保持同一套早期主线规则；
- 评估信号后 20/40/60 日峰值空间和距峰值时间。

命令：

```bash
python3 scripts/analyze_2024_concept_blind_replay.py
```

输出：

```text
reports/concept_blind_replay_2024_now/concept_blind_replay_report_2024_now.md
reports/concept_blind_replay_2024_now/concept_validation_samples.csv
reports/concept_blind_replay_2024_now/concept_blind_replay_signals.csv
reports/concept_blind_replay_2024_now/concept_theme_replay.csv
reports/concept_blind_replay_2024_now/concept_theme_summary.csv
reports/concept_blind_replay_2024_now/concept_major_captures.csv
```

重要限制：

```text
东方财富概念板块本地可用历史从 2024-12-20 开始；
经过 60 日生命周期计算后，真正可验证的早期信号主要集中在 2025 年以后。
```

核心结果：

```text
商业航天:
  40日平均峰值空间 15.31%
  40日平均超额 9.39%

半导体:
  40日平均峰值空间 15.45%
  40日平均超额 10.02%

AI主题:
  40日平均峰值空间 13.23%
  40日平均超额 3.28%

低空经济:
  40日平均峰值空间 5.54%
  40日平均超额 0.94%

创新药:
  40日平均峰值空间 3.45%
  40日平均超额 -1.78%
```

结论：

```text
概念维度比粗行业近似更精准，尤其适合商业航天、半导体、AI芯片等主题；
创新药在当前概念数据窗口内表现一般，暂不属于强优势方向。
```

### 3.7 概念板块数据同步

文件：

```text
src/ashare_a_plus/tushare_sync.py
src/ashare_a_plus/sqlite_store.py
```

新增 SQLite 表：

```text
concept_basic
concept_daily
concept_member
```

新增同步方法：

```python
TushareSync().sync_concept_basic()
TushareSync().sync_concept_daily("20240101", "20260601")
TushareSync().sync_concept_members("20260601")
```

说明：

- 概念基础信息来自 Tushare / 东方财富 `dc_index`；
- 概念日线来自 `dc_index`；
- 概念成分股来自 `dc_member`；
- 日报只使用 `idx_type = 概念板块`；
- 已过滤 `昨日涨停 / 昨日连板 / 首板 / 跌停` 等动态情绪榜，避免污染主线研究。

### 3.8 概念 + 行业共振验证

文件：

```text
scripts/generate_daily_review.py
```

作用：

- 使用 `concept_member` 概念成分股表；
- 使用 `stock_basic.industry` 股票行业归属；
- 统计每个概念成分股出现频率最高的行业，作为该概念的“对应行业”；
- 读取该行业当日主线等级；
- 在概念主题表中展示：
  - `对应行业`
  - `共振判断`

当前规则：

```text
对应行业等级为 A/B/C：✅ 共振
对应行业等级为 退潮/低频：❌ 背离
无对应行业或行业未入选：— 无对应 / — 行业未入选
```

结论卡片中新增：

```text
行业+概念共振
行业+概念背离
```

说明：

- 该模块只用于解释概念主题与底层行业是否同向；
- 不改变行业或概念 A/B/C/退潮评级；
- 不输出交易信号；
- 不因背离数量多而否定已有共振。

## 4. 日报 V0.3 当前结构

日报正文结构：

```text
0. 今日结论卡片
1. 市场环境
2. 主线总览
3. 概念主题主线
4. 主线变化复核
5. 退潮与风险
6. 主线载体摘要
7. 明日复核清单
8. 疑似漏报复核摘要
```

附录结构：

```text
附录 A：完整主线评分表
  A6：概念主题完整表
附录 B：完整主线载体池
附录 C：主线生命周期迁移规则
附录 D：疑似漏报复核明细
附录 E：数据口径、局限与 TODO
```

## 5. 早期主线优先复核逻辑

已封版规则：

```python
if early_signal_type in ["企稳重估", "重新升温", "C级结构修复"] and env_score >= 45:
    priority_label = "优先复核"
else:
    priority_label = "常规复核"
```

限制：

- 不改变 A/B/C/退潮评级；
- 不覆盖原处理动作；
- 不输出交易指令；
- 只增加复核优先级。

日报中体现为：

- 今日结论卡片新增 `今日重点复核行业`；
- 主线变化复核表新增：
  - `早期信号类型`
  - `原处理`
  - `复核优先级`
- 明日复核清单优先列出早期主线复核对象。

### 5.1 四灯机会强度展示

结论卡片新增 `四灯信号`，用于直观展示当日研究机会强度。

四灯含义：

```text
灯1 环境：环境分 >= 55 为绿，否则红；
灯2 方向：存在“优先复核”行业为绿，否则红；
灯3 共振：概念主题表中至少存在 1 对 ✅ 共振为绿，否则红；共振数据缺失为灰；
灯4 时机：优先复核行业未出现“情绪顶点 / 偏离过大”为绿，否则红。
```

展示示例：

```text
🔴🟢🟢🟢 → 机会强
```

注意：

- 四灯只做展示层解释；
- 不改变环境评分、主线评级、优先复核、共振/背离判断；
- 灯3 的口径是 `count(✅ 共振) > 0`，不受背离数量影响。

## 6. 数据与缓存

数据库：

```text
data/a_stock_selector.sqlite3
```

当前缓存统计：

```text
stock_daily:
  起始日期: 2021-01-04
  结束日期: 2026-06-01
  交易日数: 1308
  行数: 6654434

stock_daily_basic:
  起始日期: 2026-06-01
  结束日期: 2026-06-01
  交易日数: 1
  行数: 5508

stock_basic:
  股票数: 5523

concept_basic:
  概念数: 486

concept_daily:
  起始日期: 2024-12-20
  结束日期: 2026-06-01
  交易日数: 347
  原始行数: 156026
  有效概念板块行数: 112236

concept_member:
  2026-06-01 概念成分股行数: 68131
```

说明：

- 日线历史缓存覆盖五年；
- 估值快照目前只有最近完整交易日；
- 概念板块历史缓存从 2024-12-20 开始；
- 概念成分股用于“概念 + 行业共振验证”；
- 概念板块宽度用涨跌家数比例 `up_num/(up_num+down_num)` 近似；
- 概念板块价格用涨跌幅反推指数，不等同于 ETF 净值；
- 每日自动化会严格补齐当日估值快照；
- 若当日 Tushare 未返回完整数据，自动化不生成日报。

## 7. 自动化任务

Codex 自动化：

```text
任务名: A股主线研究日报
任务 ID: a
频率: 周一到周五 20:30
工作目录: /Users/Zhuanz/Documents/量化选股
入口: python3 scripts/run_daily_review_job.py
```

自动化要求：

- 必须有 Tushare token；
- 必须补齐股票日线、估值、指数、概念板块、概念成分股增量数据；
- 缺数据就失败；
- 不允许回退旧缓存凑数；
- 不允许输出缺失数据日报；
- Markdown 和 HTML 每日配套生成，HTML 使用 `scripts/render_daily_review_html.py` 的既有模板；
- 历史日报按日期保留。

## 8. 重要输出路径

日报：

```text
reports/daily_review/a_share_daily_review_YYYY-MM-DD.md
reports/daily_review/a_share_daily_review_YYYY-MM-DD.html
reports/daily_review/a_share_daily_review_YYYY-MM-DD_lifecycle.md
```

日报快照：

```text
reports/daily_review/snapshots/
reports/daily_review/lifecycle_cache/
reports/daily_review/market_snapshots/
reports/daily_review/index_cache/
```

早期主线验证：

```text
reports/mainline_early_detection_validation_5y/early_mainline_validation_report.md
reports/mainline_early_detection_validation_5y/early_mainline_summary.csv
reports/mainline_early_detection_validation_5y/early_mainline_year_summary.csv
reports/mainline_early_detection_validation_5y/early_mainline_signal_type_summary.csv
reports/mainline_early_detection_validation_5y/early_mainline_samples.csv
```

2024 至今行业盲测复盘：

```text
reports/mainline_blind_replay_2024_now/blind_replay_report_2024_now.md
reports/mainline_blind_replay_2024_now/blind_replay_signals_2024_now.csv
reports/mainline_blind_replay_2024_now/theme_replay_2024_now.csv
reports/mainline_blind_replay_2024_now/major_captures_2024_now.csv
```

概念板块盲测复盘：

```text
reports/concept_blind_replay_2024_now/concept_blind_replay_report_2024_now.md
reports/concept_blind_replay_2024_now/concept_validation_samples.csv
reports/concept_blind_replay_2024_now/concept_theme_replay.csv
reports/concept_blind_replay_2024_now/concept_theme_summary.csv
reports/concept_blind_replay_2024_now/concept_major_captures.csv
```

阅读指南：

```text
daily_review_reading_guide.md
```

## 9. 其他 agent 接管流程

### 9.1 先读文档

按顺序读：

```text
README.md
xiangmu.md
daily_review_reading_guide.md
reports/mainline_early_detection_validation_5y/early_mainline_validation_report.md
reports/mainline_blind_replay_2024_now/blind_replay_report_2024_now.md
reports/concept_blind_replay_2024_now/concept_blind_replay_report_2024_now.md
```

### 9.2 跑测试

```bash
python3 -m pytest
```

预期：

```text
15 passed
```

### 9.3 生成日报

如果只验证历史日期：

```bash
python3 scripts/generate_daily_review.py --trade-date 20260601
python3 scripts/render_daily_review_html.py --trade-date 20260601
```

如果跑每日自动化逻辑：

```bash
python3 scripts/run_daily_review_job.py
```

注意：

`run_daily_review_job.py` 是严格模式；当日数据未完整发布时会失败，这是预期行为。

## 10. 当前封版结论

当前系统已经完成：

- 五年本地数据缓存；
- 东方财富概念板块接入；
- 概念成分股接入；
- 主线研究日报 V0.3；
- 行业 + 概念双维度主线展示；
- 概念 + 行业共振 / 背离验证；
- 结论卡片四灯机会强度展示；
- HTML 日报展示；
- 每日自动化；
- 严格增量数据校验；
- T-1 / T-3 / T-5 生命周期复核；
- 早期主线历史验证；
- 2024 至今行业盲测复盘；
- 2025 以来概念板块盲测复盘；
- 早期主线优先复核机制；
- 阅读指南；
- 封版交接文档。

当前最重要的日报判断不是：

```text
今天哪个板块最强？
```

而是：

```text
在当前市场环境下，哪些行业正在表现出可持续的主线生命周期？
哪些是早期主线信号？
哪些只是弱势反弹？
哪些已经退潮？
```

## 11. 下一阶段建议

建议后续只做三类增强：

1. 行业分类升级：
   从本地 `stock_basic.industry` 升级到申万/中信行业 + 概念主题。

2. 主线载体映射：
   对每条主线建立 ETF、中军、弹性龙头、风险标的映射表。

3. 基本面排雷：
   基本面只作为排雷器和解释器，不做个股加权打分主引擎。

不建议回到纯技术形态选股或个股交易回测。
