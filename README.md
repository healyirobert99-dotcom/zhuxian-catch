# A 股主线识别与研究闭环系统

本项目当前封版定位：

**主线识别与交易复核闭环系统。核心是识别市场环境、行业/概念主线生命周期，并为板块 beta 策略提供 ETF / 中军载体与可执行复核框架。**

系统目标是经过连续复盘，识别市场环境较好时具有延续性的中高级别主线板块，并把行业主线分为：

- A/B/C 主线或观察方向；
- 企稳重估、重新升温、C级结构修复等早期主线信号；
- 确认后退潮、退潮风险、低频监控等风险方向；
- ETF/行业指数、中军龙头、弹性龙头、风险复核标的等主线载体层。

历史 A+、VCP、双大师、低波突破等回测仍保留在项目中作为研究档案，但不再是当前主系统。

## 当前核心文件

- `scripts/generate_daily_review.py`：生成 A 股主线研究日报 Markdown。
- `scripts/render_daily_review_html.py`：把 Markdown 日报渲染为同名 HTML，并刷新日报归档页。
- `scripts/run_daily_review_manual.py`：手动严格生成入口，补齐增量数据后生成 Markdown + HTML + 归档入口。
- `scripts/sync_catalysts.py`：自动同步近几日新闻/政策/公告标题，写入催化复核输入文件。
- `scripts/validate_mainline_early_detection.py`：五年早期主线识别历史验证。
- `config/catalyst_keywords.csv`：催化复核关键词配置。
- `data/catalysts/catalyst_titles.csv`：本地新闻/政策/研报/公告标题输入模板。
- `daily_review_reading_guide.md`：每日日报阅读指南。
- `xiangmu.md`：项目封版交接文档。

## 安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## 数据源与本地缓存

默认 SQLite 缓存：

```text
data/a_stock_selector.sqlite3
```

当前本地日线缓存：

```text
stock_daily: 2021-01-04 到 2026-06-01
交易日数: 1308
日线行数: 6654434
```

手动严格生成要求项目根目录 `.env` 或环境变量中存在：

```bash
TUSHARE_TOKEN=...
```

系统不允许使用缺失数据生成日报。若当日 `stock_daily`、`stock_daily_basic` 或主要指数数据不完整，任务会失败并报告原因。

## 生成日报

生成指定交易日 Markdown 日报：

```bash
python3 scripts/generate_daily_review.py --trade-date 20260601
```

严格补齐增量数据并生成最新每日复盘 Markdown + HTML：

```bash
python3 scripts/run_daily_review_manual.py
```

单独把 Markdown 渲染为 HTML：

```bash
python3 scripts/render_daily_review_html.py --trade-date 20260601
```

渲染后会同步刷新日报归档入口：

```text
reports/daily_review/index.html
```

每份 HTML 日报顶部都有“历史日报”下拉框，可直接切换到已生成的历史报告。

## 催化复核输入

日报支持轻量催化复核，用于帮助后续解读交易信号。它不改变主线评级，只解释“价格、宽度、生命周期”背后的政策、产业、资金或研报线索。

手动严格生成日报时会先尝试自动同步最近 5 天标题：

```bash
python3 scripts/sync_catalysts.py --days 5
```

同步失败不会阻断行情日报生成，但会在终端输出失败来源，日报继续使用本地已有催化缓存。

把标题追加到：

```text
data/catalysts/catalyst_titles.csv
```

字段：

```text
date,source_type,source_name,title,summary,related_industry,related_concept
```

`summary` 是可选摘要字段。有摘要时系统会同时扫描标题和摘要；没有摘要时仍按标题正常运行。

关键词配置：

```text
config/catalyst_keywords.csv
```

没有催化标题时，日报仍正常生成，并提示“暂无有效文本数据”。

生成最近 10 个缓存交易日的日报，用于 T-1 / T-3 / T-5 生命周期复核：

```bash
python3 scripts/generate_daily_review.py --recent-days 10 --end-date 20260601
```

日报输出路径：

```text
reports/daily_review/a_share_daily_review_YYYY-MM-DD.md
reports/daily_review/a_share_daily_review_YYYY-MM-DD.html
reports/daily_review/index.html
```

历史日报按日期保留，不覆盖其他日期。

## 手动严格生成规则

- 必须读取 Tushare token；
- 必须补齐当日增量数据；
- 缺数据不生成日报；
- Markdown 和 HTML 都按日期保存；
- 自动刷新 `reports/daily_review/index.html`；
- 不删除历史日报。

## 早期主线验证

运行五年早期主线识别验证：

```bash
python3 scripts/validate_mainline_early_detection.py --start 2021-01-04 --end 2026-06-01
```

输出目录：

```text
reports/mainline_early_detection_validation_5y/
```

核心验证结论：

- 泛早期主线 `early_mainline`：40 日跑赢胜率 63.85%，40 日平均超额 2.29%。
- 收窄后的 `early_core_env45`：40 日跑赢胜率 67.59%，40 日平均超额 2.77%。
- 最值得日报优先复核的早期信号：
  - 企稳重估；
  - 重新升温；
  - C级结构修复；
  - 且市场环境分 >= 45。

## 阅读指南

先读：

```text
daily_review_reading_guide.md
```

每日阅读顺序：

1. 市场环境；
2. 主线总览；
3. 主线变化复核；
4. 退潮与风险；
5. 主线载体摘要；
6. 明日复核清单。

关键原则：

**不要问今天哪个板块最强，而要问在当前市场环境下，哪些行业正在表现出可持续的主线生命周期。**

## 测试

```bash
python3 -m pytest
```

当前验证状态：

```text
15 passed
```

## 历史研究档案

以下目录保留为历史研究材料：

- `reports/low_vol_contraction_validation_5y/`
- `reports/low_vol_two_stage_execution_5y/`
- `reports/event_study_2y_full_cache/`
- `reports/rule_mining_5y/`
- `reports/trend_direction_probe_5y/`
- `reports/*a_plus*`

这些结果不再代表当前主系统方向。
