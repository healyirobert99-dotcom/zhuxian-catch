---
name: daily-review
description: |
  日报快捷命令 (/日报)。同步最新交易日数据、生成主线研究日报、然后按照解读协议进行复盘解读。
  触发词：/日报、日报、生成日报、复盘解读。
agent_created: true
---

# /日报 — A股主线研究日报生成与复盘解读

## 触发

当用户输入 `/日报`、`日报`、`生成日报`、`复盘解读` 或要求生成/解读当日主线报告时，执行以下流程。

## 工作流程

### 第一步：确定最新交易日

```bash
python -c "
import sqlite3
con = sqlite3.connect('data/a_stock_selector.sqlite3')
cur = con.execute('SELECT MAX(date) FROM stock_daily')
latest = cur.fetchone()[0]
print(latest)
con.close()
"
```

如果最新日期不是当天（今天有交易但数据未入库），先尝试从 Tushare 拉取。

### 第二步：同步数据并生成日报

```bash
cd /path/to/zhuxian-catch
python scripts/run_daily_review_manual.py --trade-date <最新交易日>
```

如果管线报错（通常是因为 Tushare 缓存检查失败），改用直接生成：

```python
import sys
sys.path.insert(0, 'scripts')
import generate_daily_review as dr
import os
token = os.environ.get('TUSHARE_TOKEN')
if not token:
    raise SystemExit('TUSHARE_TOKEN is required; set it in .env or the environment.')
pro = dr.build_tushare_client(token)
paths = dr.generate_daily_report('<trade_date>', token, pro, use_lifecycle_cache=True)
```

不要在流程文档、日报或对话中明文写入 Tushare token；统一从 `.env` 或环境变量读取。

### 第三步：读取日报

读取生成的 Markdown 日报文件：
`reports/daily_review/a_share_daily_review_YYYY-MM-DD.md`

### 第四步：复盘解读

按照 `daily_review_interpretation_protocol.md` 的解读顺序：

```text
市场环境 → 四灯信号 → 主线阶段 → 早期/确认信号 → 催化语义分析 → ETF/中军载体 → 操作框架 → 风险点
```

解读时必须：
- 输出 3-5 条要点，给具体仓位/止损/加仓条件
- 有 ETF 时附 ETF 代码，无 ETF 时查中军/弹性标的
- 没有板块效应的个股行情直接提醒不适合参与
- 检查 `data/positions.md` 中是否有持仓，如有则逐条检查触发信号
- 输出格式：先给简洁结论，再给具体框架

### 第五步：输出

将解读内容直接回复用户。如果当天环境分 ≤29，第一条必须是"不动"。

## 关键路径

| 资源 | 路径 |
|------|------|
| 日报脚本 | `scripts/run_daily_review_manual.py` |
| 日报 Markdown | `reports/daily_review/a_share_daily_review_YYYY-MM-DD.md` |
| 解读协议 | `daily_review_interpretation_protocol.md` |
| 策略范式 | `dp-xiangmu.md` 第 10-11 节 |
| 持仓记录 | `data/positions.md` |
| 数据库 | `data/a_stock_selector.sqlite3` |
