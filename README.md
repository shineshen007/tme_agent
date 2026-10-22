<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-on-dark.svg">
    <img src="assets/logo-on-light.svg" width="72" height="72" alt="tme_agent logo">
  </picture>
</p>

<h1 align="center">tme_agent</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/shineshen007/tme_agent/stargazers">
    <img src="https://img.shields.io/github/stars/shineshen007/tme_agent?style=flat&logo=github" alt="GitHub stars">
  </a>
  <a href="https://github.com/shineshen007/tme_agent/releases">
    <img src="https://img.shields.io/github/v/release/shineshen007/tme_agent?style=flat&logo=github" alt="GitHub release">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-GPL--3.0--or--later-555?style=flat" alt="License">
  </a>
  <a href="https://www.python.org/">
    <img src="https://img.shields.io/badge/python-3.12%E2%80%933.14-555?style=flat&logo=python&logoColor=white" alt="Python">
  </a>
  <a href="https://www.r-project.org/">
    <img src="https://img.shields.io/badge/R-4.4%2B-555?style=flat&logo=r&logoColor=white" alt="R">
  </a>
</p>

<p align="center">A natural-language multi-omics analysis expert in your terminal.</p>

<p align="center">
  <img src="assets/logo-lockup.svg" alt="tme_agent" width="100%">
</p>

tme_agent turns the data you provide and the research direction you care about into a reviewed,
executable plan. It binds the goal to concrete inputs, proposes a method and code, and waits for
your approval before anything runs. Every task keeps its own log, its complete results, and the
provenance needed to trace each number back to the file it came from.

- **Plan before run:** Every task is bound to declared inputs, a chosen method, and generated
  code that you review and confirm before execution.
- **Hub-and-spoke orchestration:** A planner fans work out to per-omics spokes and integrates
  their evidence; the model interprets, but never authors a statistic.
- **Numbers are injected, not generated:** Every figure and number in a report is read from the
  deterministic evidence registry, not written by a language model.
- **Reproducible by construction:** Task logs, complete result tables, and provenance records are
  kept for every run; nothing is overwritten in a completed run directory.
- **Guardrails in code, not only prompts:** Input binding, sandboxed execution, and refusal of
  cohort identifiers are enforced by the runner, not left to the prompt.
- **Bring your own model:** Configure the service with `/model` and the key with `/key`; the key
  is stored in the system credential vault and never written to config, logs, or Git.
- **Workspaces on demand:** launch from any directory with the installed `multiomics` command;
  `/cd` switches or initializes a workspace without copying the code tree or overwriting files.

## Architecture

The public entry point is a natural-language task. A hub planner produces a plan, per-omics
spokes run under the guarded runner, an integrator freezes the evidence, and the agent layer
interprets it — with all statistics injected from the registry.

```mermaid
flowchart TD
    accTitle: tme_agent hub and spoke orchestration
    accDescr: A natural-language goal becomes a reviewed plan. The hub planner fans work out to per-omics spokes; a guarded runner executes each step; the integrator freezes evidence; the agent layer interprets without authoring numbers.

    goal["🧑 Natural-language goal"] --> planner["Hub planner<br/>reviewed plan"]
    planner --> rna["Spoke · transcriptomics"]
    planner --> prot["Spoke · proteomics"]
    planner --> metab["Spoke · metabolomics"]
    planner --> scrna["Spoke · scRNA"]
    rna --> runner["Guarded runner<br/>sandboxed execution"]
    prot --> runner
    metab --> runner
    scrna --> runner
    runner --> registry["Evidence registry<br/>deterministic numbers"]
    registry --> integrator["Hub integrator<br/>frozen evidence"]
    integrator --> agents["Agent layer<br/>interpretation only"]
    agents --> report["📄 Report · tables · provenance"]

    classDef hub fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef spoke fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef guard fill:#fef9c3,stroke:#ca8a04,stroke-width:2px,color:#713f12
    classDef out fill:#f3e8ff,stroke:#9333ea,stroke-width:2px,color:#4c1d95

    class planner,integrator hub
    class rna,prot,metab,scrna spoke
    class runner,registry guard
    class agents,report out
```

## Agents

Each agent has one job, and the hard rules are enforced in code rather than only in the prompt.

| Agent | Role |
| --- | --- |
| **Hub planner** | Turns a goal into a reviewed plan bound to declared inputs and methods. |
| **Spoke agents** | Per-omics analysis for transcriptomics, proteomics, metabolomics, and scRNA. |
| **Evidence integrator** | Freezes the evidence registry; the only source of numbers. |
| **Analyst** | Reads frozen evidence and produces structured, schema-validated interpretation. |
| **Hypothesis reasoner** | Proposes testable hypotheses with counter-evidence and explicit unknowns. |
| **Critic** | Reviews agent output for overreach, unsupported claims, and schema drift. |
| **Librarian** | Retrieves literature, keeping machine stance separate and review pending. |
| **Reanalysis proposer** | Suggests follow-up analysis without changing frozen thresholds. |
| **Synthesizer** | Renders the final report; every number is injected, never authored. |

## Getting Started

### Prerequisites

- **Python 3.12–3.14** and **R 4.4+**. Existing software is reused; dependencies are only
  filled in up to the project environment.

### Install

Download `tme_agent-vX.Y.Z.zip` and its matching checksum list from
[Releases](https://github.com/shineshen007/tme_agent/releases), verify the SHA256, and extract
into a new, writable directory. Use the **Release asset ZIP** — the GitHub-generated
“Source code” ZIP is maintainer source, has no install manifest, and cannot be used for release
updates.

On macOS/Windows you can use `setup_macos.command` / `setup_windows.cmd`, or run from the project
directory:

```bash
python -m native_dist.bootstrap check
python -m native_dist.bootstrap setup
```

After setup, the installer also creates a user-level `multiomics` command
(`~/.local/bin/multiomics`, or `%USERPROFILE%\.local\bin\multiomics.cmd` on Windows) and adds
that directory to your user PATH. Open a new terminal, then run from any directory:

```bash
multiomics
multiomics --project-root /path/to/workspace
multiomics update --check
```

`multiomics` starts in the install workspace. Inside the interface, `/cd <directory>` switches
the working workspace for the current session; `/cd` shows the current path. A directory without
configuration is initialized from the installed template without overwriting existing files.
`/cd` does not change the parent shell directory and is not remembered across restarts.

The launcher uses the project `.venv`; you do not need to activate it. For a manual start, or if
PATH activation was skipped (`setup --no-path`), activate `.venv` (macOS/Linux:
`source .venv/bin/activate`; Windows PowerShell: `.venv\Scripts\Activate.ps1`, or use
`.venv\Scripts\python.exe` directly). Below, `python` means the `.venv` Python in the install
directory.

```bash
python -m multiomics_tui
```

To inspect or remove the launcher and its managed PATH entry:

```bash
python -m native_dist.command_link status
python -m native_dist.command_link uninstall
```

The first install generates local configuration and a 4 × 4 synthetic expression example. The
dependency check looks for compatible user-installed interpreters and packages first (user
site, pyenv/uv/conda, `R_LIBS_USER`) and only fills missing dependencies into the project
environment. Put
your real data under local `data/raw/`, `data/metadata/`, and `data/signature/` — do not upload
these directories. Set the service with `/model` and enter the key in the masked field with
`/key`; the key uses the system credential vault. The `/model` panel asks only for the service,
endpoint, model, authentication, and key; timeout, output limit, JSON mode, token field, and
temperature stay in local `config/api.local.yaml` (default output limit: 32000 tokens). On Linux without a desktop credential service,
you may explicitly set local config `distribution.credential_storage` to `memory`, where the key
lives only for the current process and is never written to disk. **Diagnostics run offline even
without a model key.**

Natural-language tasks are the public entry point. Historical fixed study pipelines need a
separate cohort configuration and cannot be restored from the public template. Model-generated
explanations and code are **not** independent validation — confirm the statistical unit,
grouping, and study design before you rely on a result.

## Update and Rollback

Close the interface and finish or stop background tasks, then run from any directory:

```bash
multiomics update --check
multiomics update
multiomics update --version vX.Y.Z
multiomics update --rollback
```

The same commands work as `python -m multiomics_tui update ...` from the install directory.
Updates default to the latest stable Release. The updater verifies the download, file manifest,
and task locks; it refuses to overwrite locally modified source. Data, results, personal config,
and the existing environment are never replaced by the release package. If applying code or
preparing the environment fails, the release source is restored; new dependencies are not
automatically uninstalled. Success is reported only when code, environment, and diagnostics all
pass. Restart the interface when done.

## Docs and Status

- [Install, update, and rollback](docs/INSTALL_UPDATE_zh.md)
- [Third-party resources and local data](docs/THIRD_PARTY_RESOURCES.md)
- [Source provenance and baseline](docs/V8_SOURCE.json)
- [Local validation results](docs/VALIDATION.md)
- [Current delivery status](CURRENT_STATE.md)
- [Changelog](CHANGELOG.md)
- [License notice](docs/LICENSE_NOTICE.md)

Windows, Intel Mac, and Apple Silicon native isolation still require on-device testing; portable
CI does not substitute for native acceptance. `python -m native_dist.diagnostics --project-root .`
runs only local checks and synthetic tasks, and does not call a real model API.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
