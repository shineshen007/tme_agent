updated_at: 2026-10-20T16:34:00+08:00

## current_goal

v8.0.0 公开源码及终端升级交付已完成本地验收；入口权限、macOS 便携回归、CI 三平台失败三批修复已完成，**三平台 CI 全绿**，等待发布决策。

## completed

- v8 ZIP 的195项内部清单通过，源码独立导入；原研究输入、参考数据、历史结果和环境均排除。
- GPL-3.0-or-later、合成示例、本地配置、版本入口、白名单构建器、更新与回滚、CI和发布工作流完成。
- 149项完整回归、44项更新专项通过（覆盖重叠）；Linux全新ZIP安装、doctor、CSV/RDS与GSVA合成任务、后台生命周期通过。
- 入口脚本权限：4 个脚本索引模式 100644→100755，新增索引与 ZIP/manifest 权限测试。
- macOS 便携回归：非 Linux `require_probe` 记录缺失、`/var` 别名、测试缺少 R 库、DeepSeek 真实密钥依赖、TUI 计时假设，均已修复；新增 `@pytest.mark.real_probe` 安全用例。
- CI 三平台失败（run 39386993527：ubuntu 16 / macOS 15 / Windows 29）：测试会话从宿主 R 探测 `R_HOME` 与 `.libPaths()` 写入临时配置；`r_env` 用例隔离 `R_LIBS*`；`_write_new` 兼容 Windows 无 `O_NOFOLLOW`；`redact()` 仅把长度 ≥16 的环境值当密钥；探测失败附带脱敏日志末尾；CI 使用 `--basetemp` 并在失败时上传日志 artifact。
- 本机完整回归：155 passed / 1 skipped；`public_release audit` 167 项通过。
- 第二轮 CI 修复：工作流补装 `digest`；`_step_environment` 授权 Rscript 解析路径与 bin/lib 目录（setup-r 的 `/opt/R` 不在固定前缀内）；新增解释器授权测试。
- 第三轮 CI 修复：受保护子进程调用抽为 `input_inspection._run_guarded` 接缝，Windows 单元测试在父进程侧直接执行探测。
- 三平台 CI 全绿（run 39392040336，HEAD d11740b）：ubuntu、macOS、Windows 全部 success。

## in_progress

- 无源码实施任务。发布决策待定：是否以 8.0.1 打 tag 并发布 Release。

## key_decisions

- 公开源码，不含研究输入；GPL-3.0-or-later；VERSION为唯一版本来源。
- 原v8/v7使用者首次新目录安装。使用者更新Release附件，维护者使用Git；更新保护本地配置与数据。
- macOS 入口脚本权限以 Git 索引为唯一来源（100755），发行 ZIP 由 `public_release.build` 强制 0755。
- 单元测试替换 `native_dist.doctor.require_probe`，真实探测语义由 `real_probe` 专项用例与 native doctor 验收覆盖；不引入生产环境旁路开关。
- 测试环境从宿主 R 现场探测解释器与库路径，不依赖本机 bootstrap 产物；`redact()` 环境扫描以长度 ≥16 作为凭据阈值，已知/已注册密钥不受影响。
- Linux无桌面服务验收显式使用内存凭据；不声称系统凭据库或Windows/Mac原生验收通过。

## changed_files

- start_macos.command、setup_macos.command、diagnose_macos.command、scripts/00_run.sh（索引模式 100755）。
- multiomics_tui/task_runner.py（O_NOFOLLOW 兼容）、multiomics_tui/model_config.py（redact 阈值）、multiomics_tui/input_inspection.py（错误附带日志末尾）。
- tests/conftest.py、tests/test_v8_distribution.py、tests/test_public_distribution.py、tests/test_task_planner_portable.py、tests/test_prompt_editor_portable.py、tests/test_model_interfaces.py。
- .github/workflows/ci.yml、.gitignore、CHANGELOG.md、docs/VALIDATION.md、CURRENT_STATE.md。

## latest_outputs

- v8.0.0公开源码；Release构建输出tme_agent-v8.0.0.zip、manifest-v8.0.0.json、SHA256SUMS-v8.0.0.txt。
- docs/VALIDATION.md 含 2026-10-20 三节修复记录（本机便携回归、CI 三平台失败、CI 第三轮），并记录三平台全绿。
- 本机回归：155 passed / 1 skipped（跳过项为 Linux 专用 R 管线集成用例），耗时 3m22s。

## blockers_or_warnings

- 完成标记LOCAL_PUBLIC_RELEASE_VALIDATED_NATIVE_PENDING。
- Windows 原生 AppContainer、macOS 原生沙箱、真实模型 API 仍未验收；CI 便携通过不等于原生通过。
- Windows “请求中含密钥”误报的变量名未直接观测，但长度阈值修复后该组失败已在 CI 消失。
- 完整回归耗时 3m22s，主要来自规划路径真实执行 R 输入探测。

## next_step

把 CHANGELOG 的 Unreleased 改为 v8.0.1，运行 `native_dist.public_release prepare --version 8.0.1`、`build`，提交并推送 tag 触发发布；或先做 Windows/macOS 原生验收再发布。
