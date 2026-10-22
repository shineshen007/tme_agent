# v8.0.0 本地验收

完成标记：`LOCAL_PUBLIC_RELEASE_VALIDATED_NATIVE_PENDING`。当前发布状态见 `CURRENT_STATE.md`；本文件保留历次本地验收记录。

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

## 2026-10-20 v8.0.2 发布

- 版本提交 e92448e 的 CI（run 39401541939）三平台 success；tag v8.0.2 触发发布工作流（run 39401561243），validate 与 publish 均 success。
- Release 附件：`tme_agent-v8.0.2.zip`（441854 B）、`manifest-v8.0.2.json`（31250 B）、`SHA256SUMS-v8.0.2.txt`；`shasum -a 256 -c` 通过。
- 发布清单与 tag 内容逐文件一致（171 项、`source_data` 为空）；ZIP 173 条目，含 3 个 `assets/*.svg` 与 `README.zh-CN.md`，无 `data/`、`.runtime/`、`.venv/`、`analysis/`、`workspace/`；四个 `.command`/`.sh` 为 0755。
- `public_update.ReleaseClient` 识别到最新正式版本 8.0.2。

## 2026-10-20 v8.0.2 品牌与 README

- 新增 `assets/`：`logo-on-dark.svg`、`logo-on-light.svg`（GitHub 深浅主题经 `<picture>` 自适应）、`logo-lockup.svg`（社交预览）；全部为手绘单色矢量，未生成 PNG（符合项目图件规范）。
- README 改为 Paseo 风格版式：居中 logo、标题、语言行、徽章行、一句话简介、特性列表、Mermaid hub–spoke 架构图、Agents 表、快速开始、更新/回滚、维护者发布、许可。英文主版为 `README.md`，简体中文对照为 `README.zh-CN.md`。
- 发行白名单扩展：`native_dist/public_release.py` 的 `AREAS` 增加 `assets: {.svg}`，`TOP` 增加 `README.zh-CN.md`；`PUBLIC_FILES.json` 相应新增 4 个路径。
- `public_release prepare --version 8.0.2` 通过；`audit` 171 项通过；`build` 产出 `tme_agent-v8.0.2.zip`（173 条目、含 3 个 SVG 与 `README.zh-CN.md`、`source_data` 为空）。
- 本机回归：发行分布与 v8 分布两个测试文件 57 passed；完整回归 155 passed / 1 skipped。

## 2026-10-20 v8.0.1 发布

- 版本提交 24a0d67 的 CI（run 39395711720）三平台 success；tag v8.0.1 触发发布工作流（run 39396418309），validate 与 publish 均 success。
- Release 附件：`tme_agent-v8.0.1.zip`（sha256 2fcf8e40…，431337 B）、`manifest-v8.0.1.json`、`SHA256SUMS-v8.0.1.txt`；`shasum -a 256 -c` 通过。
- 发布清单与 tag 内容逐文件一致（167 项、`source_data` 为空）；ZIP 169 条目，无 `data/`、`.runtime/`、`.venv/`、`analysis/`、`workspace/`；四个 `.command`/`.sh` 为 0755。
- 公开更新器 `ReleaseClient` 识别到最新正式版本 8.0.1 与三个附件；解包后 `installed()` 报告 8.0.1、无 `data/raw/`。
- 本机完整回归：155 passed / 1 skipped。

## 2026-10-20 全局 multiomics 与 /cd

- 新增用户级 `multiomics` 启动器与受管 PATH：POSIX 使用 `~/.local/bin` + 带标记的 shell 配置块，Windows 使用 `%USERPROFILE%\.local\bin` + 当前用户 `Path`；启动器设置 `PYTHONPATH` 并使用 `-P` 避免从调用目录导入同名包。
- 新增 `/cd` 工作区切换：应用代码根与数据/配置根分离；未初始化目录从安装模板生成缺失配置与示例，已有文件不覆盖；macOS/Windows 运行绑定新工作区的 doctor，失败不切换。
- 手动验收（macOS）：从 `/tmp` 通过临时 PATH 运行 `multiomics --version`，输出 `multiomics 8.1.0`；在临时目录执行 `/cd`，生成 `config/study_config.yaml`、`runtime.local.yaml`、`api.local.yaml`、合成示例和 `.runtime/doctor.json`。
- 本机完整回归：184 passed / 2 skipped，耗时 3m02s；`public_release audit` 175 项通过。
- Windows 原生用户 PATH、AppContainer 与 macOS 原生沙箱仍需目标机验收；CI 便携测试不替代原生验收。

## 2026-10-20 v8.1.0 发布

- 版本提交 8b9c63b；主分支 CI（run 39439373796）ubuntu、macOS、Windows 均 success。
- 发布后文档记录提交 402d6a1 的 CI（run 39440307545）macOS job 首次被取消（15 分钟未完成），重跑后三平台 success；不涉及代码变更。
- tag `v8.1.0` 触发发布工作流（run 39439385170）：validate 与 publish 均 success。
- Release 附件：`tme_agent-v8.1.0.zip`（458353 B，sha256 29dd2a5f…）、`manifest-v8.1.0.json`（31974 B，sha256 41dcc9ef…）、`SHA256SUMS-v8.1.0.txt`（174 B）；下载后 `shasum -a 256 -c` 通过。
- 发布清单与 tag 内容一致：175 项、`source_data` 为空；ZIP 177 条目，无 `data/`、`.runtime/`、`.venv/`、`analysis/`、`workspace/`；四个 `.command`/`.sh` 为 0755；本地构建 ZIP 哈希与 GitHub 附件一致。
- `public_update.ReleaseClient` 识别到最新正式版本 8.1.0 与三个附件。
- Release：https://github.com/shineshen007/tme_agent/releases/tag/v8.1.0

## 2026-10-22 /model 界面精简

- `/model` 只保留服务、接口地址、模型名、认证方式和 API key；密钥环境变量、JSON 返回模式、超时、输出长度、长度字段和 temperature 从界面移除。
- 隐藏参数从 `config/api.local.yaml` 读取并原样写回；切换服务自动更新 `api_key_env` 与认证方式。
- 默认输出上限由 8000 调整为 32000；同步发行模板、本地配置、TUI 客户端与 orchestrator 兜底默认值。
- 专项测试 31 passed；完整回归 187 passed / 2 skipped；`public_release audit` 175 项通过，版本仍为 8.1.0（本次未发布新版本）。

## 2026-10-22 首次安装用户包优先

- Python 候选新增用户级 `~/.local/bin`、pyenv、uv、conda、asdf、mise 和 Windows 用户目录；候选选择优先全部依赖可复用，其次复用数量最多。
- `inspect_python` 显式加入 user site；已有 venv 也写入复用库 `.pth`，避免检查可见但运行时导入失败。
- R 候选新增用户级 Rscript、conda、Scoop 和 MacPorts；`R_LIBS_USER`/`R_LIBS_SITE`/`R_LIBS` 显式加入 `.libPaths()`，bootstrap 缺失包检查与 `runtime.local.yaml` 使用相同库路径。
- Windows 私有运行时校验关闭 user site，防止隔离运行时误用宿主用户包。
- 任务运行器与 planner 使用同一套 R 库路径，用户库中的包不仅被列在提示中，也被沙箱授权读取。
- 新增 `tests/test_environment_discovery.py` 8 项；专项 51 passed；完整回归 195 passed / 2 skipped；`public_release audit` 176 项通过，版本仍为 8.1.0（本次未发布新版本）。

## 2026-10-22 v8.1.1 发布与 v8.1.2 修复

- v8.1.1 已发布：tag `v8.1.1`，Release 附件 `tme_agent-v8.1.1.zip`（466755 B，sha256 6a77739b…）、`manifest-v8.1.1.json`（32164 B，sha256 73b0e522…）、`SHA256SUMS-v8.1.1.txt`；下载校验通过。
- v8.1.1 主分支 CI（run 39714115480）macOS/ubuntu success、Windows failure；Release 工作流（run 39714118690）validate/publish success。
- v8.1.2 已发布（tag `v8.1.2`），但 Windows CI 仍失败：R 4.4 在自定义 `.libPaths()` 上报 `The system cannot find the path specified`。
- v8.1.3 修复：`inspect_r` 改为 Python 扫描用户库 `DESCRIPTION`，R 只检查系统库；bootstrap 复用同一清单；用户 Python 发现测试隔离 `LOCALAPPDATA`。
- v8.1.3 主分支 CI（run 39725966979）三平台 success；`prepare --version 8.1.3` 通过，`audit` 176 项通过；完整回归 195 passed / 2 skipped。
- v8.1.1 Release 附件：`tme_agent-v8.1.1.zip`（466755 B，sha256 6a77739b…）、`manifest-v8.1.1.json`（32164 B，sha256 73b0e522…）、`SHA256SUMS-v8.1.1.txt`；下载校验通过。

## 当前状态

- 三平台 CI 全绿；Windows 原生 AppContainer 隔离、macOS 原生沙箱与真实模型 API 仍未验收，`docs/VALIDATION.md` 不据此声称原生通过。
- v8.1.1、v8.1.2 已发布但 Windows CI 失败；v8.1.3 已修复并三平台 CI 通过，待发布验证。本机完整回归 195 passed / 2 skipped；`public_release audit` 176 项通过。


维护者发布前应复核暂存白名单与 Release 内容；用户只安装 Release 附件 ZIP。研究输入、结果、个人配置和未确认再分发许可的参考资源均不随公开包交付。
