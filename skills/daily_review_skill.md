# 每日主线日报生成 Skill

## 目标

生成 A 股主线研究日报 Markdown、HTML、Run Card 和主线快照。

## 输入

- 本地股票日线与估值缓存
- 本地行业分类
- 本地概念日线和概念成分
- 历史主线快照
- 催化标题与关键词配置

## 步骤

1. 校验当日缓存完整度。
2. 计算市场环境分。
3. 识别行业主线生命周期。
4. 识别概念与行业共振。
5. 执行催化复核摘要。
6. 生成主线变化复核、退潮风险、载体摘要和明日复核清单。
7. 输出 Markdown、HTML、主线快照、市场快照和 Run Card。
8. 自动更新 Shadow Mainline Observation；这是日报旁路产物，不需要额外人工指令。

## 输出

- `reports/daily_review/a_share_daily_review_YYYY-MM-DD.md`
- `reports/daily_review/a_share_daily_review_YYYY-MM-DD.html`
- `reports/daily_review/run_cards/run_card_YYYY-MM-DD.json`
- `reports/daily_review/snapshots/mainline_snapshot_YYYY-MM-DD.json`
- `reports/shadow_mainline_account/shadow_mainline_account.csv`

## 不做

- 不自动交易
- 不输出个股买卖建议
- 不让催化文本单独改变主线评级
