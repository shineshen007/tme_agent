updated_at: 2026-11-06 18:52:52 CST

## current_goal
v8.3.1 正式 Release 已发布，下载验收已完成。

## completed
- 已提交并推送累计规划可靠性与跨平台修复；Release 标签指向 392858794c33c54304477eb1d6fa16fe325a75d5。
- 本机完整回归 815 passed / 8 skipped；后续 Windows 路径修复定向回归 172 passed。
- 三平台完整 CI 通过：Linux/macOS 各 810 passed / 21 skipped，Windows 804 passed / 27 skipped。
- 发布工作流验证与上传均成功；v8.3.1 为正式、非预发布的最新 Release。
- 231 项公开清单与来源门禁通过；下载 ZIP、manifest、SHA256SUMS 校验通过，包内源码逐项匹配标签提交，ZIP 与本地独立构建字节一致。
- 19,233 个保护文件哈希未变，未跟踪 PDF 保留。

## in_progress
- 无待完成发布事项。

## key_decisions
- 标签固定到三平台 CI 通过的提交，不覆盖标签；发布后仅补充状态文档。
- 私有输入、配置、会话、模型日志和本地报告不进入发行。
- 不追加真实模型调用、不执行分析；缺少配对映射时仍保持 blocked。

## changed_files
- 规划、输入检查、传输恢复、澄清界面及对应测试。
- R 库发现测试、跨平台字节/路径处理、发行白名单与公开文档。

## latest_outputs
- Release: https://github.com/shineshen007/tme_agent/releases/tag/v8.3.1
- CI: https://github.com/shineshen007/tme_agent/actions/runs/42169936174
- 发布工作流: https://github.com/shineshen007/tme_agent/actions/runs/42170750233

## blockers_or_warnings
- 原生 Windows AppContainer 验收与科学有效性不由便携 CI 证明；科研验收状态不因发布升级。
- macOS 测试保留一条既有 fork 弃用警告。

## next_step
用户可从 Release 下载带 manifest 的发行 ZIP 安装或更新；真实分析仍需提供可追溯配对映射并另行批准。
