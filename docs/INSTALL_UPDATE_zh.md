# 安装与更新

## 安装

1. 下载 Release 附件 `tme_agent-vX.Y.Z.zip`、`manifest-vX.Y.Z.json`、`SHA256SUMS-vX.Y.Z.txt`。Linux 可用 `sha256sum -c SHA256SUMS-vX.Y.Z.txt`；Mac 用 `shasum -a 256 -c SHA256SUMS-vX.Y.Z.txt`；Windows PowerShell 用 `Get-FileHash` 对照清单。
2. 解压到新的用户可写目录。先运行 `python -m native_dist.bootstrap check`，再运行 `python -m native_dist.bootstrap setup`。安装器优先复用用户已安装的兼容 Python/R 解释器与包（user site、pyenv/uv/conda、`R_LIBS_USER` 等），只补齐缺失依赖，不修改全局软件。无需安装 Git 或 GitHub CLI。
3. 安装成功后，安装器会创建用户级 `multiomics` 命令：macOS/Linux 为 `~/.local/bin/multiomics`，Windows 为 `%USERPROFILE%\.local\bin\multiomics.cmd`。打开新终端后可在任意目录直接运行 `multiomics`，无需先激活 `.venv`；需要手动启动时可激活 `.venv` 后运行 `python -m multiomics_tui`，或双击对应 `start` 文件。
4. 在本地 `data/` 放自己的输入；输入保持只读。首次配置复制的合成示例不会覆盖已有文件。缺少真实输入时只能做示例任务，不存在默认队列。

## 全局命令与工作目录

- `multiomics` 默认使用安装目录作为工作区；`multiomics --project-root <目录>` 可指定起始工作区。
- 界面内 `/cd` 显示当前工作目录，`/cd <目录>` 切换当前会话的工作区。支持 `~`、相对路径和带空格的路径。
- 目标目录没有 `config/study_config*.yaml` 时，会从安装模板生成 `config/study_config.yaml`、`config/runtime.local.yaml`、`config/api.local.yaml`（不含密钥）和合成示例；已有文件一律不覆盖，配置路径上的符号链接会被拒绝。
- macOS/Windows 上首次进入新工作区会运行本地 doctor 并生成绑定该工作区的 `.runtime/doctor.json`；检查失败则不切换。
- `/cd` 只影响当前会话，不改变父 shell 的目录，也不会跨会话记住。下次仍从安装目录或 `--project-root` 指定的目录开始。
- 有活动后台任务时拒绝切换；旧任务保留在原工作区，切回后可用 `/session` 恢复。
- 数据工作区以自然语言任务为主；历史固定流程 `/run`、`/data` 仍需完整项目根，不会自动把代码树复制到工作区。

安装器会把启动器目录加入当前用户 PATH：POSIX 使用带标记的 shell 配置块（zsh/bash/fish/profile），Windows 使用当前用户 `Path`。只修改用户级配置，不需要管理员权限。跳过 PATH 配置可使用 `setup --no-path`；之后可运行：

```bash
python -m native_dist.command_link status
python -m native_dist.command_link install
python -m native_dist.command_link uninstall
```

`uninstall` 只删除本工具生成的启动器、受管 PATH 条目和记录，不删除安装目录或用户数据。

Python 3.12–3.14、R 4.4+；CRAN/Bioconductor 依赖由原 v8 安装器管理。`setup --offline` 仅使用已安装依赖，缺项明确失败。`config/study_config.yaml`、`config/runtime.local.yaml`、`config/api.local.yaml` 均是本地文件，不入发行清单。

系统凭据库默认启用；无 Linux Secret Service 时可显式选择本地配置中的 `credential_storage: memory`，每次会话重新输入密钥。密钥不得写入 YAML、终端参数、Git 或日志。

## 更新命令

在任意目录运行 `multiomics update --check` 查询，运行 `multiomics update` 升级。指定正式版本使用 `--version vX.Y.Z`；不允许通过此参数降级，恢复上版使用 `--rollback`。在安装目录也可使用 `python -m multiomics_tui update ...`；全局启动器会把安装目录作为项目根。

更新器只访问 `shineshen007/tme_agent` 的 Releases，通过 HTTPS 获取附件与校验和，验证 GitHub digest（若提供）、ZIP 和逐文件 SHA256。校验和用于检查下载和发布内容一致性，不是独立于 GitHub 账号的签名。没有正式 Release、网络不可用、API 限流或附件缺失时停止，不替换源码。

包只允许管理声明过的源码、模板、文档和示例；所有路径先校验，拒绝路径穿越、链接、大小写冲突和异常体积。数据、凭据、配置、环境、历史结果从不作为发行文件管理。更新期间 bootstrap 使用 `--preserve-local-config`，已有 runtime.local.yaml 逐字节保留；若其环境已不兼容，诊断失败并恢复源码，不静默重写配置。新增源码与本地同名文件冲突也会停止。

升级前关闭界面并停止后台任务。更新器使用原任务的 admission/pipeline 锁及独立更新锁；检测到活动记录时拒绝操作。不要边编辑源码边更新；已有源码改动需先另行备份，并在新目录安装。

## 失败和回滚

源码备份及阶段记录在 `.runtime/public-updates/<id>/journal.json`。更新成功要求源码应用、bootstrap 环境准备和 doctor 均通过。任何应用或环境失败都会尝试自动恢复源码，并以非零退出码结束；依赖增加及环境诊断记录保留，不卸载全局或项目软件。若自动恢复失败会明确报错，保留备份。

运行 `python -m multiomics_tui update --rollback` 恢复最近一次尚未回滚的更新。更新中断后也使用此命令恢复，再重试。恢复前验证当前文件及备份哈希；升级后源码再次修改时拒绝覆盖。用户数据和配置不参与回滚。环境版本不会随源码回滚，请重新运行 bootstrap 检查兼容性。

Git 工作副本没有安装 manifest；维护者使用 `git pull --ff-only` 和 bootstrap。GitHub 自动生成的源码 ZIP 同样没有发行 manifest，请使用 Release 附件安装。

## 原生验收

运行 `python -m native_dist.diagnostics --project-root .`，检查 `.runtime/doctor.json`、`v8_acceptance.json` 和 `native_diagnostic.json`。合成验收不调用真实 API、不分析患者输入。回传前检查日志是否含个人路径，不回传数据或整个 `.runtime`。

Windows/Mac 原生状态以目标机日志为准。Linux 本地验收和三平台便携 CI 不能证明所有系统的原生沙箱或凭据服务均已通过。
