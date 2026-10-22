# Changelog

## v8.1.2

- 修复 v8.1.1 Windows CI 失败：R 用户库探测移除对 `R.version$arch/platform` 的路径推导，仅保留 `R_LIBS_USER`/`R_LIBS_SITE`/`R_LIBS` 与通用用户目录，避免 Windows Rscript 在输出版本前失败。
- 修复 Windows 用户 Python 发现测试未隔离 `LOCALAPPDATA`：测试现在把用户级 Python 放到临时 LOCALAPPDATA 下验证。

## v8.1.1

- 首次安装检查优先发现用户级 Python：扫描 `~/.local/bin`、pyenv、uv、conda、asdf、mise 和 Windows 用户 Python 目录，再回退到 PATH 与系统目录。
- Python 检查显式加入 user site（即使 `PYTHONNOUSERSITE` 或 venv 默认禁用），已有 venv 也会写入复用库 `.pth`，避免“检查能看到、运行时导入不到”。
- R 检查显式加入 `R_LIBS_USER`、`R_LIBS_SITE`、`R_LIBS` 与常见用户库，`inspect_r`、bootstrap 缺失包检查和 `runtime.local.yaml` 使用同一套库路径。
- 安装候选选择改为优先“全部依赖可复用”，其次“复用数量最多”，最后才按顺序选择；显式 `MULTIOMICS_PYTHON` / `MULTIOMICS_RSCRIPT` 仍优先。
- Windows 私有运行时校验显式关闭 user site，确保隔离运行时不会误用宿主用户包。
- 任务运行器使用与 planner 相同的 R 库路径，用户库中的包在沙箱内可读可用。
- 新增用户解释器、user site、R 用户库和候选选择回归测试；发行白名单增至 176 项。
- `/model` 设置界面精简：只保留服务、接口地址、模型名、认证方式和 API key；密钥环境变量、JSON 返回模式、超时、输出长度、长度字段和 temperature 从界面移除，减少必填输入。
- 隐藏参数继续从 `config/api.local.yaml` 读取并原样写回；切换服务时自动更新密钥环境变量与认证方式。高级用户仍可直接编辑该文件。
- 默认输出长度上限由 8000 调整为 32000，并同步更新 `config/study_config.example.yaml` 与本地配置模板。
- 新增界面字段精简、隐藏参数保留、服务切换和 32K 默认值回归测试。

## v8.1.0

- 新增用户级 `multiomics` 启动器：安装成功后可在任意目录启动，自动设置 `PYTHONPATH` 并转发参数；默认使用安装目录，`--project-root` 可指定其他工作区。
- 新增受管 PATH 配置：POSIX 写入带标记的 zsh/bash/fish/profile 配置块，Windows 写入当前用户 `Path`；只修改用户级设置，无需管理员权限。`setup --no-path` 可跳过，`--path` 可重新启用。
- 新增 `python -m native_dist.command_link status|install|uninstall`，用于检查、修复或移除启动器和 PATH 条目。
- 新增 `/cd` 命令：显示或切换当前会话的工作区；支持 `~`、相对路径和带空格的路径，未初始化目录会从安装模板生成缺失配置与示例，已有文件不覆盖。
- 将应用代码根与工作区根分离：`native_dist.layout.code_root` 在工作区无代码时回退到安装目录；doctor 指纹从代码根读取 requirements 与私有运行时，使新工作区可独立完成本机探测。
- `/cd` 在 macOS/Windows 上为新工作区运行 doctor；检查失败不切换。有活动后台任务时拒绝切换，旧任务保留在原工作区。
- 新增启动器、PATH、工作区切换和 TUI 重置回归测试；发行白名单由 171 项增至 175 项。
- Windows 共享运行时：任务环境新增 `MULTIOMICS_RUNTIME_ROOT`，`windows_native.launch_paths` 从安装目录读取私有 Python/R 运行时，避免为每个 `/cd` 工作区复制运行时。

## v8.0.2

- 新增品牌资源 `assets/`：`logo-on-dark.svg`、`logo-on-light.svg`（GitHub 深浅主题自适应）与 `logo-lockup.svg`（社交预览/OG 图），均为单色矢量，无 PNG。
- README 改为 Paseo 风格版式：居中 logo、标题、语言行、徽章行、一句话简介、特性列表、Mermaid 架构图、Agents 表、快速开始、更新/回滚、维护者发布与许可。
- 新增英文主 README（`README.md`）与简体中文对照（`README.zh-CN.md`），语言行互相链接。
- 发行白名单增加 `assets` 区域（`.svg`）与顶层 `README.zh-CN.md`，公开发行文件由 167 项增至 171 项。

## v8.0.1

- 修复 macOS 入口脚本在 Git 工作副本中缺少可执行位、双击提示"没有正确的访问权限"：`setup_macos.command`、`start_macos.command`、`diagnose_macos.command`、`scripts/00_run.sh` 的索引模式改为 100755，与发行 ZIP 的 0755 规则一致。
- 增加回归测试：锁定索引可执行位，并校验发行 ZIP 与 manifest 中 `.command`/`.sh` 的 0755 模式一致。
- 修复非 Linux 平台上的 16 项便携回归失败：单元测试显式替换 `native_dist.doctor.require_probe`，并新增 `real_probe` 标记用例验证缺记录/指纹不符时仍拒绝；临时项目根统一解析 `/var`→`/private/var` 别名，测试配置复用本机 R 库以满足输入探测的 jsonlite 依赖。
- 修复 `test_deepseek_legacy_parameter` 依赖环境真实 `DEEPSEEK_API_KEY`：改用合成密钥并隔离常用密钥环境变量。
- TUI 用例改为按墙钟等待后台规划线程，不再按 `pause()` 次数计时。
- CI 增加 `fail-fast: false`，避免单平台失败掩盖其余平台结果。
- 修复 CI 三平台失败：测试会话从宿主 R 探测 `R_HOME` 与 `.libPaths()` 并写入临时项目配置，解决沙箱把 `HOME` 指向任务目录后找不到 `jsonlite` 的问题；`r_env` 用例隔离 `R_LIBS*` 环境变量。
- 修复 Windows 代码生成崩溃：`os.O_NOFOLLOW` 在 Windows 不存在，`_write_new` 现使用 `getattr(os, "O_NOFOLLOW", 0)`，`O_EXCL` 继续保证不覆盖。
- 收窄 `redact()` 的环境变量扫描：仅把长度 ≥16 的环境值当作密钥，避免 CI 注入的短变量被误判为泄漏；已注册密钥与长环境密钥仍替换。
- 输入探测失败时错误信息附带脱敏、限长的 `probe.log` 末尾；CI 测试使用 `--basetemp=.pytest-tmp` 并在失败时上传 probe/step 日志。
- 修复 setup-r 环境下的执行拒绝：`_step_environment` 显式授权 Rscript 的解析路径及其 `bin/`、`lib/` 目录，避免 Landlock 拒绝 `/opt/R` 下的解释器与 `libR.so`。
- 修复 CI 缺少探测依赖：工作流安装 `jsonlite` 与 `digest`（`input_probe.R` 两者都用）。
- Windows 单元测试替换 AppContainer 启动：输入探测的受保护子进程调用抽为 `input_inspection._run_guarded` 接缝，测试在父进程侧直接运行探测命令；私有运行时与原生隔离仍由 bootstrap 和 Windows 原生验收覆盖。

## v8.0.0

- 从正式提供的 v8 ZIP 导入源码，保留依赖感知、Bioconductor/GSVA 和 macOS 沙箱修复。
- 提供无研究数据的公开 GPL-3.0-or-later 源码及独立 Release 安装包。
- 增加合成示例、本地配置模板、GitHub Releases 更新、冲突检查、任务互斥与源码回滚。
- 增加版本准备、白名单构建、发布流水线和更新器故障测试。
- Windows/macOS 原生验收仍待目标机器执行；真实 API 未测试。
