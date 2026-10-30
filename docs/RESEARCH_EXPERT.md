# 多组学科研专家工作流

这是通用科研入口的第一版实现。疾病、物种、组织、独立统计单位和比较方向来自已审批计划，不能由文件名或默认 HCC 队列补全。支持差异结果和独立的通路富集证据类型；其他结果需增加明确的适配器后才能进入统计证据注册表。

## 从分析到科研解释

1. 描述数据和研究问题；规划器先读取输入结构、检索已审查 Wiki，生成分析计划。
2. 检查计划中的研究背景、比较方向和结果列映射。科研任务应有 `research.enabled: true`；文件整理任务无需开启。
3. 审批哈希同时绑定输入、步骤、科研背景、证据映射和规划知识快照。执行仍使用原沙箱与输出合约。
4. 统计完成后核验输入、脚本和所有声明产物的哈希，读取真实结果行，自动串联 Analyst、Librarian、Integrator、Reasoner、Critic。
5. 查看 `analysis/<task-id>/research/attempt_<id>/research_report.md`。完整记录、查询、文献段落、模型调用和迭代结果位于同目录。
6. 用 `/wiki pending` 审查候选知识。未审候选不进入默认检索；批准不改变原有证据等级。

每条统计证据保留原文件、SHA256、行号、行键和列映射。模型只获得定性观察，报告中的效应、FDR、n 从冻结表注入；缺失值保持未知。负结果与不可估计行保留。RNA 派生评分、富集结果不能作为第二种实测组学，跨组学同向也不自动成为因果、酶活性或代谢通量证据。

证据映射可声明冻结的 `selection` 阈值（`adjusted_p_max`，可选 `effect_min/effect_max/analysis_unit_count`）。新计划若绑定 `selected` 列必须同时声明阈值，且数值必须来自用户或配置参数；适配器逐行重算 `padj/效应/status/n` 与 `selected` 的一致性，不一致直接阻断。没有冻结阈值的旧计划仍可加载，但 `selection.status=not_verifiable`、`observation.selected=null`，不能作为确定性显著性证据。

完整 Evidence Registry 不因 120 条展示上限而丢失。Analyst/Integrator/Reasoner 获得按问题与已引用实体检索的完整相关行；Critic 额外获得每个主张的确定性反证与阴性/不可估计候选，并必须逐条列出 `evidence_review`。遗漏候选、未审阴性结果或 `accept` 时仍有 `concern/insufficient` 都会阻断。报告保存反证审查表，便于人工回查。

文献关系按具体主张记录 `supports/contradicts/unclear` 和 `direct/indirect/unknown`。引文必须在冻结来源中真实存在；Critic 对同一主张及来源复核上下文。语义判断仍是模型审查，需要科研人员复核，不能由字符串匹配证明论文支持机制。创新性限定在已记录检索范围内；失败、零结果和缺少直接证据不等于“首次发现”。

证据不足时可以完成解释并返回空假说集合。实验设计包含模型选择依据、扰动、必要对照、救援或正交验证、主要读数、支持与推翻标准及未知参数。缺少效应量和变异依据时，应提出 pilot 或功效分析需求，不生成样本量。

## 状态与恢复

| 状态 | 含义 |
|---|---|
| `status=succeeded` | 统计执行和产物核验完成 |
| `research_status=complete` | 科研流程和 Critic 完成，文献检索无已记录错误 |
| `research_status=partial` | 解释保留，但文献、知识候选、某阶段或新增分析需求未完成 |
| `research_status=blocked` | 科研校验、模型调用或 Critic 阻断 |
| `scientific_status=EXPLORATORY` | 形成探索性解释，不代表实验验证成立 |
| `scientific_status=NOT_EVALUABLE` | 本次无法完成科研评价 |

`/research <task-id>` 为已审批且统计已完成的科研任务建立新 attempt，不重新运行统计。重试前恢复该任务原模型服务、端点、模型和认证方式；每次重试重新检索并保留新版本。Critic 的新增分析只写入 `analysis_requests.json`，必须建立新的审批计划后执行。

只校验历史材料、不联网、不调用模型：

```sh
python -m orchestrator.research_pipeline /absolute/workspace task_id \
  --verify /absolute/workspace/analysis/task_id/research/attempt_id
```

校验包括任务回执中的 manifest 哈希、产物覆盖、各冻结文件和原始分析来源。保留整个工作区及任务会话才能回查来源。`code_snapshot.json` 保存当时源码与 schema；提示、模型配置和调用审计分别保存。校验历史产物不代表重新执行相同模型必然生成相同文本。

## Scientific Wiki 与 Know-How

项目库在 `knowledge/wiki/knowledge.sqlite3`。共享库只接收人工确认发布的条目，默认路径：macOS `~/Library/Application Support/tme_agent/wiki`，Linux `$XDG_DATA_HOME/tme_agent/wiki`（缺省 `~/.local/share`），Windows `%LOCALAPPDATA%/tme_agent/wiki`。可在项目配置 `knowledge.shared_enabled: false` 关闭共享检索，或设置 `knowledge.shared_root` 指定目录。

```text
/wiki search 细胞组成
/wiki show <entry-id> [revision]
/wiki pending
/wiki import knowledge/resources/curated.json
/wiki review <proposal-id|all> [approve|reject]
/wiki retract <entry-id>
/wiki publish <entry-id>
/wiki export knowledge/exports/reviewed_v1 [关键词]
```

写操作先显示具体内容与哈希，使用应用的确认操作执行。导入、批准、共享发布各自独立；文件或候选版本改变后旧确认失效。撤回排除当前检索并撤回本项目已发布版本，历史任务使用的快照保留。导出写入新目录，不能覆盖既有快照。`/ask` 复用同一已审查知识检索服务。

导入 JSON 可以是单条对象、条目数组或 `{"entries": [...]}`，一次最多 50 条、2 MiB。条目字段：

| 字段 | 契约 |
|---|---|
| `title`, `statement` | 标题与有边界的陈述 |
| `kind` | `statistical_fact`, `literature_report`, `biological_inference`, `hypothesis`, `know_how`, `experience` |
| `context` | 疾病、物种、组织及适用范围；实验经验可记录前置条件、失败和限制 |
| `sources` | 来源 ID、类型、定位、SHA256、是否主要来源、原文与准确引文 |
| `entities` | 命名空间 ID、独立类型、名称与可选同义词；gene/protein/cell_type/region/variant 使用 `namespace:taxon:accession`，如 `ncbigene:9606:7157`；物种未明确时显式写 `unknown` |
| `relations` | subject、predicate、object 和 source_ids；支持反向关联 |
| `shareable` | 默认 false；发布前还必须符合去标识与来源约束 |
| `entry_id`, `base_sha256` | 修订已有条目时同时提供，防止覆盖并发更新 |

主要文献/数据库来源须保存原文、准确引文和文本哈希；文献须注明 `metadata.content_scope`。统计事实须有冻结产物来源，实验经验须有实际实验记录来源。生成报告只能作为派生来源，不能用自己的陈述循环证明自己。基因与蛋白分开建模，同义词歧义不合并；候选代谢物不能当作确定鉴定。原始实验失败、阴性结果和否决条件可作为有来源的经验导入；自动候选来自已通过 Critic 的解释/假说，未通过的草稿保留在 attempt 中供人工审阅。

Know-How 使用相同版本和来源机制，在实验设计前检索。`kind: know_how` 的 context 必须包含 `applicability`、`prerequisites`、`method_basis`、`common_failures`、`limitations` 五个非空文本字段，缺失知识应明确写 unknown，并保留来源。第一版不预装未经许可或审查的完整生物医学数据库；新工作区的 Wiki 可能为空，需要精选来源导入或由任务积累候选。

## 文献连接器与配置

可信连接器调用 PubMed 和 Europe PMC；生成分析代码仍不能联网。默认保存全文摘要、按相关段落选取上下文。`fetch_full_text: true` 时仅尝试 Europe PMC 可获得的开放全文。查询、时间、结果范围、失败、重试、摘要/全文状态和文本哈希均记录，文献中的指令不提供工具授权。

`config/study_config.yaml` 的可选设置：

```yaml
knowledge:
  shared_enabled: true
research:
  literature:
    providers: [pubmed, europepmc]
    offline: false
    refresh: false
    fetch_full_text: false
    max_queries: 8
    max_records: 10
    timeout: 25
    retries: 2
```

缓存位于 `.cache/research_literature/`。`offline: true` 只使用缓存；缓存缺失会显式降级。`refresh: true` 重新请求并建立新任务材料，不修改已冻结 attempt。请勿把患者/样本标识写入研究问题、Wiki 正文或检索词；绑定的样本/患者 ID 在模型和检索出口阻断。

外发隐私检查覆盖模型请求、文献查询和 Wiki 共享：已绑定的患者/样本标识、TCGA barcode、MRN/住院号、姓名/出生日期/电话/邮箱/地址以及绑定元数据中的临床自由文本，都会在发出前触发阻断。模型请求抛 `PrivacyViolation`，文献查询拒绝构造查询，Wiki 发布抛 `KnowledgeError`。阻断只影响该外发操作，本地统计和冻结产物不回滚、不重算；无法读取的非 CSV/TSV 元数据保留“需本地隐私复核”的限制，不会假装已检查。

电话识别要求“电话语义”：带 `phone/tel/手机/联系电话` 等标签、或以 `+` 开头的国际格式、或独立出现的 3-3-4/3-4-4 分组。UUID（如 `1e1587e8-8938-4578-8529-0831573a9fe1`）、日期区间、DOI、版本号和 Ensembl ID 不构成电话，不得因此阻断外发。文件清单同样不暴露下载/构建缓存：`catalog()` 跳过任一路径段以 `_` 或 `.` 开头的条目，`data/raw/_gdc_cache/` 这类中间产物因此既不会挤占模型可见输入，也不会把不透明标识带进提示。

## 实现与验收边界

复用现有 Evidence Graph、AgentReasoningSession、LangGraph 阶段拓扑、审计客户端和安全执行层。SQLite、标准库 HTTP 和现有 jsonschema 足以支撑第一版，无新增数据库服务。参考 Biomni 的资源选择与 Know-How、llm-wiki-skill 的来源分层和增量关联、ToolUniverse 的连接器契约、BioChatter 的统一上下文、BioCypher 的类型与命名空间；未复制这些项目代码。

离线夹具检验工程边界；内置 TCGA-LIHC 是真实单组学输入，不能用来证明多组学机制推理质量。真实模型、真实多组学案例、人工审阅、目标平台原生隔离的验证结果以 [VALIDATION.md](VALIDATION.md) 为准。`scripts/90_scientific_benchmark.py` 的合成 guard 用例和真实数据结构检查见 [BENCHMARKS.md](BENCHMARKS.md)；其 `scientific_validation_status` 恒为 `NOT_EVALUABLE`。任何工程通过均不改变尚未完成的科学验收 `NOT_EVALUABLE`。

发行白名单仅包含代码、测试、文档与明确声明的内置开放数据；用户 Wiki、文献缓存、凭据和任务结果不随发行包交付。
