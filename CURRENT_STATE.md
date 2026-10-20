updated_at: 2026-10-20T22:40:00+08:00

## current_goal

v8.1.0 已准备（全局 `multiomics` 启动器与 `/cd` 工作区切换）；发布流程待提交、打 tag 并验证 GitHub Release。

## completed

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

- v8.1.0 发布：更新 VERSION、CHANGELOG 与状态后提交，推送 `v8.1.0` tag，等待 release 工作流和附件校验。
- Windows 原生 PATH/AppContainer 验收与 macOS 原生沙箱验收待执行；本机 `/cd` 真实 doctor 已在 macOS 通过。

## key_decisions

- `multiomics` 默认进入安装目录；`multiomics --project-root <dir>` 指定其他工作区；`/cd` 仅影响当前会话，不跨会话记住。
- `/cd` 到未初始化目录时自动生成缺失文件，已有文件绝不覆盖；数据工作区不复制代码树。
- 应用代码根与用户工作区根分离；doctor 记录绑定工作区配置与库，不把安装目录的探测记录直接复用到新工作区。
- PATH 只改当前用户配置，使用带标记的受管块，支持 `uninstall` 精确移除。
- 历史固定流程 `/run`、`/data` 仍要求完整项目根；数据工作区以自然语言任务为主。

## changed_files

- 新增：`native_dist/command_link.py`、`multiomics_tui/workspace.py`、`tests/test_command_link.py`、`tests/test_cd_workspace.py`。
- 修改：`native_dist/bootstrap.py`、`native_dist/layout.py`、`native_dist/doctor.py`、`multiomics_tui/controller.py`、`multiomics_tui/app.py`、`multiomics_tui/fallback.py`、`multiomics_tui/task_runner.py`、`multiomics_tui/windows_native.py`、`PUBLIC_FILES.json`、`README.md`、`README.zh-CN.md`、`docs/INSTALL_UPDATE_zh.md`、`docs/VALIDATION.md`、`CHANGELOG.md`、`CURRENT_STATE.md`。

## latest_outputs

- 本机完整回归：`184 passed / 2 skipped`，耗时 3m02s。
- `python -m native_dist.public_release audit`：175 项通过，版本 8.1.0。
- 手动验收：从 `/tmp` 通过临时 PATH 运行 `multiomics --version`（输出 `multiomics 8.1.0`）；在临时目录执行 `/cd` 新工作区，自动初始化并生成 macOS doctor 记录。
- 新增测试覆盖：启动器内容/权限/幂等/拒绝覆盖、PATH 受管块增删、参数转发、工作区初始化、活动任务拒绝、探测失败回滚、TUI 回到 Home。

## blockers_or_warnings

- Windows 原生 AppContainer、Windows 用户 PATH 实际生效、macOS 原生沙箱仍需目标机验收；CI 便携通过不等于原生通过。
- PATH 修改后需要新终端；`/cd` 不改变父 shell 目录。
- 自动初始化只写缺失文件；数据工作区不支持历史固定流程 `/run`、`/data`。
- 真实模型 API 未验收。

## next_step

提交并推送 `v8.1.0`，验证 Release 附件（ZIP、manifest、SHA256SUMS）与公开更新器识别结果；随后在 Windows 与 Intel macOS 目标机执行原生验收并记入 `docs/VALIDATION.md`。
