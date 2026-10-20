# Changelog

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
