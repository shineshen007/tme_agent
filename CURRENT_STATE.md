updated_at: 2026-10-27

## current_goal
v8.3.0 源码与版本交付；真实模型与真实多组学科学验收继续独立记录。

## completed
- VERSION 与 CHANGELOG 更新为 v8.3.0；版本/发行/数据专项 78 passed，公开白名单与数据来源检查通过；源码交付范围仅限 PUBLIC_FILES.json，不包含本地知识、缓存、凭据和任务结果。
- 通用 ResearchContext、TaskResearchAdapter/LegacyResearchAdapter、EvidenceRecord v3 与差异/富集独立类型；来源行列和输入/脚本/输出哈希绑定，公开入口串联既有科研会话。
- 项目 SQLite Scientific Wiki 与明确发布的本机共享库；实体命名空间/物种、版本/来源、Know-How、候选质检、人工审查、撤回、共享发布和 JSON/Markdown 导出。
- 规划、解释、假说和实验设计的知识快照；PubMed/Europe PMC 连接器、完整可得文本、逐主张引文与直接性核验、Critic 同源复核。
- 独立 research_status/scientific_status；/wiki、/ask、/research；科研重试不重跑统计，新增分析须另建审批计划；代码/schema、查询、文献、提示、配置和逐次响应冻结。
- 完整回归 481 passed / 2 skipped / 1 warning；最后响应审计修改后相关专项 151 passed（覆盖重叠）。发行白名单 209 项审计与 provenance gate 通过，回归覆盖实际 ZIP 构建/解包/升级。
- 真实公共连接器首次查询 complete（4 条），缓存重放通过；另一轮 Europe PMC 失败如实记 partial。
- 保护数据 8/8 个文件哈希不变；已有 v8.2.0 开发改动与科研专家首版合并为 v8.3.0。

## in_progress
- 最后真实模型 Reasoner/Critic 冻结材料复核待明确外发授权；已准备具体请求预览和哈希。
- 真实 HCC 新科研入口端到端解释、真实多组学案例与人工科学审阅尚未验收。

## key_decisions
- 通用疾病框架，HCC 是首个真实输入；合成工程测试不等于科学验证。
- 自动候选经人工审查才能入库；共享发布单独确认；批准不改变主张证据等级。
- 重复队列、RNA 派生评分和丰度关联不能升级为独立组学、因果、活性或通量；无假说是合法结果。
- 缺少成熟包实现时保持依赖阻塞，不让模型自行重写统计方法。

## changed_files
- orchestrator/knowledge.py、knowledge_context.py、research_context.py、research_literature.py、research_phases.py、research_pipeline.py；新增 schema 和科研/Wiki 测试。
- 既有 agent_loop、loop_graph、evidence_graph、文献核验、llm_client 和 TUI planner/runner/controller/qa 局部接入。
- docs/RESEARCH_EXPERT.md、docs/VALIDATION.md、README、PROJECT_CONTEXT、CHANGELOG、配置示例、PUBLIC_FILES.json。

## latest_outputs
- v8.3.0：reports/research_expert_upgrade_20261026/v830_commit_tests.log/xml，78 passed；发布标签尚未创建。
- reports/research_expert_upgrade_20261026/：baseline.json、delivery_regression.log/xml、delivery_audit_fix.log/xml、protected_hashes_final.json、public_audit_final.json。
- live_literature.json、live_literature_offline_replay.json；live_model_fixture 保留两次失败尝试，live_frozen_replay/result.json 保留预算失败。
- approval_scope.json、approval_reasoner_request.json：仅 3 条合成证据与 6 条公开文献；私有 ID 检查通过，不含项目 Wiki；具体请求 SHA256 在 scope 中。

## blockers_or_warnings
- 真实模型未端到端通过：已观察并修复查询限额、主张 ID 和直接性说明；一次重放消耗 60,176 tokens 后预算阻断。最后真实复核未执行。
- 自动审批审查拒绝最后复核：读取凭据并向外部端点发送材料的具体范围未明确授权。预览端点 https://api.deepseek.com，模型 deepseek-flash，配置预算 120,000 tokens，仅 Reasoner/Critic，冻结文献、不重新检索或统计。
- 科学验收仍为 NOT_EVALUABLE；原生跨平台和真实实验可行性需要独立验收。

## next_step
获得对 approval_scope.json 所列 DeepSeek 外发范围的明确同意后，运行冻结材料的 Reasoner/Critic 复核；记录真实结果，再推进 HCC 与真实多组学人工验收。
