updated_at: 2026-11-09 09:13:07 CST

## current_goal
提交并发布 v8.4.1；最终完整本地回归及提交前审计通过，正在准备提交、推送和三平台 CI。已发布 v8.4.0 保持不变。

## completed
- 最终源码单次完整回归 936 passed / 8 skipped / 0 failed / 0 errors，保留 1 条已有 fork 警告；944 个用例记录完整保存在 reports/release_v8.4.1_20261109/full_final.log/xml。
- 238 项已测公开源码未变化，26 项当前保护文件快照未变化；公开源码审计、内置数据来源门禁通过。
- 本机执行 guard/runtime 指纹与通过的诊断记录匹配。初轮候选包 238 项文件、ZIP CRC、内外 SHA256、权限和源码字节检查通过。
- 固定界面及模型设置统一英文；中文自然语言任务的回复和各阶段澄清使用中文，排除路径、代码、引文和文件名影响，重试/恢复保留任务语言。
- 澄清提交改用 Textual 后台 worker，保留同步 busy 预留、问题哈希及重复提交保护；共享模型入口在隐私、缓存和审计前明确 JSON 输出要求。
- 原始要求、完整选项与答案、输入/审批哈希及科研边界保留；历史失败和复验保存在 reports/ui_language_fix_20261108/ 和 reports/json_prompt_fix_20261108/。

## in_progress
- 显式暂存并提交本次修复及 VERSION=8.4.1，推送 main；须等待 Windows/Linux/macOS CI 全通过后创建 v8.4.1 标签。
- 随后等待发布工作流，下载全部附件并与精确标签源码及独立候选包核对。

## key_decisions
- 固定 UI 英文；任务语言独立持久化；方法检索 reply_language 默认 en，由规划入口明确传入。
- 不新增统计/绘图算法，不安装本地依赖，不更改共享环境；真实模型调用不属于离线发布验证。
- 不移动 v8.4.0 标签或覆盖附件；未跟踪 PDF、私有数据、配置、环境、会话和本地报告不进入提交或发行。
- 三平台 CI、发布工作流与下载附件验证全部完成后才宣告发布验收；scientific_status 维持 NOT_EVALUABLE。

## changed_files
- 本次已审查 UI/语言、连接/凭据、规划接口、原生入口提示、共享 JSON 请求、相关测试与版本/说明文档；显式暂存清单存于本轮报告目录。
- assets/multiagent_architecture.pdf 为既有未跟踪文件，保留但不纳入。

## latest_outputs
- reports/release_v8.4.1_20261109/local_verified.json、full_final.log/xml、protected_baseline.json、protected_after_local.json。
- 同目录 public_audit.json、provenance.json、native_doctor_verified.json、candidate_initial_verified.json、candidate_initial/、baseline.json、worktree_before.patch。
- 上轮界面快照与修复记录：reports/ui_language_fix_20261108/REPORT.md。

## blockers_or_warnings
- 发布门禁仍在进行：三平台 CI、发布工作流和下载附件尚未验收。
- 离线/便携工程通过不等于真实 API、Windows 原生 AppContainer 或科学/临床验收。
- 当前快照按本次文件实际字节建立，不沿用旧任务已不同的 sample.info.csv 哈希；不覆盖原始数据。

## next_step
提交并推送已审查的修复与 v8.4.1 版本文件，等待三平台 CI；通过后标记同一提交发布并核验下载附件，最后回写正式状态。
