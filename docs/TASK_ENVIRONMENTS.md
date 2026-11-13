# Task-local dependency installation

Approval of a new `method_evidence_v2` plan authorizes preparation of its declared
packages and required dependencies. Missing local packages no longer imply that
method selection must stop: source evidence can be verified while installed API
verification remains explicitly pending. Plans with existing verified APIs retain
the `method_evidence_v1` validation path.

## Workflow

1. Inspect inputs and clarify the design. Retrieve official software/method sources.
2. Display the method, package versions, sources and pending API checks. Freeze
   `environment_spec` within the complete plan approval hash.
3. After approval, resolve dependencies, download artifacts and prepare a private
   environment under `.runtime/task_environments/`.
4. Install with network access denied to package hooks. Verify dependencies and
   installed API signatures/documentation before generating analysis code.
5. Bind probes, code preflight and execution to the same environment. Check its
   files, locks and interpreters before execution and final reporting.

Generated analysis code cannot install packages. Shared libraries, the controller's
Python environment, raw inputs and historical tasks are not installation targets.
The report includes the actual environment, packages and source commitments.

## Sources and package managers

- Python uses an existing compatible uv 0.9–0.13 executable, copied into the
  task's tool directory with its hash recorded. Otherwise it bootstraps the pinned
  uv 0.13.7 wheel from PyPI metadata and verifies the official SHA256.
- PyPI requirements are resolved to exact versions with hashes. Registry packages
  currently require compatible wheels; no online source build is permitted.
- R uses pinned pak 0.11.2, bootstrapped from the current platform's official CRAN package
  artifact. pak resolves required dependencies and creates a lockfile; downloaded
  packages are installed offline in the task library. System requirements are
  not installed automatically. The pak macOS binary fallback `mac.cran.dev`
  is included, as recorded by the [R-universe build configuration](https://github.com/r-universe-org/actions/blob/v12/Rprofile).
  Folded whitespace around registry SHA256 values is normalized; the original
  resolver lock remains available alongside the installation lock.
- Public GitHub author repositories require a link from official registry
  metadata, its registered homepage/documentation, or a retrieved methodology
  passage. The source is frozen to a complete commit and archive hash. An arbitrary
  model-supplied repository URL is insufficient.
- Author Python packages currently require explicit PEP 621 name/version and
  static runtime/build dependencies in `pyproject.toml`. R authors require a
  matching `DESCRIPTION`. Undeclared remote dependencies, dynamic Python runtime
  dependency metadata and missing build tools remain explicit failures.

## Isolation and recovery

macOS uses the native Seatbelt profile; installers may signal only themselves
and their own child processes for package-manager worker cleanup; Windows uses AppContainer with the task
runtime explicitly bound. Linux installation additionally requires an existing
`bwrap` executable: read-only runtime mounts and private writable staging permit
installer metadata operations without changing the analysis guard's syscall rules.
There is no unrestricted installation fallback or automatic system-tool install.

The interface reports environment preparation stages. `/retry` on an environment
failure retries the unchanged approved plan; it does not invoke the planner again.
Immutable artifact downloads support bounded retries and partial-download resume.
A new attempt preserves failed/interrupted staging directories. A complete,
unchanged environment is reused; concurrent preparation uses a kernel file lease.
Offline evidence mode only permits reuse of a matching verified environment and
will not initiate installation downloads.

A receipt is published after API verification and linked to the task plan hash in
the session. Changed dependencies, source identities or scientific choices require
a new plan. Successful package/API checks are engineering evidence, not scientific
or clinical acceptance. Native acceptance results must be recorded per platform.
