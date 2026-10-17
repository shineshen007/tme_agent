# Project context

- Repository: shineshen007/tme_agent; license: GPL-3.0-or-later.
- Public baseline: 20261015_v8 ZIP; first public release: 8.0.0. Exact imported hashes are in docs/V8_SOURCE.json.
- Supported entry: natural-language tasks with reviewed plans and guarded execution. Historical cohort pipelines need separate study-specific configuration and are not a public entry point.
- User inputs live under local data/; personal configuration, environment, tasks and results stay outside the release manifest.
- Public source and ZIP are built from PUBLIC_FILES.json. VERSION is the sole release version. Release assets include the full corresponding project source; no Python/R binaries or private reference resources are bundled.
- End users install the Release asset ZIP and update through the Python CLI. Maintainers use Git and version tags; a Git working tree refuses source overlays.
- Offline acceptance uses synthetic data and deterministic mock models. Platform-native sandbox, R and credential behavior require actual target-machine checks.
