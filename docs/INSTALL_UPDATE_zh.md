# 安装与更新

## 安装

1. 下载 Release 附件 `tme_agent-vX.Y.Z.zip`、`manifest-vX.Y.Z.json`、`SHA256SUMS-vX.Y.Z.txt`。Linux 可用 `sha256sum -c SHA256SUMS-vX.Y.Z.txt`；Mac 用 `shasum -a 256 -c SHA256SUMS-vX.Y.Z.txt`；Windows PowerShell 用 `Get-FileHash` 对照清单。
2. 解压到新的用户可写目录。先运行 `python -m native_dist.bootstrap check`，再运行 `python -m native_dist.bootstrap setup`。安装器只补项目依赖，不修改全局软件。无需安装 Git 或 GitHub CLI。
3. 激活 `.venv`，后续命令使用此环境的 Python。运行 `python -m multiomics_tui`，或双击对应 `start` 文件。
4. 在本地 `data/` 放自己的输入；输入保持只读。首次配置复制的合成示例不会覆盖已有文件。缺少真实输入时只能做示例任务，不存在默认队列。

Python 3.12–3.14、R 4.4+；CRAN/Bioconductor 依赖由原 v8 安装器管理。`setup --offline` 仅使用已安装依赖，缺项明确失败。`config/study_config.yaml`、`config/runtime.local.yaml`、`config/api.local.yaml` 均是本地文件，不入发行清单。

系统凭据库默认启用；无 Linux Secret Service 时可显式选择本地配置中的 `credential_storage: memory`，每次会话重新输入密钥。密钥不得写入 YAML、终端参数、Git 或日志。

## 更新命令

在已解压的安装目录运行 `python -m multiomics_tui update --check` 查询，运行 `python -m multiomics_tui update` 升级。指定正式版本使用 `--version vX.Y.Z`；不允许通过此参数降级，恢复上版使用 `--rollback`。

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
