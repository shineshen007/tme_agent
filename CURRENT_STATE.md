updated_at: 2026-10-16T23:52:28+08:00

## current_goal

v8.0.0 公开源码及终端升级交付已完成本地验收，等待维护者上传和发布。

## completed

- v8 ZIP 的195项内部清单通过，源码独立导入；原研究输入、参考数据、历史结果和环境均排除。
- GPL-3.0-or-later、合成示例、通用本地配置、版本入口、白名单构建器、更新与回滚、CI和发布工作流完成。
- 149项完整回归、44项更新专项通过（覆盖重叠）；Linux全新ZIP安装、doctor、CSV/RDS与GSVA合成任务、后台生命周期通过。
- 实际本地HTTP模拟升级、真实bootstrap/doctor及CLI回滚通过，8项本地配置、数据和环境标记哈希不变。

## in_progress

- 无源码实施任务。远程上传/Release发布待维护者执行。

## key_decisions

- 公开源码，不含研究输入；GPL-3.0-or-later；VERSION为唯一版本来源。
- 原v8/v7使用者首次新目录安装。使用者更新Release附件，维护者使用Git；更新保护本地配置与数据。
- Linux无桌面服务验收显式使用内存凭据；不声称系统凭据库或Windows/Mac原生验收通过。

## changed_files

- 公开配置、CLI、bootstrap配置保护、发行与更新模块、便携测试及文档；完整交付白名单见PUBLIC_FILES.json。

## latest_outputs

- v8.0.0公开源码；Release构建输出tme_agent-v8.0.0.zip、manifest-v8.0.0.json、SHA256SUMS-v8.0.0.txt。
- docs/VALIDATION.md、docs/V8_SOURCE.json和安装/更新/发布说明。

## blockers_or_warnings

- 完成标记LOCAL_PUBLIC_RELEASE_VALIDATED_NATIVE_PENDING。
- 尚未提交/推送远程，尚无本轮Release或Actions运行；Windows/macOS原生及真实模型API未测试。

## next_step

按README审核暂存文件，提交并推送main，再推送v8.0.0标签以触发Release发布。
