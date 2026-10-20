# v8.0.0 本地验收

完成标记：`LOCAL_PUBLIC_RELEASE_VALIDATED_NATIVE_PENDING`。尚未推送 GitHub，尚未发布远程 Release。

- 原 v8 ZIP 的 195 项内部清单校验通过；原包 SHA256 见 V8_SOURCE.json。
- 完整便携回归：149 passed，0 failed，0 skipped。配置保护修复后的更新专项：44 passed；两者覆盖重叠，不累计。
- 在全新目录从公开 ZIP 实际安装：Python 3.13.5、R 4.4.2，离线复用已有依赖。
- Linux doctor 通过：批准读、拒绝改输入、拒绝越界读写、拒绝网络和内存读取系统调用；原输入哈希不变。
- 合成 CSV/RDS 双输入任务通过：3 个输出、2 行结果，RDS 契约及输入哈希检查通过。
- GSVA 合成评分：2 个基因集 × 4 个样本；后台任务、真实退出码、并发拒绝、停止子进程和锁释放通过。
- 本地 HTTP 模拟 v8.0.1：真实升级、bootstrap、doctor 和 CLI 回滚通过；8 项本地配置、数据、结果及环境标记哈希不变。
- 错误下载、校验失败、危险 ZIP、源码修改、任务锁、活动任务、中断、失败恢复和回滚冲突均有测试。

Linux 无桌面凭据服务的验收目录显式使用 `credential_storage: memory`。未使用真实模型 API 密钥；未声称系统凭据库已通过本次验收。

Windows、Intel Mac、Apple Silicon 的原生隔离、R 和系统凭据库仍待目标机器验证。CI 配置提供三平台便携测试；首次远程运行（e4c587e）未通过，见下节。本地通过不等于这些平台原生通过。

## 2026-10-20 macOS 便携回归修复

- 修复前：macOS 本机 `python -m pytest -q` 为 134 passed / 16 failed / 1 skipped。16 项失败在干净 HEAD 工作副本中复现，与入口脚本权限修复无关。
- 根因一：非 Linux 平台在规划前调用 `native_dist.doctor.require_probe`，测试临时项目根没有 `.runtime/doctor.json`，规划在断言前即中止（15 项）。
- 根因二：`test_deepseek_legacy_parameter` 未存合成密钥，只能读取环境中真实的 `DEEPSEEK_API_KEY`（1 项）。
- 修复过程中发现并处理：pytest 临时目录的 `/var`→`/private/var` 别名被 Seatbelt 拒绝；测试 `cfg` 未带本机 R 库导致输入探测缺少 `jsonlite`；两个 TUI 用例按 `pause()` 次数等待后台线程。
- 修复后：151 passed / 0 failed / 1 skipped（跳过项为 Linux 专用 R 管线集成用例），耗时 3m06s。修复前耗时 47s，增量来自规划路径现在真实执行 R 输入探测。
- 新增 `@pytest.mark.real_probe` 用例：缺记录与指纹不符时 `require_probe` 仍拒绝，合法记录才放行。
- GitHub Actions 首次运行（e4c587e）结论 failure：ubuntu-latest 在 pytest 步骤失败，macOS/Windows 因 fail-fast 被取消；工作流已改为 `fail-fast: false`，三平台结果待重跑。

## 2026-10-20 CI 三平台失败修复

- 第二次远程运行（5653314，run 39386993527）：ubuntu 16 项、macOS 15 项、Windows 29 项 pytest 失败。
- 输入探测失败（三平台）：测试的 `runtime.r_library_paths` 在 CI 为空，且 `_step_environment` 把 `HOME` 指向任务目录，R 读不到 `~/.Renviron` 中的用户库，探测脚本报 `there is no package called 'jsonlite'`。测试会话现从宿主 R 探测 `R.home()` 与 `.libPaths()` 写入临时项目配置；本机以空库复现了同一报错。
- `r_env` 两个用例：CI 的 setup-r 通过 `R_LIBS*` 注入用户库，断言不再封闭；用例内清除这些变量。
- Windows 产品缺陷：`task_runner._write_new` 使用 `os.O_NOFOLLOW`（Windows 无此常量）导致代码生成抛 `AttributeError`；改为 `getattr(os, "O_NOFOLLOW", 0)`，`O_EXCL` 保持不覆盖语义，并新增跨平台回归测试。
- Windows “请求中含密钥”误报（13 项）：`redact()` 曾把名称含 KEY/TOKEN/PASSWORD/SECRET 的所有环境变量值当密钥，运行器注入的短值与提示词文本相撞。现要求环境值长度 ≥16 才参与替换；已注册密钥与长密钥行为不变，新增两条回归测试。
- 本机修复后完整回归：154 passed / 1 skipped（新增 3 项测试），`public_release audit` 167 项通过。
- 可观测性：探测失败信息附带脱敏限长的 `probe.log` 末尾；CI 使用 `--basetemp=.pytest-tmp`，失败时上传 probe/step 日志 artifact。
- 三平台结果待下一次远程运行确认。

## 2026-10-20 CI 第二轮修复

- 第二次远程运行（89d9d2f，run 39389423229）仍失败：macOS 13 项、ubuntu 14 项、Windows 13 项，全部收敛到输入探测与执行环境；Windows 的密钥误报与 `O_NOFOLLOW` 已消失，`r_env` 两项已修复。
- macOS：`input_probe.R` 同时依赖 `jsonlite` 与 `digest`，CI 只装了前者 → 工作流补齐 `digest`。
- ubuntu：`PermissionError: [Errno 13] Permission denied: '/usr/local/bin/Rscript'`。setup-r 把 R 装在 `/opt/R`，不在 `task_guard` 的固定系统前缀内；`_step_environment` 现显式授权解析后的 Rscript、其 `bin/` 与 `lib/` 目录（Landlock 需要执行权限）。
- Windows：`Private native runtime or approved input snapshot is missing`。AppContainer 依赖 bootstrap 准备的私有运行时；单元测试改为替换 `windows_native.run_appcontainer`，原生隔离仍由 bootstrap/原生验收覆盖。
- 本机完整回归：155 passed / 1 skipped；`public_release audit` 167 项通过。
- 三平台结果待下一次远程运行确认。

## 2026-10-20 CI 第三轮修复

- 第三次远程运行（31b30ab，run 39390928389）：ubuntu 与 macOS **success**；Windows 仍有 13 项，全部与输入探测相关。
- Windows 根因：探测命令在独立子进程里运行 `task_guard.py`，进程内 monkeypatch 影响不到该子进程，仍报 `Private native runtime or approved input snapshot is missing`。
- 修复：受保护子进程调用抽为 `input_inspection._run_guarded` 接缝，Windows 单元测试在父进程侧直接运行探测命令；产品路径不变，AppContainer 语义仍由 bootstrap 与原生验收覆盖。
- 本机回归：探测相关三个测试文件 54 passed；`public_release audit` 167 项通过。
- 第四次远程运行（d11740b，run 39392040336）：ubuntu、macOS、Windows **全部 success**，便携回归首次三平台通过。

## 2026-10-20 v8.0.1 发布

- 版本提交 24a0d67 的 CI（run 39395711720）三平台 success；tag v8.0.1 触发发布工作流（run 39396418309），validate 与 publish 均 success。
- Release 附件：`tme_agent-v8.0.1.zip`（sha256 2fcf8e40…，431337 B）、`manifest-v8.0.1.json`、`SHA256SUMS-v8.0.1.txt`；`shasum -a 256 -c` 通过。
- 发布清单与 tag 内容逐文件一致（167 项、`source_data` 为空）；ZIP 169 条目，无 `data/`、`.runtime/`、`.venv/`、`analysis/`、`workspace/`；四个 `.command`/`.sh` 为 0755。
- 公开更新器 `ReleaseClient` 识别到最新正式版本 8.0.1 与三个附件；解包后 `installed()` 报告 8.0.1、无 `data/raw/`。
- 本机完整回归：155 passed / 1 skipped。

## 当前状态

- 三平台 CI 全绿；Windows 原生 AppContainer 隔离、macOS 原生沙箱与真实模型 API 仍未验收，`docs/VALIDATION.md` 不据此声称原生通过。
- 本机完整回归 155 passed / 1 skipped；`public_release audit` 167 项通过。


维护者发布前应复核暂存白名单与 Release 内容；用户只安装 Release 附件 ZIP。研究输入、结果、个人配置和未确认再分发许可的参考资源均不随公开包交付。
