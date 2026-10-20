updated_at: 2026-10-20T14:56:01+08:00

## current_goal

v8.0.0 公开源码及终端升级交付已完成本地验收；入口脚本权限与 macOS 便携回归两项修复已实现并通过本机完整回归，等待提交、推送并重跑三平台 CI。

## completed

- v8 ZIP 的195项内部清单通过，源码独立导入；原研究输入、参考数据、历史结果和环境均排除。
- GPL-3.0-or-later、合成示例、通用本地配置、版本入口、白名单构建器、更新与回滚、CI和发布工作流完成。
- 149项完整回归、44项更新专项通过（覆盖重叠）；Linux全新ZIP安装、doctor、CSV/RDS与GSVA合成任务、后台生命周期通过。
- 实际本地HTTP模拟升级、真实bootstrap/doctor及CLI回滚通过，8项本地配置、数据和环境标记哈希不变。
- 入口脚本权限修复：4 个脚本索引模式 100644→100755，工作区已 `chmod +x`；新增索引与 ZIP/manifest 权限回归测试。
- macOS 便携回归修复：`python -m pytest -q` 由 134 passed/16 failed/1 skipped 变为 **151 passed/0 failed/1 skipped**（3m06s）。修复项：非 Linux `require_probe` 记录缺失、临时根 `/var` 别名被 Seatbelt 拒绝、测试 cfg 未带本机 R 库（jsonlite）、DeepSeek 用例依赖真实环境密钥、TUI 用例按 `pause()` 次数计时。
- 新增 `@pytest.mark.real_probe` 用例：缺记录与指纹不符时 `require_probe` 仍拒绝。`public_release audit` 167 项通过。

## in_progress

- 变更尚未提交：4 个脚本模式变更已暂存，测试/CI/文档改动未暂存。CI 三平台结果待推送后重跑。

## key_decisions

- 公开源码，不含研究输入；GPL-3.0-or-later；VERSION为唯一版本来源。
- 原v8/v7使用者首次新目录安装。使用者更新Release附件，维护者使用Git；更新保护本地配置与数据。
- macOS 入口脚本权限以 Git 索引为唯一来源（100755），发行 ZIP 由 `public_release.build` 强制 0755，回归测试锁定。
- 单元测试替换 `native_dist.doctor.require_probe`（不执行生成代码），真实探测语义由 `real_probe` 专项用例与 native doctor 验收覆盖；不引入生产环境旁路开关。
- 测试项目根统一 `resolve()`，测试 cfg 复用 `config/runtime.local.yaml` 的 R 库（CI/发行环境无此文件时退回系统库）。
- Linux无桌面服务验收显式使用内存凭据；不声称系统凭据库或Windows/Mac原生验收通过。

## changed_files

- start_macos.command、setup_macos.command、diagnose_macos.command、scripts/00_run.sh（索引模式 100755）。
- tests/conftest.py（探测替换、real_probe 标记、路径解析、R 库发现）、tests/test_v8_distribution.py、tests/test_public_distribution.py、tests/test_task_planner_portable.py、tests/test_prompt_editor_portable.py、tests/test_model_interfaces.py。
- .github/workflows/ci.yml（fail-fast: false）、CHANGELOG.md、docs/VALIDATION.md、CURRENT_STATE.md。

## latest_outputs

- v8.0.0公开源码；Release构建输出tme_agent-v8.0.0.zip、manifest-v8.0.0.json、SHA256SUMS-v8.0.0.txt。
- docs/VALIDATION.md（新增 2026-10-20 macOS 便携回归修复记录）、docs/V8_SOURCE.json、安装/更新/发布说明。
- 本机回归：151 passed / 1 skipped（跳过项为 Linux 专用 R 管线集成用例）。

## blockers_or_warnings

- 完成标记LOCAL_PUBLIC_RELEASE_VALIDATED_NATIVE_PENDING。
- GitHub Actions 首次运行（e4c587e）为 failure：ubuntu pytest 失败、macOS/Windows 因 fail-fast 取消；失败详情需授权查看日志，修复后尚未重跑。
- Windows 平台从未实际运行便携回归，可能出现额外失败。
- 完整回归耗时由 47s 增至 3m06s，增量来自规划路径真实执行 R 输入探测。
- 尚未提交/推送远程；macOS/Windows 原生沙箱与真实模型API未测试。

## next_step

审核暂存与未暂存变更后提交并推送 main，触发三平台 CI；按结果修复剩余平台差异，再决定是否以 8.0.1 发布。
