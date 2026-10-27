# Changelog

## v8.3.0 — 2026-10-27

科研专家工作流首版，包含此前尚未发布的内置数据和交互改动。工程验证与科研验收分别记录；真实模型端到端、真实多组学和人工科学审阅仍未完成。

### 科研专家与任务交互

- 科研专家入口：新增通用 ResearchContext、TaskResearchAdapter 与旧运行适配器；差异/富集显式映射、EvidenceRecord v3、来源行列定位与哈希核验接入既有科研会话。科研计划和规划知识快照纳入审批。
- Scientific Wiki：SQLite 事务、版本/来源/实体关系与反向关联、待审候选、哈希绑定人工审查、撤回、独立共享发布；新增 `/wiki`，`/ask` 复用已审查检索。规划、解释、假说和实验设计冻结实际知识版本。
- 文献与推理：PubMed/Europe PMC 可信连接器、缓存/限流/有界重试、完整可得摘要与相关段落、逐主张引文验证及支持/矛盾/直接性审查。修复旧文献部分失败仍返回 complete 和仅凭 PMID 接受引用的问题。
- 科研状态独立保存；`/research` 仅重试已批准科研阶段，不重跑统计。Critic 新分析需求进入新审批，空假说是合法结果；实验方案要求模型依据、对照、可证伪判据和未知参数。
- 冻结分析来源、知识、文献、提示、代码/schema、配置和模型审计；新增离线生命周期、隐私、篡改、失败恢复与科学边界测试。真实多组学科学验收仍为 NOT_EVALUABLE；用法见 `docs/RESEARCH_EXPERT.md`。
- 真实模型试运行暴露的接口边界已修复：查询限额前置校验、明确引用的主张 ID、直接证据的上下文条件、纠错前预算预留、逐次请求/回答审计；源校验失败的科研重试和审计落盘失败均显式 blocked，统计成功状态保留。真实模型最终复核仍待外发授权，失败记录完整保留。
- 命令补全：输入 `/` 后在输入框上方列出匹配命令及一行中英文说明（含参数形状提示，如 `/tail <job-id> [n]`）；`↑`/`↓` 移动高亮，`Tab` 或 `Enter` 采纳高亮项，`Esc` 关闭列表。采纳后只插入命令名、不自动提交；命令已完整输入时列表关闭，`Enter` 直接执行。
- 请求执行中补全只显示只读命令；`Tab`/`→` 的单行补全通道保留，不再出现“能补全但看不到候选”的情况。
- `/new` 回到欢迎界面：创建独立草稿任务并返回 Home，草稿任务与历史记录仍保存在磁盘上，可用 `/sessions`、`/session <task-id>` 重新打开；REPL 中 `/new` 后继续补充要求的行为不变。
- 命令目录集中到 `multiomics_tui/command_menu.py`，帮助文本、补全列表与测试同源，新增命令不会只出现在一处。
- `Tab` 补全与菜单采纳插入完全相同的文本（命令名本身），参数在命令名之后输入，避免两条补全路径产生不同结果。
- 新增 `tests/test_command_menu.py` 25 项；`PUBLIC_FILES.json` 176 → 178 项。

### 官方数据与发行

- 内置数据集 `datasets/TCGA-LIHC/` 改为**从 NCI GDC 官方下载**：TCGA-LIHC bulk RNA-seq，STAR - Counts 流程、GENCODE v36，19938 个 protein_coding 基因 × 424 样本（371 原发瘤、50 癌旁、3 复发瘤，50 对肿瘤/癌旁配对）。含 `counts_full.rds`（11.5 MB）、`counts_paired.rds`（2.7 MB）、`tpm_full.rds`（20.3 MB）、`gene_annotation.csv`（0.9 MB）与两份样本表；总计约 36 MB。
- 新增 `scripts/08_fetch_gdc_tcga_lihc.py`：只调 GDC REST API（纯标准库），分批下载 424 个文件的 tar 包并逐文件校验 `MANIFEST.txt` 的 md5 与字节数；支持 `--resume`、`--verify-only`；`fetch_report.json` 固定 `data_release`（46.0）、GDC API 版本（8.5.0）与 commit。
- `scripts/07_build_builtin_dataset.R` 改为读取 GDC TSV：只做结构规范化，counts 取 `unstranded`、TPM 取 `tpm_unstranded`，不重算、不归一化、不取整；保留 GENCODE 顺序，同一输入可复现相同 `.rds` 字节。
- **修复重复基因符号合并错误**：GENCODE v36 有 24 个重复 `gene_name`（PAR 基因等），旧派生表只取第一行，导致 `MATR3`/`PDE11A`/`POLR2J3`/`TMSB15B` 在 8,453,712 个单元中出现 560 个错误值（最大差 27 counts）。现按符号求和，规则写入 `quality_flags` 并逐个列出符号。
- **修复临床数据损坏**：旧表的 `morphology` 列被 Excel 误识别为日期（`Mar-70` 等，424/424 行）。新数据集不再随附诊断表，只保留 GDC 样本级字段（`sample_type`/`tissue_type`/`patient`/`group`/`pairing`/`pair_id`/`file_id`），并在 README 说明临床终点需另行从 GDC 获取。
- 样本标识改为 **16 位 TCGA sample barcode**（与 GDC 一致），`file_id` 列可回查原文件；README 给出与 28 位 aliquot barcode 的对照说明。
- 新增 `expression/gene_annotation.csv`（每行代表性 `gene_id`/`gene_name`/`gene_type`），用户可据此自行过滤基因类型。
- `dataset.json` 的 `provenance_status` 改为 `recorded`：记录 GDC 仓库、项目、访问级别 `open`、Data Release 46.0、API 8.5.0、commit、上游流程 `STAR - Counts`、基因模型 `GENCODE v36`、表达列与重复符号规则；`upstream_files` 记录 424 个文件的 `file_id`/md5/字节数。
- `python -m native_dist.public_release gate --provenance` 现返回 **exit 0**；release workflow 保持该步骤，作为未来新增数据集的强制门槛。
- 新增 `multiomics_tui/datasets.py`：读取并校验 `dataset.json`（逐文件 sha256/字节数），返回工作区播种映射与待补来源列表；缺 `datasets/` 视为未安装而非错误，清单不符则失败。
- 新增只读命令 `/datasets`：列数据集、维度、分组计数、冻结的 `input_bindings` 与 `provenance_status`；不打印任何样本 ID。`/datasets` 加入 `READONLY_COMMANDS` 与命令目录。
- 安装与 `/cd` 新工作区自动播种数据集到 `data/raw/TCGA-LIHC/` 与 `data/metadata/TCGA-LIHC/`，沿用独占创建，已有文件逐字节保留；播种放在配置写入之后，清单异常不会留下半配置工作区。
- 发行白名单新增 `datasets` 区域（`.rds`/`.csv`/`.md`/`.json`）；manifest 新增 `builtin_datasets` 字段并与 `datasets/*/dataset.json` 交叉校验，`source_data` 仍固定为空以继续保证私有队列不随发行。
- `audit` 对未记录的来源打印警告但不失败，日常 CI 与本地开发不受影响。
- 新增 `tests/test_builtin_datasets.py` 21 项（含 16 位 barcode、重复符号求和、无 Excel 损坏值、`gene_annotation` 结构）；`tests/test_public_distribution.py` 的发行夹具改为 session 级构建（含 36 MB 数据集后仍 43 s），并断言数据集随 ZIP 发布且仍不含 `data/`。
- `PUBLIC_FILES.json` 178 → 190 项；README、`docs/THIRD_PARTY_RESOURCES.md`、`docs/LICENSE_NOTICE.md`、`PROJECT_CONTEXT.md` 明确数据来自 GDC 开放获取层、不在 GPL 授权范围内。
- 删除用户手工放入的 `data/raw/TCGA/*.csv` 与据此构建的第一版数据集；删除前记录 sha256 与废弃原因于 `docs/VALIDATION.md`。

## v8.1.3

- 修复 v8.1.2 Windows CI 失败：`inspect_r` 不再通过 `.libPaths()` 注入用户库（Windows R 4.4 在自定义库路径上报 `The system cannot find the path specified`），改为用 Python 扫描用户库 `DESCRIPTION`，R 只检查系统库。
- bootstrap 的 R 缺失包检查复用同一份 Python 清单，不再二次运行带自定义 `.libPaths()` 的 R 检查；`runtime.local.yaml` 的用户库路径改由 Python 生成。
- v8.1.2 的 Windows 用户 Python 测试隔离和 R 探测错误日志保留；三平台 CI 已通过。

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
