updated_at: 2026-11-08 16:31:32 CST

## current_goal
v8.4.0 方法证据优先规划及矩阵视图修复已提交、正式发布并完成下载验收；不包含用户真实统计分析。

## completed
- 发布标签 v8.4.0 固定到 f8c2f72b069eabdad12554e5b44dc07a0d1e5e80，正式 Release 已上线： https://github.com/shineshen007/tme_agent/releases/tag/v8.4.0 。
- 三平台 CI 42462057589 全部通过：Windows 887 passed / 28 skipped，Linux/macOS 各 893 passed / 22 skipped；macOS 保留 1 条已有 fork 警告。
- 发布工作流 42462707091 全部通过；独立完整复验 893 passed / 22 skipped，公开源码审计和内置 TCGA-LIHC 来源门禁通过。
- 下载 3 项发行附件；238 项公开源码与标签 Git blobs 逐项一致，GitHub digest、SHA256SUMS、ZIP CRC、内部 manifest/哈希和权限通过，与独立本地候选包字节一致。
- ZIP SHA256：56909022f3353e94ca2add11bdd70f727953b7da25eb4a258b36d78116960ea2。
- 方法证据主体完整回归 906 passed / 8 skipped；Windows 修复后方法证据 41 passed、发行/回滚复验 57 passed。所有初次失败及中断记录保留，不用中间记录代替最终通过证据。
- 方法文献、官方软件来源及实际 R/Python API 核验接入规划前；方法、原文、版本/环境、输入及请求与证据快照纳入审批，缺证据/依赖/API 或设计冲突时阻止审批。
- 默认 30 天证据缓存、离线复用与失败恢复已实现；GSVA 冻结协议、澄清、矩阵视图和分析后科研续接保留。旧计划可读，未执行计划须重新规划，旧校验器拒绝新协议。
- Windows 证据路径统一为 POSIX 序列化，R 探测绑定实际启动器（含 Rscript.EXE）；生产沙箱约束保持。
- 26 项原始输入、历史会话及原有 PDF SHA256 不变；私有数据、配置、密钥、环境、会话与 reports/ 未进入发行。原有未跟踪 assets/multiagent_architecture.pdf 保留。

## in_progress
- 无功能、发布或下载步骤待完成。

## key_decisions
- 模型只能提出检索词与候选；可信工程代码核验来源、版本、API 和完整性，必要证据不足不得以模型记忆或手写统计实现兜底。
- 用户已明确批准必要工程和最小测试函数及提交发布；未新增自编统计/绘图算法，未安装本地依赖或更改共享环境，未调用真实模型。
- 标签保留在已测代码提交；后续文档提交不移动标签，避免改变下载验收对象。
- scientific_status 保持 NOT_EVALUABLE；工程通过不构成科学、临床或出版验收。

## changed_files
- 规划证据模块与 R/Python 探测、规划/审批/执行/UI 接口、矩阵视图、配置和依赖清单、离线测试、公开文件清单、协议与使用说明、CI/发布工作流及状态文档。
- 具体实现见标签提交、CHANGELOG.md 及 docs/PLANNING_EVIDENCE.md；实施前已有矩阵视图修改完整保留。

## latest_outputs
- reports/release_v8.4.0_20261108/REPORT.md、download_verified.json、candidate_final_verified.json、protected_final.json、ci_final_passed.json、release_run_passed.json。
- 同目录 downloaded/、candidate_final/、01_verify_download.py、ci_final_all.log、release_all.log、distribution_recheck.log/xml；原始失败及初轮候选包完整保留。
- reports/planning_evidence_preparation_20261108_123659/REPORT.md、full_accepted.log/xml、native_api_receipts.json、guarded_utility_receipts/。

## blockers_or_warnings
- 无发布阻塞。各平台跳过项与已有警告明确保留；Windows CI 使用现有便携测试接缝，不构成原生 AppContainer 验收。
- 未完成真实模型端到端、实时方法连接器及完整 Windows/macOS 原生安装验收；发行 manifest 保留 native_pending / real_api not_tested。
- 来源原文、版本和 API 核验不证明语义或科学适用性；维护状态可能 unknown，依赖缺失时提供清单并阻塞，不自动安装。
- 本地发行测试曾遇回环 HTTP 连接失败，进程级 NO_PROXY/no_proxy 后 57 项全部通过，未修改系统代理。

## next_step
本次发布收尾已完成；后续真实模型、连接器或原生安装验收以独立任务记录，不改变本次已发布标签。
