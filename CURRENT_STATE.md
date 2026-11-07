updated_at: 2026-11-07 21:46:55 CST

## current_goal
发布 v8.3.2：澄清选项完整显示与隐私路径修复已完成，正在提交并验证三平台 CI。

## completed
- 单选/多选统一使用可换行 OptionList，完整显示标题和理由；支持鼠标、方向键、空格/Enter、纵向滚动、缩放、草稿恢复和过期事件防护。
- 核对页显示将提交的完整答案，原始问题、答案、哈希和审批流程保持有效。
- 项目内已存在且安全的绝对输入路径只在外发副本中转换；覆盖意图分类、旧目标上下文、规划、澄清和 /retry。项目外、缺失、越界及链接路径本地阻断，继续执行原有隐私检查。
- 本机全量回归 846 passed / 8 skipped / 0 failed；公开清单审计 231 项通过；保存 12 份 SVG 界面样例和核心源码哈希。
- 19,233 保护文件与发布基线比对：19,232 一致，无缺失；仅早于本轮修改的 Finder .DS_Store 不同。原始数据、配置、会话和未跟踪 PDF 一致。

## in_progress
- 提交并推送 v8.3.2 更新，等待三平台 CI；通过后打新标签、发布并下载验收。

## key_decisions
- 每项完整展开并自动换行；协议长度限制及原文验证不变。
- 仅核实当前项目内路径，不猜测旧电脑根目录映射；不降低患者标识、联系方式和凭据门禁。
- 本轮不调用真实模型、不安装依赖、不运行真实代谢组分析。VERSION 已准备为 8.3.2，正式发布须通过三平台 CI。

## changed_files
- multiomics_tui/app.py、styles.tcss、task_planner.py、controller.py
- tests/test_privacy.py、tests/test_prompt_editor_portable.py
- CURRENT_STATE.md、PROJECT_CONTEXT.md、CHANGELOG.md

## latest_outputs
- reports/release_v8.3.2_20261107/
- reports/clarification_privacy_fix_20261107/REPORT.md
- reports/clarification_privacy_fix_20261107/pytest_full.log
- reports/clarification_privacy_fix_20261107/validation.json
- reports/clarification_privacy_fix_20261107/artifact_manifest.json
- reports/clarification_privacy_fix_20261107/protected_verification.json
- reports/clarification_privacy_fix_20261107/screens/、fix.patch

## blockers_or_warnings
- 三平台 CI 正在准备；本机通过不代表 Windows/Linux 原生验收。
- 全量回归保留 8 项跳过和 1 条既有 macOS fork 弃用警告；工程通过不代表科学验收。
- HTTP 500 服务端故障不在本次修复范围。
- 失败记录已保留：受限沙箱不支持嵌套 sandbox-exec；根目录自动收集误扫历史测试副本。最终原生 tests/ 全量回归已通过。

## next_step
推送本次公开白名单内改动，等待三平台 CI 全部通过后创建 v8.3.2 标签并验收下载产物。
