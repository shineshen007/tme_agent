updated_at: 2026-10-22T13:10:00+08:00

## current_goal

v8.1.2 已发布但 Windows CI 失败；v8.1.3 已修复 R 用户库探测并三平台 CI 通过，待提交、打 tag 和发布。

## completed

- `/model` 界面精简：移除密钥环境变量、JSON 返回模式、超时、输出长度、长度字段和 temperature 输入；保留服务、接口地址、模型名、认证方式和 API key。
- 隐藏参数继续从 `config/api.local.yaml` 读取并原样写回；切换服务时自动更新 `api_key_env` 与认证方式；高级用户可编辑本地配置。
- 默认输出上限由 8000 调整为 32000；同步 `config/study_config.example.yaml`、本地 `config/study_config.yaml` 与客户端兜底默认值。
- 新增 `/model` 字段精简、隐藏参数保留、服务切换和默认 32K 回归测试；完整回归 187 passed / 2 skipped。
- 首次安装优先扫描用户级解释器：Python 增加 `~/.local/bin`、pyenv、uv、conda、asdf、mise 与 Windows 用户目录；R 增加用户 `Rscript`、conda、Scoop 与 MacPorts 路径。
- Python 检查显式加入 user site，并对已有 venv 写入复用库 `.pth`；R 检查显式加入 `R_LIBS_USER`/`R_LIBS_SITE`/`R_LIBS`，bootstrap 缺失包检查与 `runtime.local.yaml` 共用同一套库路径。
- 安装候选选择改为优先全部依赖可复用，其次复用数量最多；Windows 私有运行时校验关闭 user site，避免误用宿主用户包。
- 新增 `tests/test_environment_discovery.py`；完整回归 195 passed / 2 skipped，发行白名单 176 项。
- v8.1.1 已发布：提交 `6643acf`，tag `v8.1.1`；Release 附件：`tme_agent-v8.1.1.zip`（466755 B，sha256 6a77739b…）、`manifest-v8.1.1.json`（32164 B，sha256 73b0e522…）、`SHA256SUMS-v8.1.1.txt`（174 B）；下载校验通过。
- v8.1.1 主分支 CI（run 39714115480）macOS/ubuntu success、Windows failure：测试未隔离 `LOCALAPPDATA`；R 用户库探测在 Windows Rscript 输出版本前失败。Release 工作流（run 39714118690）validate/publish success，附件已发布。
- v8.1.2 已发布：提交 `bff78e2`，tag `v8.1.2`；Release 工作流成功，但主分支 Windows CI 仍失败（R 用户库 `.libPaths()` 在 Windows R 4.4 上报路径错误）。
- v8.1.3 修复：`inspect_r` 改为 Python 扫描用户库 `DESCRIPTION`，R 只检查系统库；bootstrap 复用同一清单；Windows 用户 Python 测试隔离 `LOCALAPPDATA`。
- v8.1.3 主分支 CI（run 39725966979）ubuntu、macOS、Windows 全部 success；`prepare --version 8.1.3` 与 audit 176 项通过。
- v8.1.0 发布：提交 `8b9c63b`，tag `v8.1.0`；主分支 CI（run 39439373796）ubuntu、macOS、Windows 全部 success；发布工作流（run 39439385170）validate 与 publish 均 success。
- 发布后文档记录提交 `402d6a1` 的 CI（run 39440307545）macOS job 首次被取消（15 分钟未完成），重跑后三平台 success；发布提交本身的 CI 无此问题。
- Release 附件：`tme_agent-v8.1.0.zip`（458353 B，sha256 29dd2a5f…）、`manifest-v8.1.0.json`（31974 B，sha256 41dcc9ef…）、`SHA256SUMS-v8.1.0.txt`（174 B）；`shasum -a 256 -c` 通过。
- 发布内容校验：manifest 175 项、`source_data` 为空；ZIP 177 条目，无 `data/`、`.runtime/`、`.venv/`、`analysis/`、`workspace/`；四个 `.command`/`.sh` 为 0755；本地构建 ZIP 哈希与 GitHub 附件一致。
- `public_update.ReleaseClient` 识别到最新正式版本 8.1.0 与三个附件。
- 新增 `native_dist/command_link.py`：生成 POSIX `~/.local/bin/multiomics` / Windows `%USERPROFILE%\.local\bin\multiomics.cmd`，设置 `PYTHONPATH`、转发参数、原子写入、拒绝覆盖非本工具文件；提供 `install/status/uninstall`。
- 新增受管 PATH 配置：POSIX 写入带标记的 zsh/bash/fish/profile 块，Windows 写入当前用户 `HKCU\Environment` 的 `Path`；不需要管理员权限；`setup --no-path` 跳过，`--path` 强制启用，偏好记录在 `.runtime/command_link.json`。
- `bootstrap.setup` 在 doctor 通过后安装启动器，把结果写入 `.runtime/install_report.json`；启动器失败只告警，不把环境安装判为失败。
- 新增 `multiomics_tui/workspace.py`：`/cd` 路径解析（`~`、相对路径、带空格路径）、自动初始化（study 配置、runtime 配置、无密钥的模型配置、合成示例）、独占创建与符号链接拒绝。
- `Session` 增加 `app_root` / 当前工作区分离；`/cd` 支持无参数显示、切换、`-` 返回上一目录；有活动后台任务或探测失败时拒绝切换，旧任务保留在原工作区。
- `native_dist.layout.code_root` 在工作区无代码时回退到安装目录；`doctor.runtime_fingerprint` 从代码根读取 requirements 与私有运行时，使新工作区可生成绑定自身的 `.runtime/doctor.json`。
- Windows 任务运行时根与工作区根分离：`_step_environment` 设置 `MULTIOMICS_RUNTIME_ROOT`，`windows_native.launch_paths` 从安装目录共享私有 Python/R 运行时，不复制到每个 `/cd` 工作区。
- TUI 接入 `/cd`：命令补全、帮助文本、切换到 Home、清空旧任务视图、更新标题/状态栏；REPL 提示符显示当前工作区。
- 新增 `tests/test_command_link.py`、`tests/test_cd_workspace.py`；发行白名单 171 → 175 项。
- 文档更新：`README.md`、`README.zh-CN.md`、`docs/INSTALL_UPDATE_zh.md`、`CHANGELOG.md`。

## in_progress

- v8.1.3 发布：提交当前修复，推送 `v8.1.3` tag，等待 release 工作流和附件校验。
- Windows 原生 PATH/AppContainer 验收与 macOS 原生沙箱验收待执行；本机 `/cd` 真实 doctor 已在 macOS 通过。

## key_decisions

- `multiomics` 默认进入安装目录；`multiomics --project-root <dir>` 指定其他工作区；`/cd` 仅影响当前会话，不跨会话记住。
- `/cd` 到未初始化目录时自动生成缺失文件，已有文件绝不覆盖；数据工作区不复制代码树。
- 应用代码根与用户工作区根分离；doctor 记录绑定工作区配置与库，不把安装目录的探测记录直接复用到新工作区。
- PATH 只改当前用户配置，使用带标记的受管块，支持 `uninstall` 精确移除。
- `/model` 主界面不暴露技术参数；隐藏值从本地配置读取并原样保存，避免简化界面导致既有设置丢失。默认输出上限为 32000。
- 历史固定流程 `/run`、`/data` 仍要求完整项目根；数据工作区以自然语言任务为主。

## changed_files

- 新增：`native_dist/command_link.py`、`multiomics_tui/workspace.py`、`tests/test_command_link.py`、`tests/test_cd_workspace.py`。
- 修改：`native_dist/bootstrap.py`、`native_dist/environment.py`、`native_dist/windows_runtime.py`、`native_dist/layout.py`、`native_dist/doctor.py`、`orchestrator/llm_client.py`、`multiomics_tui/controller.py`、`multiomics_tui/app.py`、`multiomics_tui/fallback.py`、`multiomics_tui/task_runner.py`、`multiomics_tui/windows_native.py`、`multiomics_tui/model_panel.py`、`multiomics_tui/model_config.py`、`multiomics_tui/model_client.py`、`multiomics_tui/r_env.py`、`config/study_config.example.yaml`、`tests/test_model_interfaces.py`、`tests/test_v8_distribution.py`、`PUBLIC_FILES.json`、`README.md`、`README.zh-CN.md`、`docs/INSTALL_UPDATE_zh.md`、`docs/VALIDATION.md`、`CHANGELOG.md`、`CURRENT_STATE.md`。
- 新增：`tests/test_environment_discovery.py`。

## latest_outputs

- 首次安装发现专项：`tests/test_environment_discovery.py` 8 passed；`inspect_python` 可识别 `PYTHONUSERBASE` 下的用户包，`inspect_r` 可识别 `R_LIBS_USER` 用户库。
- 完整回归：195 passed / 2 skipped，耗时 3m07s。
- `python -m native_dist.public_release audit`：176 项通过，版本 8.1.3。
- `/model` 精简专项：`tests/test_model_interfaces.py` 31 passed。
- v8.1.1 Release：https://github.com/shineshen007/tme_agent/releases/tag/v8.1.1
- v8.1.2 Release：https://github.com/shineshen007/tme_agent/releases/tag/v8.1.2（Windows CI 失败）
- v8.1.3 修复后主分支 CI（run 39725966979）三平台 success，待发布。
- 附件：`tme_agent-v8.1.1.zip`（sha256 6a77739b…）、`manifest-v8.1.1.json`（sha256 73b0e522…）、`SHA256SUMS-v8.1.1.txt`；下载后校验通过。
- 手动验收：从 `/tmp` 通过临时 PATH 运行 `multiomics --version`（输出 `multiomics 8.1.1`）；在临时目录执行 `/cd` 新工作区，自动初始化并生成 macOS doctor 记录。
- 新增测试覆盖：启动器内容/权限/幂等/拒绝覆盖、PATH 受管块增删、参数转发、工作区初始化、活动任务拒绝、探测失败回滚、TUI 回到 Home。

## blockers_or_warnings

- Windows 原生 AppContainer、Windows 用户 PATH 实际生效、macOS 原生沙箱仍需目标机验收；CI 便携通过不等于原生通过。
- v8.1.1/8.1.2 的 Windows CI 曾失败；v8.1.3 已修复并通过三平台 CI（run 39725966979），待 release 验证。
- PATH 修改后需要新终端；`/cd` 不改变父 shell 目录。
- 自动初始化只写缺失文件；数据工作区不支持历史固定流程 `/run`、`/data`。
- 用户级解释器和包优先复用；版本冲突或导入失败时回退到项目环境，显式 `MULTIOMICS_PYTHON` / `MULTIOMICS_RSCRIPT` 可覆盖。
- 真实模型 API 未验收。

## next_step

提交并推送 `v8.1.3`，确认 release 工作流与附件校验通过；随后在 Windows 与 Intel macOS 目标机执行原生验收并记入 `docs/VALIDATION.md`。
