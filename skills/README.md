# A 股主线研究系统 Skills

本目录保存可复用研究流程文档。Skill 是流程说明，不是自动交易策略，也不是 Agent 调度系统。

## 当前 Skills

1. `daily_review_skill.md`
2. `early_mainline_validation_skill.md`
3. `retreat_validation_skill.md`
4. `concept_resonance_skill.md`
5. `catalyst_review_skill.md`
6. `third_party_review_skill.md`
7. `codex_instruction_skill.md`
8. `shadow_mainline_account_skill.md`

## 使用原则

- 所有流程服务于“A 股主线研究日报 + 主线生命周期验证系统”。
- 不输出买入、卖出、建仓、加仓、清仓等交易指令。
- 第三方建议必须先审查，再决定是否转化为 Codex 修改指令。
- Run Card 和 Shadow Observation 用于追溯与复盘，不自动改写主线评级。
- 每日生成日报时会自动输出 Run Card 并更新 Shadow Observation，不需要额外人工指令。
