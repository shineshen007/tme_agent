updated_at: 2026-11-07 22:12:59 CST

## current_goal
v8.3.2 正式 Release 已发布，三平台 CI、发布工作流与下载验收全部通过。

## completed
- 修复提交 74881d51f3524384f62d8d0bb7a60c5a3399247f 已推送；v8.3.2 标签固定于该提交。
- 澄清单选/多选完整换行、草稿恢复和完整答案核对；项目内绝对路径仅在外发副本中安全转换，原始请求、隐私门禁和审批边界保留。
- 本机完整回归 846 passed / 8 skipped；发行/更新器定向回归 57 passed。
- 三平台 CI：Linux/macOS 各 833 passed / 21 skipped；Windows 827 passed / 27 skipped。发布工作流复验 833 passed / 21 skipped，构建与上传成功。
- GitHub latest 已指向正式、非预发布 v8.3.2；下载 ZIP、manifest 和 SHA256SUMS 全部通过 GitHub digest 与校验和检查。
- ZIP 的 231 项公开源码、CRC、文件权限均通过，逐项匹配标签 Git blobs；下载 ZIP 与本地独立构建字节一致。
- 发布前后 19,233 项保护文件 SHA256 一致，原始数据、配置、既有会话及未跟踪 PDF 保留。

## in_progress
- 无待完成发布事项。

## key_decisions
- 不移动或覆盖标签；发布后只补充状态文档，不改变 v8.3.2 的已验证源码与产物。
- 私有输入、配置、会话、模型日志与本地报告不进入发行。
- 不调用真实模型，不安装共享环境依赖，不运行真实代谢组分析；科学验收不因工程发布升级。

## changed_files
- VERSION、CHANGELOG.md、PROJECT_CONTEXT.md、CURRENT_STATE.md
- multiomics_tui/app.py、styles.tcss、task_planner.py、controller.py
- tests/test_privacy.py、tests/test_prompt_editor_portable.py

## latest_outputs
- Release: https://github.com/shineshen007/tme_agent/releases/tag/v8.3.2
- CI: https://github.com/shineshen007/tme_agent/actions/runs/42378629614
- 发布工作流: https://github.com/shineshen007/tme_agent/actions/runs/42379257956
- reports/release_v8.3.2_20261107/REPORT.md
- reports/release_v8.3.2_20261107/download_verified.json、protected_verification.json、completion.json
- reports/release_v8.3.2_20261107/downloaded/、01_verify_download.py
- reports/clarification_privacy_fix_20261107/REPORT.md、screens/

## blockers_or_warnings
- 便携 CI 不等于原生 Windows AppContainer 或科学有效性验收；保留各平台跳过项目及 macOS 既有 fork 警告。
- HTTP 500 服务端故障不在本次修复范围。
- 初始附件下载较慢，保留部分文件后以单请求直连续传完成；系统代理和共享环境未修改。

## next_step
另一台电脑从 Release 更新到 v8.3.2 并重启界面复测；Release ZIP 安装可运行 multiomics update，Git 检出使用 git pull。
