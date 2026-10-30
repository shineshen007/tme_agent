"""Source-backed reporting; no fabricated literature or biological certainty."""
from pathlib import Path
import pandas as pd
from .io import read_json, write_json


def render_report(run, state):
    run=Path(run)
    validation=read_json(run/'validation/validate_report.json')
    hypotheses=read_json(run/'report/hypotheses.json')
    domains=[state[k] for k in ['rna_result','protein_result','metabolite_result']]
    scrna=state.get('scrna_result')
    agent=state.get('agent_result')
    literature=state.get('literature_result')
    prognosis_status=(state.get('prognosis_result') or {}).get('status','absent')
    scientific_status='blocked' if (agent or {}).get('status')=='blocked' else 'review_only'
    lines=['# HCC 配对多组学分析报告','',
           f'本报告为观察性 bulk 组织分析，科学状态为 **{scientific_status}**。结果用于候选发现，不构成机制、细胞来源或通量验证。','',
           '## 数据与设计','',f'- 159 人 RNA/蛋白，109 人三组学子集；代谢包含 {validation["cohorts"]["metabolomics"]["n_qc"]} 个技术 QC。',
           f'- 肝硬化编码冲突 {validation["clinical_conflicts_resolved"]} 处，按用户指定的 Clinic_159 解决。',
           '- 配对基于用户确认的行序生成显式 patient_id；四个 counts 别名通过独立映射连接，不修改原始文件。',
           '- 代谢峰与候选注释分离；每个峰只进行一次差异检验。','',
           '## 统计产物','', '| 组学 | 队列 | 配对数 | 完整特征数 | 可报告 FDR 数 | 上调 | 下调 |','|---|---|---:|---:|---:|---:|---:|']
    for domain in domains:
        for d in domain['de']:
            lines.append(f'| {domain["omics"]} | {d["cohort_id"]} | {d["n_pairs"]} | {d["n_features"]} | {d["n_tested"]} | {d["up"]} | {d["down"]} |')
        for e in domain['errors']:
            lines.extend(['',f'失败：{domain["omics"]}：{e["message"]}'])
    lines.extend(['','上述计数为冻结阈值下的结果摘要；未显著、过滤和无法检验的特征均保留在 de.csv。',
                  '','## 跨组学证据与候选','',
                  '- `integration/cross_matrix.csv`：相同109人队列的 RNA–蛋白效应，包括同向、反向及缺测。',
                  '- `integration/patient_correlations.csv`：患者配对变化的关联；FDR按预先定义检验族校正，区间方法在每行注明。',
                  '- `integration/axis_coverage.csv` 与 `axis_evidence.csv`：预设轴和已有候选轴的完整覆盖及证据。',
                  '- `integration/open_discovery_pathways.csv`：开放发现的全部 Hallmark 通路，不以预设轴筛选。',
                  '- `integration/metabolomics_batch_sensitivity.csv`：109对主分析与108对同批次配对的逐峰比较。',
                  '- `figures/`：三个组学的配对 PCA，提供 PDF、SVG、source data 和清单。',
                   f'- 当前产生 {len(hypotheses["hypotheses"])} 条满足至少两种测量组学阈值的候选假说；不按证据数量授予机制置信度。',
                   '- Hallmark全排序富集用于开放发现；免疫程序分数不代表细胞比例，剔除候选轴基因后的分数用于关联敏感性分析。'])
    if scrna is not None:
        lines.extend(['','## 外部单细胞图谱背景（review_only）',''])
        if scrna['status'] in {'complete','review_only'}:
            lines.extend([
                f'- 图谱为外部泛癌肿瘤细胞合集（用户确认范围）：{scrna["n_cells"]} 个细胞、{scrna["n_samples"]} 个样本、{scrna["n_celltypes"]} 类主要细胞类型；供者与本队列不同，仅作基因/通路/细胞类型层面背景整合，不构成独立验证。',
                '- `scrna/axis_localization.csv`：轴基因与冻结 bulk 候选基因在各细胞类型的定位；供者（样本）级 pseudobulk 配对 Wilcoxon 为主检验，细胞级 presto 统计仅作描述性支持。',
                '- `scrna/axis_localization_by_source.csv`：轴基因按数据来源×细胞类型的描述性表达，用于透明呈现癌种混杂。',
                '- `scrna/geneset_celltype.csv`：bulk 差异基因集（冻结阈值）在各细胞类型的 ssGSEA 评分与供者级比较（描述性）。',
                '- `scrna/deconvolution/`：NNLS 估计细胞比例（泛癌签名主分析；HCC 来源签名与 top50 marker 为预先声明的敏感性）；配对 T−N limma 检验；为估计值而非实测比例。',
                '- `integration/scrna_axis_celltype.csv` 与 `integration/scrna_evidence_registry.csv`：外部背景证据独立登记（external_context/external_atlas），不进入 bulk 证据登记，也不计入两种测量组学规则。',
                '- `integration/deconvolution_paired_summary.csv`：三个去卷积面板的配对结果对照与方向一致性。',
                '- 免疫程序关联新增 cell_proportion_adjusted 敏感性（协变量为估计免疫比例变化），与肿瘤纯度敏感性并列报告。'])
        else:
            lines.append(f'- scRNA 分支状态：{scrna["status"]}；错误见 scrna/result.json 与 scrna 日志。bulk 证据链不受影响。')
    if (run/'prognosis/statistics.json').exists():
        prog=read_json(run/'prognosis/statistics.json')
        axis_label=(state.get('prognosis_result') or {}).get('axis','catecholamine-3mt')
        families=[str(f) for f in (prog.get('families') or [])]
        lines.extend(['','## 预注册候选轴追踪：预后与 T 细胞相关（review_only）','',
                      f'- 轴：`{axis_label}`（`origin=existing_candidate`）。暴露为**肿瘤组织值**，效应按每 SD 报告；特征面板与阈值在运行前冻结，本分支为候选轴追踪而非发现性筛选。',
                      f'- triple109：OS 事件 {prog.get("n_events_os_triple109")}、RFS 事件 {prog.get("n_events_rfs_triple109")}；Cox 模型 {prog.get("n_cox_rows")} 行（tested {prog.get("n_cox_tested")}，padj 可计算 {prog.get("n_cox_padj")}，FDR 入选 {prog.get("n_cox_selected")}，not-estimable {prog.get("n_not_estimable")} 全部保留）。',
                      f'- 代谢特征的额外灵敏度队列为同批次 {prog.get("n_patients_same_batch108")} 人。',
                      f'- 多重检验：在预先声明的 队列×结局×模型 家族内进行 Benjamini-Hochberg (BH) 校正（{len(families)} 个家族：{", ".join(families)}），冻结阈值 fdr = {prog.get("fdr")}；`selected` 仅当 `status=tested` 且 `padj<=fdr`，not-estimable 行保留且 `padj=NA`。',
                      f'- T 细胞读数：外部图谱 NNLS 估计比例（主面板 pancancer，hcc_reference/top50 为敏感性）与 RNA 同源 ssGSEA 评分（签名状态：{prog.get("signature_status")}）；二者均为估计/同源读数，不是实测细胞比例。',
                      '- `integration/prognosis_association.csv`：全部预后检验（含 not-estimable 行）；直接复用 R 端的 `family` 与家族内 BH `padj`，整合层不重复校正；`ph_p` 为比例风险诊断，仅记录、不切换模型。',
                      '- `integration/tcell_correlation.csv`：特征×T 细胞读数关联，FDR 按 面板×读数类型×调整模式 家族校正；`purity_adjusted` 为预先声明的敏感性。',
                      '- 边界：单队列观察性关联，不建立因果、酶活性、通量或细胞来源；3MT 峰身份仍为歧义；RFS 未使用竞争风险模型。'])
    elif prognosis_status == 'failed':
        lines.extend(['','## 预注册候选轴追踪：预后与 T 细胞相关（review_only）','',
                      '- 分支状态：failed；错误见 `prognosis/result.json` 与 `prognosis/06_prognosis.R.log`。该分支失败不阻断 bulk 证据链，但本运行没有预后/T 细胞证据层。'])
    if literature is not None:
        lines.extend(['','## 文献检索（review_only）','',
                      f'- 状态：{literature.get("status")}；查询数：{literature.get("n_queries", "-")}；'
                      f'合并记录数：{literature.get("n_records", 0)}（冻结基线 {literature.get("n_frozen_base_records", 0)} + 本次 {literature.get("n_new_records", 0)}）。',
                      '- 记录保留 `stance=unreviewed`；语义立场由智能体层单独评估并标注“机器评估·待人工复核”，不构成支持/反对证据。',
                      '- 查询由预设轴与整合候选确定性生成，智能体仅可提出补充查询且由确定性工具执行并记录。'])
    if agent is not None:
        lines.extend(['','## 智能体层（模型生成，非证据）',''])
        if agent.get('status') == 'disabled':
            lines.append('- LLM 已禁用（`llm.enabled=false`）；本运行未生成模型解读。')
        elif agent.get('status') == 'blocked':
            lines.append('- Critic 闭环已阻断；仅保留 `reasoning/` 逐轮草稿和 `report/agent/blocked.json`，未发布最终模型假说或 dossier。')
            for e in agent.get('errors', []):
                lines.append(f'- 失败：{e.get("step")}：{e.get("message")}')
        else:
            lines.extend([
                f'- 模型：`{agent.get("model")}`；状态：{agent.get("status")}；假说数：{agent.get("n_hypotheses",0)}；'
                f'被校验器拒绝的假说数：{agent.get("n_rejected_hypotheses",0)}；'
                f'靶点分组数：{agent.get("n_target_groups",0)}；对抗复核问题数：{agent.get("n_review_issues",0)}'
                f'（其中 blocking：{agent.get("critic_blocking_issues",0)}）。',
                f'- token 用量：{agent.get("tokens_used","-")}；调用数：{agent.get("n_calls","-")}；缓存命中：{agent.get("cache_hits","-")}。',
                '- 文字由 LLM 生成，**所有数值由确定性渲染器从证据注册表注入**；假说维持 `not_independently_validated`。',
                '- 完整报告：`report/agent_report.md`；机器可读：`report/agent/hypotheses_agent.json`、`targets.json`、`literature_stances.json`、`review.json`、`agent_manifest.json`。',
                '- 该层不改变任何统计结果、阈值或证据状态；文献立场在人工复核前不得引用为支持或反对。'])
            for e in agent.get('errors', []):
                lines.append(f'- 失败：{e.get("step")}：{e.get("message")}')
    dossier=state.get('dossier_result') or {}
    dossier_status=dossier.get('status','absent')
    dossier_publish=state.get('dossier_publish') or {}
    if dossier_status!='absent':
        lines.extend(['','## 靶点证据包（review_only，确定性汇总冻结证据）',''])
        if dossier_status=='disabled':
            lines.append('- 已禁用（`deliverables.dossiers.enabled=false` 或未产出靶点）；未生成 `result/`。')
        elif dossier_status=='skipped_no_targets':
            lines.append('- 本次运行未产出靶点（智能体层被禁用或未提出靶点）；未生成 `result/`。')
        else:
            lines.extend([
                f'- 状态：{dossier_status}；靶点数：{dossier.get("n_targets",0)}；'
                f'发布：{dossier_publish.get("status","absent")}'
                f'（新增 {dossier_publish.get("n_published",0)} / 替换 {dossier_publish.get("n_replaced",0)} / '
                f'未变 {dossier_publish.get("n_unchanged",0)} / 失败 {dossier_publish.get("n_failed",0)}）。',
                f'- 发布目录：`{dossier_publish.get("output_dir") or (state.get("dossier_result") or {}).get("output_dir","result")}`。'])
            for item in dossier.get('targets',[]):
                lines.append(f"  - `{item.get('slug')}/`（{item.get('identifier')}，{item.get('status')}）："
                             f"引用证据 {item.get('n_evidence_cited','NA')} 条；跳过面板 {item.get('n_skipped_panels','NA')} 个。")
            lines.append('- 每包含 README、manifest、证据表（含未入选/not-estimable/反向）、证据图与 source data、复现代码；'
                         'K-M 面板仅中位分组描述性展示（无 log-rank 或任何检验）。')
            lines.append('- 证据包不新增统计检验、不排序、不提升验证状态；`validation_status` 恒为 `not_independently_validated`。')
            for item in dossier_publish.get('failures',[]):
                lines.append(f"- 发布失败：{item.get('slug')}：{item.get('error')}")
            for item in dossier.get('errors',[]):
                lines.append(f"- 构建失败：{item.get('target')}：{item.get('message')}")
    lines.extend(['','## 已知限制','',
                  '- 3MT 所在峰有 3-Methoxytyramine 与 (-)-Sympatol 两个候选身份，需标准品、RT和MS/MS核验。',
                  '- MetDNA grade不是MSI等级；蛋白居中方向和代谢上游处理历史尚未完全说明。',
                  '- 代谢检验基于提供矩阵的log2尺度，不追加未经授权的上游批次校正；108个同批次配对作为敏感性分析。',
                  '- RNA使用非负整数counts。原RNA_FPKM表含负值及基因名问题，不进入主统计。',
                  '- 基因映射数据库已固定版本，但原始比对GTF版本未提供；映射歧义记录在审计表。',
                  '- 队列HBsAg均阳性，癌旁不能作为健康肝组织；109人与159人不构成独立验证。',
                  '- 文献检索条目需语义审查；未提供独立队列和功能扰动实验，未执行外部验证。',
                  '- 外部单细胞图谱为泛癌合集且供者不同于本队列；细胞定位与去卷积结果仅为背景/估计，不构成独立验证，也不改变假说的验证状态。',
                  '- 去卷积签名来自图谱 marker 基因；恶性/上皮签名迁移到 HCC bulk 的解释不确定，已用 HCC 来源签名敏感性对照。',
                  '- MOFA2、监督分类/预测模型与外部验证仍属后续阶段；本阶段只报告单队列观察性预后关联，不构成临床可用性结论。',''])
    (run/'report/summary.md').write_text('\n'.join(lines),encoding='utf-8')
    failures={k:v for k,v in state.get('pathway_status',{}).items() if v['status']!='complete'}
    scrna_status=scrna['status'] if scrna is not None else 'absent'
    agent_status=(agent or {}).get('status','absent')
    integration=state.get('integration_status',{}) or {}
    dossier_ok=dossier_status in {'complete','disabled','absent','skipped_no_targets'}
    publish_ok=dossier_publish.get('status','absent') in {'complete','disabled','absent','skipped_no_build','skipped_blocked_reasoning'}
    technical='complete' if all(d['status'] in {'complete','review_only'} for d in domains) and not failures \
        and scrna_status in {'complete','review_only'} \
        and prognosis_status in {'complete','review_only','absent'} \
        and agent_status in {'disabled','review_only','blocked','absent'} \
        and dossier_ok and publish_ok else 'partial'
    status={'technical_status':technical,'scientific_status':scientific_status,'pathway_failures':failures,
            'scrna_context':scrna_status,'prognosis_layer':prognosis_status,
            'n_prognosis_evidence':integration.get('n_prognosis_evidence'),
            'n_tcell_evidence':integration.get('n_tcell_evidence'),
            'dossier_layer':dossier_status,'n_dossiers':dossier.get('n_targets',0),
            'dossier_publish':dossier_publish.get('status','absent'),
            'dossier_publish_failed':dossier_publish.get('n_failed',0),
            'agent_layer':agent_status,
            'agent_critic_blocking_issues':(agent or {}).get('critic_blocking_issues',0),
            'external_validation':'not_performed','functional_validation':'not_performed',
            'llm':('enabled_model_generated_not_evidence' if agent_status not in {'disabled','absent'}
                   else 'disabled_statistics_remain_available')}
    write_json(run/'report/evidence_status.json',status)
    return status
