# Project context

- Repository: shineshen007/tme_agent; license: GPL-3.0-or-later for code only (see docs/LICENSE_NOTICE.md).
- Public baseline: 20261015_v8 ZIP; first public release: 8.0.0. Exact imported hashes are in docs/V8_SOURCE.json.
- Supported entry: natural-language tasks with reviewed plans and guarded execution. Historical cohort pipelines need separate study-specific configuration and are not a public entry point.
- User inputs live under local data/; personal configuration, environment, tasks and results stay outside the release manifest.
- Public source and ZIP are built from PUBLIC_FILES.json. VERSION is the sole release version. Release assets include the full corresponding project source; no Python/R binaries or private reference resources are bundled.
- Built-in open-access data ships under datasets/<id>/ and is declared in the release manifest's builtin_datasets. It is seeded into each workspace under data/raw/<id>/ and data/metadata/<id>/ without overwriting existing files. Private cohort inputs never ship, which is what source_data: [] still guarantees.
- A shipped dataset must record its upstream source and data terms in dataset.json (provenance_status). While that field is not "recorded", python -m native_dist.public_release gate --provenance blocks publishing. The current TCGA-LIHC dataset is recorded (GDC open-access tier, Data Release 46.0, STAR - Counts, GENCODE v36) and its per-file md5s are in the manifest.
- End users install the Release asset ZIP and update through the Python CLI. Maintainers use Git and version tags; a Git working tree refuses source overlays.
- Offline acceptance uses synthetic data and deterministic mock models. Platform-native sandbox, R and credential behavior require actual target-machine checks.
- Research-enabled plans freeze explicit ResearchContext and evidence-output column mappings within the existing approval hash. TaskResearchAdapter reads verified results into EvidenceRecord v3; the legacy v2 adapter remains read-only. Differential and pathway-enrichment evidence are separate types.
- Public research continuation reuses AgentReasoningSession and the existing Analyst/Librarian/Integrator/Reasoner/Critic topology. It runs after statistical success, saves immutable attempts and separates analysis, research and scientific status. Critic analysis requests require a new approved task.
- Scientific Wiki uses project knowledge/wiki/knowledge.sqlite3 plus an explicitly published local curated shared library. Knowledge candidates require content-bound human review; publication is a separate confirmation. SQLite is authoritative, JSON freezes versions, Markdown is a reading view.
- statistical_fact, literature_report, biological_inference and hypothesis retain separate evidence grades after review. Reports are derived sources, not independent primary evidence; failed/negative experiments require actual experiment records. Empty hypotheses are a valid outcome.
- Trusted PubMed/Europe PMC connectors preserve retrieval state and frozen passages. Exact quotes and claim/source relations are checked; semantic validity still requires scientific review. Generated code retains existing network and filesystem restrictions.
- New research-engineering tests do not validate real multi-omics inference or experimental success. TCGA-LIHC is the initial real single-omics input; independent multi-omics/manual scientific acceptance remains NOT_EVALUABLE until documented.

- Version 8.3.0 consolidates the research-expert workflow and previously untagged GDC dataset work. Scientific acceptance remains separate from source/version delivery; live-model and real multi-omics acceptance are pending.
