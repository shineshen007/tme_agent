# tme_agent

基于 v8 的多组学自然语言终端工具。输入分析目标，审核输入绑定、方法和代码后执行；保留任务日志、完整结果及来源。公开版采用 **GPL-3.0-or-later**，不包含患者数据或原研究结果。

仓库：<https://github.com/shineshen007/tme_agent>。

## 使用者安装

在 [Releases](https://github.com/shineshen007/tme_agent/releases) 下载 `tme_agent-vX.Y.Z.zip` 和同版本校验清单，核对 SHA256 后完整解压到新的、可写目录。使用 Release 附件 ZIP；GitHub 自动生成的 “Source code” ZIP 是维护源码，不含安装清单，不能直接用于发行更新。

需要 Python 3.12–3.14、R 4.4+。已有软件直接复用，依赖只补齐到项目环境。Mac/Windows 可使用 `setup_macos.command` / `setup_windows.cmd`；也可在项目目录运行：

```bash
python -m native_dist.bootstrap check
python -m native_dist.bootstrap setup
```

安装后，Mac/Linux 激活 `source .venv/bin/activate`；Windows PowerShell 激活 `.venv\Scripts\Activate.ps1`，或直接使用 `.venv\Scripts\python.exe`。下文 `python` 均指安装目录的 `.venv` Python。

```bash
python -m multiomics_tui
```

首次安装生成本地配置和 4 × 4 合成表达示例。真实数据自行放入本地 `data/raw/`、`data/metadata/`、`data/signature/`；不要上传这些目录。界面 `/model` 设置服务，`/key` 在掩码框输入密钥并使用系统凭据库。Linux 无桌面凭据服务时，可显式将本地配置 `distribution.credential_storage` 改为 `memory`，密钥仅本次进程有效，不写磁盘。无模型密钥也能运行离线诊断。

自然语言新任务是公开版入口；原固定研究流程需要独立的队列配置，不可用公开模板恢复。模型生成的解释和代码不代表独立验证，分析前仍需确认统计单位、分组和研究设计。

## 使用者更新

关闭界面、完成或停止后台任务，在安装目录运行：

```bash
python -m multiomics_tui update --check
python -m multiomics_tui update
python -m multiomics_tui update --version v8.0.0
python -m multiomics_tui update --rollback
```

默认更新到最新正式 Release。更新验证下载、文件清单和任务锁；源码有本地改动时拒绝覆盖。数据、结果、个人配置和已有环境不被发行包替换。代码应用或环境准备失败时恢复发行源码；新增依赖不自动卸载。只有代码、环境和诊断均通过才报告成功。完成后重新启动界面。

原 v8/v7 ZIP 安装先在新目录安装一次公开版，自行放置本地数据；不合并覆盖旧目录。Git clone 工作副本采用下面的维护方式，不能使用发行更新器。

## 维护者发布

本目录保留既有 `main` 历史；首次上传在本目录完成审核：

```bash
git status --short
python -m native_dist.public_release audit
python -m pytest -q
```

使用以下白名单命令暂存，避免把本地数据加入 Git：

```bash
python -c "import json,subprocess; subprocess.run(['git','add','--',*json.load(open('PUBLIC_FILES.json',encoding='utf-8'))['files']],check=True)"
git diff --cached --stat
git commit -m "Release public v8.0.0"
git push origin main
git tag -a v8.0.0 -m "Public v8.0.0"
git push origin v8.0.0
```

标签触发 Actions 验证和生成 Release。公开源码、安装包和清单均不带研究数据。发布前确认 GitHub Actions 已启用，维护者有推送权限；Git 使用自己的安全认证，不把 token 写入配置或命令。

以后修改程序，新增文件时同时维护 `PUBLIC_FILES.json` 的显式白名单，更新 CHANGELOG，再执行（以 v8.0.1 为例）：

```bash
git pull --ff-only origin main
python -m native_dist.public_release prepare --version 8.0.1
python -m pytest -q
python -m native_dist.public_release build --output dist
python -c "import json,subprocess; subprocess.run(['git','add','--',*json.load(open('PUBLIC_FILES.json',encoding='utf-8'))['files']],check=True)"
git diff --cached --stat
git commit -m "Release v8.0.1"
git push origin main
git tag -a v8.0.1 -m "Release v8.0.1"
git push origin v8.0.1
```

删除文件时先从白名单移除，再显式 `git rm -- <路径>`。同一版本号禁止替换已发布内容。维护者更新本地运行环境使用 `python -m native_dist.bootstrap setup`。

## 文档与状态

- [安装、更新和回滚](docs/INSTALL_UPDATE_zh.md)
- [第三方资源与本地数据](docs/THIRD_PARTY_RESOURCES.md)
- [源码来源与基线](docs/V8_SOURCE.json)
- [本地验证结果](docs/VALIDATION.md)
- [当前交付状态](CURRENT_STATE.md)
- [版本记录](CHANGELOG.md)
- [许可声明](docs/LICENSE_NOTICE.md)

Windows、Intel Mac 和 Apple Silicon 的原生隔离仍需目标机器实测。CI 的便携测试不代替原生隔离验收。`python -m native_dist.diagnostics --project-root .` 只运行本地检查与合成任务，不调用真实模型 API。
