# v8.0.0 本地验收

完成标记：`LOCAL_PUBLIC_RELEASE_VALIDATED_NATIVE_PENDING`。当前发布状态见 `CURRENT_STATE.md`；本文件保留历次本地验收记录。

## 2026-10-28 修复规划被隐私误判阻断（未发布）

- 现象：请求“计算TCGA-LIHC中KEGG通路gsva score”在规划阶段 `Planning blocked`，错误为 `PrivacyViolation: Outbound payload contains private or clinical content (contact)`。
- 根因：规划输入选择提示把 `catalog()` 全量清单发给模型，`data/raw/_gdc_cache/` 的 UUID 目录名（如 `1e1587e8-8938-4578-8529-…`）中 `8938-4578-8529` 命中电话正则。清单 160 条中 159 条来自该缓存。
- 修复：`CONTACT` 改为要求电话语义（标签 / `+` 国际格式 / 独立 3-3-4 分组），两侧 lookaround 排除 UUID 片段；`catalog()` 跳过 `_`/`.` 开头路径；`/cd` 对已有工作区补 `seed_datasets()`；无基因集资源时规划提示要求向用户索取。
- 验证：`tests/test_privacy.py` 39 项、`test_builtin_datasets.py` 23 项、`test_task_planner_portable.py` 31 项、`test_cd_workspace.py` 全通过；完整回归 **689 passed / 8 skipped / 1 warning**（316.50 s）；`public_release audit` 224 项通过。
- 真实复现确认：同一 selector 提示现通过隐私守卫（fixture key 时载荷到达 transport）；`catalog()` 在本工作区只返回 2 条 examples，无 `_gdc_cache` 条目。
- 保持拦截：`+86 138 0013 8000`、`138-0013-8000`、`contact: 555-123-4567`、`555 123 4567`、`555.123.4567`、`jane.doe@example.org`、`Tel 010-12345678`、`cell 5551234567`、`联系电话：…` 仍全部阻断；其余 8 类模式未改动。
- 未做：未清理 2.0 GB `data/raw/_gdc_cache/`（本地构建缓存，可能仍需复用），只是不再进入模型可见清单；未自动下载 KEGG/MSigDB 基因集。

## 2026-10-28 GSVA 多方法通路评分协议（未发布）

- 范围：可配置触发关键词、本地冻结的四方法 GSVA 协议（`ssgsea`/`plage`/`zscore`/`gsva`，走 `*Param()` 参数对象）、每方法独立 BH 判定、跨方法一致性计数、五张输出表与 Lead-owned 独立校验器；新增 `orchestrator/pathway_scoring.py`、`scripts/32_gsva_multimethod_reference.R`、`tests/test_pathway_scoring.py`、`docs/PATHWAY_SCORING.md`。
- 触发与协议：关键词来自 `analysis.pathway_scoring.trigger`（默认含中英文 9 个词），命中且计划声明评分才冻结协议；命中但计划不评分时只加一条可见限制。阈值来源顺序为请求引用 → 配置 `adjusted_p_max` → 失败（不静默默认）。`-log10FDR` 等图形标注不会被误判为阈值；`FDR 5%` 视为 0.05。
- 新增测试 `tests/test_pathway_scoring.py`：**53 项通过**（离线 50 项 + 真实 R 3 项），真实 R 部分 46.9 s。离线覆盖触发/方法选择/阈值来源/协议冻结/计划闸门/预览披露/校验器逐项篡改拒绝（padj、selected、pvalue、status、methods_tested、consensus、删通路、删样本、改报告字段、改阈值、越界词表、缺协议）；真实 R 覆盖四方法产出五表 → 校验器通过、`32_gsva_multimethod_reference.R` 逐方法重算 `max_abs_error ≤ 1e-8`、篡改得分表被参考脚本拒绝。
- 真实 R 实测（本机 R 4.5.2 + 项目库 `.runtime/r-library` GSVA 2.4.9）：四方法在 40 基因 × 8 样本合成矩阵上全部成功，参考脚本 `max_abs_error`：`ssgsea` 5.0e-16、`plage` 5.0e-16、`zscore` 4.7e-15、`gsva` 5.0e-16；零方差特征 2 个被剔除并记入 `dropped_features`；全零配对差值通路被标 `not_estimable/zero_difference` 且保留。
- 原生验收探针：`native_dist/v8_acceptance.py` 改为依次执行四个参数对象并逐方法记录 `gsva_methods`，实测 `ssgsea/plage/zscore/gsva` 全部 `ok`；保留 `plageParam` 与 `method="plage"` 回退路径。
- 计划 schema 升至 `tui_task_plan_v7`（提示内容变化导致缓存身份变化），同步更新 4 处既有断言。
- 发行审计：`python -m native_dist.public_release audit` **224 项通过**（新增 4 个文件已声明）；未 bump VERSION、未创建 tag。
- 真实 TCGA-LIHC 结构冒烟（非科学结论）：用 `datasets/TCGA-LIHC/expression/counts_paired.rds`（19,938 × 100，50 对）+ `metadata/samples_paired.csv` + 6 个从真实基因符号随机抽取的合成基因集（25/40/60/80/120/200 个成员）跑四方法。零方差特征 670 个被剔除（19,268 × 100 参与评分）；四方法耗时 ssgsea 0.68 s、plage 0.37 s、zscore 0.32 s、gsva 1.52 s；`verify_outputs` 通过（6 基因集 × 4 方法 = 24 行 tested，20 行 selected，n_units=50，not_estimable 0）；`scripts/32_gsva_multimethod_reference.R` 在同一真实矩阵上重算，`max_abs_error` ssgsea 5.6e-16、plage 5.0e-16、zscore 4.4e-14、gsva 5.0e-16。
- 该冒烟暴露的正确行为：6 个随机基因集全部 `selected=TRUE`，但 `direction_consistent=FALSE`，因此 `consensus=0` —— 显著性不等于跨方法方向一致，协议如实区分二者而不是把显著当共识。
- 已知限制：运行期校验器只能证明表内自洽与统计规则正确，**不能**证明得分矩阵由点名方法产生（方法真实性靠合成数据参考脚本）；四种方法量纲不同，故不合并分数；msigdbr 在本机离线不可用，基因集必须由用户提供 GMT/RDS 并经输入探测；无真实模型外发授权，未做真实队列科学结论。

## 2026-10-28 科学性与统计正确性修复（未发布）

- 范围：冻结选择阈值与证据一致性、完整 Evidence Registry 检索与 Critic 反证、Cox 家族内 BH 与不可估计状态、scRNA signature matrix/pseudobulk/参考审计、模型/文献/Wiki 隐私阻断、数值一致性、合成与真实结构 benchmark。
- 完整回归：**618 passed / 8 skipped / 1 warning**，281.95 s。无失败；未通过削弱断言取得绿色。跳过项：6 项模拟去卷积 benchmark 为 `NOT_EVALUABLE`（本机 R 缺 nnls/limma/Seurat，未安装任何包），其余为既有平台条件跳过。
- 真实 R 执行：合成 `06_prognosis.R` 运行通过，输出 `family/padj/selected`，`validate_prognosis_outputs` 独立重算 family BH 通过；合成分离模型被标为 `not_estimable` 而非 `tested`。`tests/test_prognosis_r.py` 9 项、`test_prognosis_validation.py` 32 项通过。
- scRNA：`signature_matrix()` 隔离执行测试通过（union marker 全量 CPM，无 forced-zero；对旧实现可检出 12/18 个 forced-zero 条目）；`validate_scrna_outputs` 26 项通过。完整 05 R 管线因缺 Seurat/presto/nnls/limma 未执行，保持 `NOT_EVALUABLE`。
- 隐私：模型/文献/Wiki 外发阻断测试通过；本地统计任务状态保持 succeeded。
- 科学 benchmark：`scripts/90_scientific_benchmark.py` 合成 guard 用例全部 passed，TCGA-LIHC 真实结构检查通过（100 样本/50 对）；`scientific_validation_status=NOT_EVALUABLE`。真实模型、真实多组学、真实去卷积和人工审阅仍未验收。
- 发行审计：`python -m native_dist.public_release audit` 220 项通过；未 bump VERSION、未创建 tag。
- 已知限制：本机无 Seurat/presto/nnls/limma，05 全流程只能做可解析数学单测和跳过；没有真实模型外发授权；没有真实多组学输入。

## 2026-10-27 v8.3.0 源码交付验证

- VERSION 升至 8.3.0，包含此前未打标签的 GDC 数据改动与科研专家首版。
- 版本、发行和数据专项：78 passed，55 s；public_release audit 209 项与 provenance gate 通过。未执行新的外部模型请求。
- 提交范围以 PUBLIC_FILES.json 为限；源码提交不表示真实模型、真实多组学或跨平台原生验收完成。

## 2026-10-27 科研专家首版（未发布）

- 范围：通用研究上下文与差异/富集证据桥接、Scientific Wiki/Know-How、文献连接器及逐主张引文核验、既有代理科研阶段、`/wiki`、`/research` 和 `/ask` 集成。没有新增数据库服务或统计方法实现；既有 v8.2 数据和发行改动保留。
- 完整回归：**481 passed / 2 skipped / 1 warning**，226 s；跳过项保持平台条件限制，warning 为既有多线程进程 fork 提示。最后增加每次请求/响应审计后，相关模型接口与科研专项 **151 passed**，22 s；与完整回归重叠，不累加。日志和 JUnit 位于 `reports/research_expert_upgrade_20261026/delivery_regression.*`、`delivery_audit_fix.*`。
- 回归在外层执行沙箱之外运行，以允许本机原生输入探测和本地 HTTP 夹具；未关闭产品内部沙箱。正常沙箱中的 HTTP 夹具曾出现 13 个 PermissionError，保留于 `budget_fix.log`。这不替代 Windows/AppContainer 等目标平台验收。
- 早期统一回归 450 passed / 12 failed / 2 skipped：失败涉及新增命令顺序、规划 schema v6、知识快照改变审批哈希以及共享来源夹具；均修正后重新回归。初始数量校验遗漏、审计写入失败状态和同文多引文冲突的失败记录同样保留。
- 科研专项覆盖非 HCC 数据链、独立组学与派生评分、未知方向/数值/错误映射阻断、引文定位、矛盾证据、空假说、Critic 拒绝、预算、并发审查、跨项目隔离、撤回与冻结回查。完整结果行保留，不以模型数字替代统计来源。
- **真实连接器**：PubMed 与 Europe PMC 的公开 `TP53 colorectal cancer` 查询均 complete，共 4 条冻结记录；离线重放 4 条、两个缓存命中。另一次模型生成的 4 条查询中 Europe PMC 报错，PubMed 返回 6 条记录；整体 partial，未把失败隐藏成 complete。
- **真实模型未完成验收**：第一轮查询数超过上限被阻断；第二轮主张/引文 ID 不对应被阻断；冻结材料重放的直接性校验触发纠错，服务端报告消耗 60,176 tokens，超过该次配置预算 50,000 后终止。没有修改这些失败产物或重跑统计。已补检索限额提示/前置校验、明确 claim_id 和 directness 契约、纠错前预算预留以及逐次回答审计；最后一次真实复核被自动审批审查拒绝，未执行。不得把离线通过表述为真实模型端到端通过。
- 预算预留复用前次服务端输入用量与新增提示字节数，保守拒绝预计无足够预算的纠错；实际计费仍以服务端返回为准，不声称可在无服务端 tokenizer 的情况下严格预测所有请求费用。回归复现了上述消耗结构，确认不足预算时仅发出第一请求并保留实际用量。
- `public_release audit` **209 项通过**，provenance gate 通过；内置数据 8 个文件 SHA256 与升级前一致。发行不含用户知识库、查询缓存、凭据或任务结果。
- 科学验收：真实 TCGA-LIHC 输入结构/配对/来源由现有真实数据测试覆盖；尚未完成真实 HCC 的新科研链端到端解释，也没有真实多组学及人工审阅验收，仍为 **NOT_EVALUABLE**。当前真实调用仅使用隔离合成夹具和公开文献。
- 下一次模型复核的具体外发预览、SHA256、端点及预算已准备于审计目录的 `approval_scope.json` 和 `approval_reasoner_request.json`；需要明确外发授权。没有发布或推送本次升级。

## v8.0.0 初始验收明细

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
- v8.1.3 已发布：提交 `7cc5413`，tag `v8.1.3`；主分支 CI（run 39726862397）三平台 success；发布工作流（run 39726865701）validate/publish success。
- v8.1.3 附件：`tme_agent-v8.1.3.zip`（467991 B，sha256 36c27230…）、`manifest-v8.1.3.json`（32164 B，sha256 e6ccfbfe…）、`SHA256SUMS-v8.1.3.txt`（174 B）；下载校验通过；manifest 176 项、ZIP 178 条目、`source_data` 为空。
- v8.1.1 Release 附件：`tme_agent-v8.1.1.zip`（466755 B，sha256 6a77739b…）、`manifest-v8.1.1.json`（32164 B，sha256 73b0e522…）、`SHA256SUMS-v8.1.1.txt`；下载校验通过。

## 当前状态

- 三平台 CI 全绿；Windows 原生 AppContainer 隔离、macOS 原生沙箱与真实模型 API 仍未验收，`docs/VALIDATION.md` 不据此声称原生通过。
- v8.1.3 已发布，三平台 CI 与发布工作流成功；Windows 原生 AppContainer 隔离、macOS 原生沙箱与真实模型 API 仍未验收。
- 命令补全与 `/new` 回欢迎界面为未发布改动（`CHANGELOG.md` 的 Unreleased 段）：新增 `tests/test_command_menu.py` 25 项，白名单 178 项。

## 2026-10-26 内置 TCGA-LIHC 改为 GDC 官方数据（v8.2.0 最终版）

- `scripts/08_fetch_gdc_tcga_lihc.py` 从 GDC REST API 取 424 个 `Gene Expression Quantification` 文件（全部 `access: open`），分 3 批下载 tar.gz 共 **410,544,479 B**；用每批自带的 `MANIFEST.txt` 逐文件校验 md5 与字节数，**424/424 通过**；`--verify-only` 复检同样通过。
- 记录上游身份：`data_release` = Data Release 46.0（2026-08-10）、GDC API 8.5.0、commit `8f7c2a51ab0084b216ad1b62a3fae8b945439c53`、workflow = `STAR - Counts`、`# gene-model: GENCODE v36`。
- 构建（`scripts/07_build_builtin_dataset.R`，R 4.5.2，xz）：counts_full 19938×424 integer **11,515,176 B**；counts_paired 19938×100 **2,742,248 B**；tpm_full 19938×424 numeric **20,290,288 B**；gene_annotation 19938×3 **922,774 B**；samples_full 424×8 **52,130 B**；samples_paired 100×7 **12,628 B**。数据集约 **34 MB**。
- 可复现性：连续两次构建，三个 `.rds` sha256 完全一致（`8b695b12…`、`f1b28cdd…`、`469d2708…`）。
- 数值核对：随机抽 3 个样本（`TCGA-DD-A4NB-01A`、`TCGA-CC-A3M9-01A`、`TCGA-DD-AACI-01A`）与 GDC 原 TSV 逐值比对，counts 与 TPM **全部一致**，且已验证重复符号求和规则生效。
- 结构核对：19,938 个唯一基因符号（来自 19,962 条 protein_coding 行）、无重复行名；TPM 列和 776,405–978,224（protein_coding 口径，非 1e6）；counts 全整数、最大 12,581,910、无负值；`samples_paired` 行序 == `counts_paired` 列序；`samples_full` 行序 == `counts_full` 列序；分组 371 Tumor / 50 Non_Tumor / 3 Recurrent_Tumor，50 对配对；`TCGA-DD-AACA` 保留 3 个样本。
- 样本标识：424 个 16 位 sample barcode，无 28 位 aliquot 后缀泄漏；`patient` 与 barcode 前 12 位一致；每行有 `file_id` 可回查 GDC。
- 真实输入探测（R 4.5.2）：`counts_paired` 与 `counts_full` 均 `numeric_matrix=True`、`finite=True`、`negative_count=0`；`validate_bindings` 接受配对绑定（`pair_key=pair_id`）与全量三水平绑定（`pair_key=null`）。
- `dataset.json`：`provenance_status: recorded`；`source` 含 GDC 仓库、`access_tier: open`、数据版本、commit、上游流程、基因模型、表达列、基因范围、重复符号规则、临床规则；`upstream_files` 424 条含 `file_id`/md5/字节数；6 条质量注记（protein_coding 口径 TPM 列和、24 个重复符号逐个列出、复发瘤无配对、counts 未标准化、16 位 barcode、无临床诊断表）。
- `python -m native_dist.public_release gate --provenance` → **exit 0**（`provenance_pending: []`）；`audit` **190 项通过**、无 provenance 警告。
- 发行 ZIP：`tme_agent-v8.2.0.zip` **35,272,725 B**、192 条目、190 项 manifest；含 8 个 `datasets/TCGA-LIHC/*` 与 `scripts/07`、`scripts/08`；不含 `data/`、`knowledge/`、`analysis/`、`.runtime/`、`.venv/`；`shasum -a 256 -c` 通过。
- 端到端：ZIP 解包 → `installed()` 8.2.0 → `initialize()` 播种 9 个文件（含 6 个数据集文件）→ 二次调用返回 `[]`（幂等）→ 冻结绑定路径在工作区内可解析。
- 回归：完整套件 **228 passed / 13 failed / 2 skipped**；13 个失败与 pristine HEAD 基线完全相同，均为本机 DSH 沙箱内嵌套 `sandbox-exec` 被拒（`sandbox_apply: Operation not permitted`），非本次改动引入。`tests/test_builtin_datasets.py` 21 项、`tests/test_public_distribution.py` 45 项（44 s）全通过。

## 2026-10-26 已废弃：手工 TCGA 表与基于其构建的第一版数据集

用户在 `data/raw/TCGA/` 手工放入的三份表，以及据此构建的第一版 `datasets/TCGA-LIHC/`，已删除并由 GDC 官方下载数据取代。删除前记录哈希以便追溯：

- `data/raw/TCGA/TCGA-LIHC_clinical.csv` `ed26a2b7eb59faa3…`（325858 B）
- `data/raw/TCGA/TCGA-LIHC_mrna_expr_counts.csv` `909039d67143bf27…`（30584678 B）
- `data/raw/TCGA/TCGA-LIHC_mrna_expr_tpm.csv` `ece5e0139881d252…`（54244908 B）
- 第一版 `datasets/TCGA-LIHC/expression/counts_full.rds` `76daf7fbb6033d31…`、`counts_paired.rds` `7dbff29ced02879d…`、`tpm_full.rds` `1b0cb77b542fdd71…`、`metadata/clinical_gdc_full.csv` `ed26a2b7eb59faa3…`、`samples_full.csv` `e1e8563001467ad2…`、`samples_paired.csv` `dae6d2d9958917dd…`

废弃原因（均已实测确认）：

1. **溯源字段缺失**：`dataset.json` 的 `source`/`license` 全为 `not_recorded`，`gate --provenance` 阻塞发版。
2. **重复基因符号合并错误**：旧数据对 24 个重复 `gene_name` 取首行而非求和，`MATR3`/`PDE11A`/`POLR2J3`/`TMSB15B` 在 8,453,712 个单元中造成 560 个错误值（最大差 27 counts）。
3. **临床 `morphology` 列被 Excel 损坏**：424/424 行被写成 `Mar-70`/`Mar-80`/`Mar-74`/`Mar-71`，实际 ICD-O 码为 `8170/3`/`8480/3`/`8074/3`/`8071/3`。
4. **临床字段与 GDC 不一致或缺失**：`ajcc_pathologic_stage` 115/424、`days_to_last_follow_up` 158/424、`prior_treatment` 131/424 不一致；缺 `child_pugh_classification`、`ishak_fibrosis_score`、`residual_disease`、`ajcc_staging_system_edition`。
5. **样本 ID 用 28 位 aliquot barcode**，与 GDC 的 16 位 sample barcode 不一致，且无法从 barcode 直接回溯 GDC 文件。

另需更正一次错误结论：本次调查早期曾判断"旧 TPM 与 GDC STAR 非同源、ALB 差 192 倍"，该判断源于比对脚本的列索引偏移 bug，**不成立**。修正后实测旧 counts/TPM 除上述 4 个重复符号基因外与 GDC 逐值完全一致（排除后 0 差异），即旧数据本身就是 GDC STAR - Counts 数据。

## 2026-10-26 内置 TCGA-LIHC 数据集（v8.2.0，已被上节取代）

- `data/raw/TCGA/` 三份上游表整理为 `datasets/TCGA-LIHC/`：counts_full（19938×424，integer，10339240 B）、counts_paired（19938×100，2467828 B）、tpm_full（19938×424，18638020 B）、samples_full（424×20）、samples_paired（100×12）、clinical_gdc_full（424×63，上游逐字节副本 325858 B）。总计约 31.5 MB。
- 上游原始表未被修改：counts `909039d6…`、tpm `ece5e013…`、clinical `ed26a2b7…`，与构建前一致；`dataset.json.upstream_files` 记录同一组哈希。
- `Rscript --vanilla scripts/07_build_builtin_dataset.R . <built-at>` 连续两次运行，三个 `.rds` 的 sha256 完全一致（xz 写入可复现）；`counts_full` = `76daf7fb…`、`counts_paired` = `7dbff29c…`、`tpm_full` = `1b0cb77b…`。
- 用真机 R 4.5.2 运行 `multiomics_tui/input_probe.R`：三份 RDS 均 `numeric_matrix=True`、`finite=True`、`negative_count=0`、无重复行列名；`counts_paired` 列序与 `samples_paired.sample_id` 完全一致。原始宽表 CSV 实测 `numeric_matrix=False`（首列表头为空导致 R 丢失行名），因此规范格式必须是 RDS。
- `validate_bindings` 用真实探测结果接受三组冻结绑定（`counts_paired` + `pair_key`；`counts_full`/`tpm_full` 三水平 `group_map`、`pair_key=null`）。给全量绑定填 `pair_key` 会被拒绝（`pair_id` 在未配对样本为 NA）。
- 队列构成核实：371 TP / 50 NT / 3 TR；50 位患者 TP+NT 配对；`TCGA-DD-AACA` 有 3 条样本记录（1 TP + 2 TR）。
- `python -m native_dist.public_release prepare --version 8.2.0` 通过；`audit` 189 项通过（`builtin_datasets: ["TCGA-LIHC"]`、`source_data: []`、`provenance_pending: ["TCGA-LIHC"]`）。
- `python -m native_dist.public_release gate --provenance` 返回 `blocked` / exit 1：`dataset.json` 的 `source` 与 `license` 仍为 `not_recorded`，已接入 release workflow 阻止发版。
- 本地 `build`：`tme_agent-v8.2.0.zip` 32,038,301 B（8.1.3 为 467,991 B），189 项 + manifest + sums；ZIP 内 `datasets/TCGA-LIHC/*` 齐备且不含 `data/`、`knowledge/`、`analysis/`、`.runtime/`、`.venv/`。
- 回归：`tests/test_builtin_datasets.py` 18 项；`test_command_menu.py`、`test_cd_workspace.py`、`test_v8_distribution.py` 全通过；`tests/test_public_distribution.py` 45 项通过，发行夹具改为 session 级构建后由 162 s 降至 43 s。
- 未完成：`dataset.json` 的来源字段（GDC 项目/版本、访问级别、下载日期、上游定量流程）待补齐；补齐前不得发布含该数据集的版本。


维护者发布前应复核暂存白名单与 Release 内容；用户只安装 Release 附件 ZIP。私有研究输入、结果、个人配置和未确认再分发许可的参考资源均不随公开包交付；`datasets/` 内的开放获取数据按 `dataset.json` 记录的上游条款分发。
