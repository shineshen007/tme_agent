# Changelog

## Unreleased

- 修复 macOS 入口脚本在 Git 工作副本中缺少可执行位、双击提示"没有正确的访问权限"：`setup_macos.command`、`start_macos.command`、`diagnose_macos.command`、`scripts/00_run.sh` 的索引模式改为 100755，与发行 ZIP 的 0755 规则一致。
- 增加回归测试：锁定索引可执行位，并校验发行 ZIP 与 manifest 中 `.command`/`.sh` 的 0755 模式一致。
- 修复非 Linux 平台上的 16 项便携回归失败：单元测试显式替换 `native_dist.doctor.require_probe`，并新增 `real_probe` 标记用例验证缺记录/指纹不符时仍拒绝；临时项目根统一解析 `/var`→`/private/var` 别名，测试配置复用本机 R 库以满足输入探测的 jsonlite 依赖。
- 修复 `test_deepseek_legacy_parameter` 依赖环境真实 `DEEPSEEK_API_KEY`：改用合成密钥并隔离常用密钥环境变量。
- TUI 用例改为按墙钟等待后台规划线程，不再按 `pause()` 次数计时。
- CI 增加 `fail-fast: false`，避免单平台失败掩盖其余平台结果。

## v8.0.0

- 从正式提供的 v8 ZIP 导入源码，保留依赖感知、Bioconductor/GSVA 和 macOS 沙箱修复。
- 提供无研究数据的公开 GPL-3.0-or-later 源码及独立 Release 安装包。
- 增加合成示例、本地配置模板、GitHub Releases 更新、冲突检查、任务互斥与源码回滚。
- 增加版本准备、白名单构建、发布流水线和更新器故障测试。
- Windows/macOS 原生验收仍待目标机器执行；真实 API 未测试。
