# Shadow Mainline Observation Skill

## 目标

用影子观察账本追踪主线优先复核信号的后续表现，不使用真实资金。

## 输入

- 每日 Run Card
- 优先复核行业
- 概念行业共振列表
- 本地行业 beta 数据

## 运行方式

Shadow Observation 是日报生成的旁路任务。平时生成日报时会自动更新，不需要单独下指令；独立脚本只用于补跑历史或调试。

## 触发信号

- `early_core_env45`
- `concept_industry_resonance`
- 后续可扩展 `price_catalyst_resonance`

## 输出

- `reports/shadow_mainline_account/shadow_mainline_account.csv`
- 活跃影子观察
- 完成观察
- 5/10/20/40/60 日收益和 20/40 日最大回撤

## 不做

- 不自动交易
- 不输出买卖建议
- 不默认使用弹性个股作为载体
