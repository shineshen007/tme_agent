"""Generate and run one approved task in a restricted host subprocess."""
from __future__ import annotations

import ast
import csv
import hashlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import shutil
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "multiomics_tui"

from orchestrator.llm_client import LLMNonRetryableValidationError

from . import artifacts
from .jobs import _atomic_json
from .llm import build_client, key_env_name
from .task_guard import available as guard_available
from .actions import rscript_path
from .task_planner import CODEGEN, CODEGEN_STEP, validate_plan
from .task_sessions import TaskStore, canonical_hash, utc

class GeneratedCodeError(ValueError):
    """Statically proven invalid generated code, eligible for bounded correction."""


class MissingDependencyError(ImportError, GeneratedCodeError):
    """Generated code needs a package that is not installed.

    Eligible for bounded correction: the model is asked to rewrite the step
    using base R or the already-installed packages, rather than the task being
    aborted. Never eligible for installation of new packages. It subclasses
    ImportError so failure records still classify it as a dependency problem.
    """


LOG = logging.getLogger(__name__)
FORBIDDEN_CODE = re.compile(
    r"\b(?:subprocess|os\.system|os\.popen|shutil\.rmtree|requests|urllib\.request|"
    r"socket|install\.packages|file\.remove|unlink\s*\(|system\s*\(|system2\s*\(|"
    r"pip\s+install|rm\s+-|curl\s+|wget\s+)\b", re.I)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_new(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_NOFOLLOW 在 Windows 上不存在；O_EXCL 已保证不覆盖、不跟随已存在的路径。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def _check_code(root: Path, path: Path, language: str, env: dict | None = None, dependencies=True) -> None:
    content = path.read_text(encoding="utf-8")
    if FORBIDDEN_CODE.search(content):
        raise LLMNonRetryableValidationError(f"Forbidden code pattern in {path.name}")
    if language == "python":
        tree = ast.parse(content, filename=str(path))
        if not dependencies: return
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
        missing = sorted(name for name in modules if importlib.util.find_spec(name) is None)
        if missing:
            raise MissingDependencyError(
                f"缺少 Python 分析包 / Missing Python dependencies in {path.name}: {', '.join(missing)}；"
                "请仅使用已安装的包或标准库重写该步骤，不要安装软件包。")
    else:
        rscript = Path(shutil.which(rscript_path(root)) or rscript_path(root))
        if not rscript.is_file():
            raise RuntimeError("Rscript is not installed")
        result = subprocess.run([str(rscript), "--vanilla", str(Path(__file__).with_name("r_preflight.R")), str(path)],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 4:
            raise MissingDependencyError(f"缺少 R 分析包 / Missing R dependencies in {path.name}: {result.stderr[-1500:]}；"
                                         "请改用 base R 或已安装的包重写该步骤；任务不会自动安装软件包。")
        if result.returncode == 3:
            raise GeneratedCodeError(result.stderr[-1500:])
        if result.returncode == 2:
            raise SyntaxError(result.stderr[-1500:])
        if result.returncode:
            raise RuntimeError("R preflight process failed: " + result.stderr[-1500:])
        if not dependencies: return
        packages = sorted(set(
            re.findall(r"\b(?:library|require|requireNamespace)\s*\(\s*['\"]?([A-Za-z][A-Za-z0-9.]*)", content) +
            re.findall(r"\b([A-Za-z][A-Za-z0-9.]*):::{0,1}", content)))
        if packages:
            expression = "packages <- commandArgs(TRUE); missing <- packages[!vapply(packages, requireNamespace, logical(1), quietly=TRUE)]; if (length(missing)) { cat(paste(missing, collapse=',')); quit(status=2) }"
            check = subprocess.run([str(rscript), "--vanilla", "-e", expression, *packages],
                                   cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False)
            if check.returncode:
                raise MissingDependencyError(f"缺少 R 分析包 / Missing R dependencies in {path.name}: {check.stdout.strip() or check.stderr[-500:]}；"
                                             "请改用 base R 或已安装的包重写该步骤；任务不会自动安装软件包。")


def _step_environment(root: Path, task_dir: Path, cfg: dict, inputs) -> dict:
    """Restricted step environment; R must use the configured libraries, not HOME-derived ones."""
    # LOCALAPPDATA is consumed only by the trusted Windows guard to restrict its
    # new profile. run_appcontainer filters it out of the generated script env.
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "LOCALAPPDATA"}}
    from .temp_paths import task_tmp
    env.update(PROJECT_ROOT=str(root), TASK_DIR=str(task_dir), TMPDIR=str(task_tmp(root, task_dir)),
               HOME=str(task_dir), PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="2",
               OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2")
    font_cache = task_dir / "tmp" / ".cache"
    font_cache.mkdir(parents=True, exist_ok=True)
    env["XDG_CACHE_HOME"] = str(font_cache)
    read_paths = [str((root / item["path"]).resolve()) for item in inputs]
    # A reused venv can point at a real interpreter outside the project. Grant
    # only that trusted installation, otherwise exec of its symlink is denied.
    read_paths.append(str(Path(sys.base_prefix).resolve()))
    if (root / ".venv").is_dir():
        read_paths.append(str((root / ".venv").resolve()))
    # An installed app runs a real private interpreter outside the workspace.
    if os.environ.get('MULTIOMICS_APP_ROOT'):
        read_paths.append(str(Path(sys.prefix).resolve()))
    r_home = cfg.get('runtime', {}).get('r_home')
    if r_home:
        env['R_HOME'] = str(Path(r_home).resolve())
        # Unix Rscript selects its executable using RHOME (without underscore).
        env['RHOME'] = env['R_HOME']
        read_paths.append(env['R_HOME'])
    for key in ("R_LIBS", "R_LIBS_USER", "R_LIBS_SITE"):
        if key in os.environ:
            env[key] = os.environ[key]
            read_paths.extend(part for part in os.environ[key].split(os.pathsep) if part and Path(part).is_dir())
    libraries = [str(Path(part).resolve()) for part in cfg.get("runtime", {}).get("r_library_paths", [])
                 if Path(part).is_dir()]
    if libraries:
        env["R_LIBS_USER"] = os.pathsep.join(libraries)
    read_paths.extend(libraries)
    python_libraries = [str(Path(p).resolve()) for p in cfg.get('runtime', {}).get('python_library_paths', [])
                        if Path(p).is_dir() and Path(p).name in {'site-packages','dist-packages'}]
    read_paths.extend(python_libraries)
    env["TASK_ALLOWED_READS"] = json.dumps(sorted(set(read_paths)))
    # Seatbelt treats mapping executable library pages separately from reads.
    # Inputs are intentionally absent; only trusted runtime libraries qualify.
    executable_reads = [str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve()), *libraries, *python_libraries]
    if r_home:
        executable_reads.append(env['R_HOME'])
    env['TASK_RUNTIME_READS'] = json.dumps(sorted(set(executable_reads)))
    if sys.platform == "darwin":
        from .macos_guard import snapshot_runtimes
        # Keep lexical paths separately from canonical read/map targets. Inputs
        # never enter this channel; only configured libraries and interpreters do.
        runtime_paths = [sys.prefix, sys.base_prefix, sys.executable, rscript_path(root)]
        runtime_paths.extend(p for p in cfg.get("runtime", {}).get("r_library_paths", []) if Path(p).is_dir())
        runtime_paths.extend(p for p in cfg.get("runtime", {}).get("python_library_paths", [])
                             if Path(p).is_dir() and Path(p).name in {"site-packages", "dist-packages"})
        runtime_paths.extend(part for key in ("R_LIBS", "R_LIBS_USER", "R_LIBS_SITE")
                             for part in os.environ.get(key, "").split(os.pathsep) if part and Path(part).is_dir())
        if r_home:
            runtime_paths.append(r_home)
        env['TASK_RUNTIME_PATHS'] = json.dumps(snapshot_runtimes(runtime_paths))
    if os.name == "nt":
        view = root / ".runtime/inputs" / task_dir.name
        view.mkdir(parents=True, exist_ok=False)
        for item in inputs:
            source = root / item["path"]
            dest = view / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            if file_hash(dest) != item["sha256"]:
                raise ValueError(f"Input snapshot hash mismatch: {item['path']}")
        # The trusted guard initializes per-user Windows APIs using the real host
        # profile. It replaces this with TASK_DIR before starting generated code.
        env.update(PROJECT_ROOT=str(view), TEMP=env["TMPDIR"], TMP=env["TMPDIR"],
                   MULTIOMICS_WORKSPACE_ROOT=str(root.resolve()))
        for name in ('USERPROFILE', 'APPDATA', 'HOMEDRIVE', 'HOMEPATH'):
            if name in os.environ:
                env[name] = os.environ[name]
        env["TASK_ALLOWED_READS"] = json.dumps([str(view)])
    return env


def _validate_step_reply(parsed, step):
    files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise ValueError("Generate exactly one script for the current step")
    if files[0].get("step_id") != step["id"]:
        raise ValueError(f"Generated step ID must be {step['id']}")
    content = files[0].get("content")
    if not isinstance(content, str) or not 10 <= len(content) <= 120000:
        raise ValueError("Generated script is empty or too large")
    contracts = parsed.get("output_contracts")
    if not isinstance(contracts, list) or len(contracts) != len(step["outputs"]):
        raise ValueError("Provide one output contract for every declared current-step output")
    paths = []
    for item in contracts:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or
                not isinstance(item.get("structure"), str) or not item["structure"].strip() or
                len(item["structure"]) > 4000):
            raise ValueError("Output contracts require a path and a concise nonempty structure")
        paths.append(item["path"])
    if len(set(paths)) != len(paths) or set(paths) != set(step["outputs"]):
        raise ValueError("Output contract paths must exactly match the declared current-step outputs")


def _codegen(client, plan: dict, on_step=None, validate_code=None, on_generation=None, cfg=None) -> dict:
    if len(plan["steps"]) > 1 or plan.get("input_bindings"):
        system = CODEGEN_STEP.replace("{plan}", json.dumps(plan, ensure_ascii=False))
        if cfg is not None:
            from .r_env import availability_note
            note = availability_note(cfg)
            if note:
                system += ("\n" + note + "\nIf the requested method needs a package outside this list "
                           "(for example a Bioconductor package), implement it with base R or the listed "
                           "packages instead; do not install anything.")
        code, contracts = {}, []
        for number, step in enumerate(plan["steps"], 1):
            if on_generation: on_generation(step)
            print(f"Generating step {number}/{len(plan['steps'])}: {step['id']}", flush=True)
            user = json.dumps({"step": step, "previous_output_contracts": contracts}, ensure_ascii=False)
            def validate(parsed):
                _validate_step_reply(parsed, step)
                from .output_contracts import validate_contracts
                validate_contracts(parsed["output_contracts"], step, required=bool(plan.get("input_bindings")))
                if validate_code: validate_code(step, parsed["files"][0]["content"])
            try:
                result = client.chat_json("tui_task_codegen", system, user, "tui_task_codegen_step_v3",
                                          validator=validate)
                parsed = result["parsed"]
                _validate_step_reply(parsed, step)
            except Exception as exc:
                from orchestrator.llm_client import LLMError
                message = f"Code generation failed for step {step['id']}: {type(exc).__name__}: {exc}"
                if isinstance(exc, (LLMError, ValueError)):
                    raise type(exc)(message) from exc
                raise RuntimeError(message) from exc
            code[step["id"]] = parsed["files"][0]["content"]
            produced = [{**item, "step_id": step["id"]}
                        for item in parsed["output_contracts"]]
            if on_step is not None:
                on_step(step, code[step["id"]], produced)
            contracts.extend(produced)
        return code
    # Legacy plans remain readable; new cache identity requires syntax validation.
    if on_generation: on_generation(plan["steps"][0])
    system = CODEGEN.replace("{plan}", json.dumps(plan, ensure_ascii=False))
    if cfg is not None:
        from .r_env import availability_note
        note = availability_note(cfg)
        if note:
            system += ("\n" + note + "\nUse only these packages or base R; do not install anything.")
    def validate_single(parsed):
        files = parsed.get("files") if isinstance(parsed,dict) else None
        if not isinstance(files,list) or len(files)!=1 or not isinstance(files[0],dict):
            raise ValueError("Generate exactly one script for the current step")
        entry=files[0]
        if entry.get("step_id")!=plan["steps"][0]["id"] or not isinstance(entry.get("content"),str) or not 10<=len(entry["content"])<=120000:
            raise ValueError("Invalid single-step script")
        if validate_code: validate_code(plan["steps"][0],entry["content"])
    result = client.chat_json("tui_task_codegen", system, "Generate the scripts for this approved plan.",
                              "tui_task_codegen_v2", validator=validate_single)
    parsed = result["parsed"]
    files = parsed.get("files") if isinstance(parsed, dict) else None
    if not isinstance(files, list) or len(files) != len(plan["steps"]):
        raise ValueError("Code generation returned an incomplete file set")
    expected = {step["id"] for step in plan["steps"]}
    got = {entry.get("step_id") for entry in files if isinstance(entry, dict)}
    if got != expected:
        raise ValueError("Generated step IDs do not match the approved plan")
    for entry in files:
        if not isinstance(entry.get("content"), str) or not 10 <= len(entry["content"]) <= 120000:
            raise ValueError("Generated script is empty or too large")
    code = {entry["step_id"]: entry["content"] for entry in files}
    if on_step is not None:
        on_step(plan["steps"][0], code[plan["steps"][0]["id"]], [])
    return code


def _output_record(path: Path, task_dir: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(task_dir):
        raise ValueError(f"Missing or unsafe output: {path}")
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"Empty output: {path}")
    record = {"path": path.relative_to(task_dir).as_posix(), "bytes": size, "sha256": file_hash(path)}
    if path.suffix.lower() in {".csv", ".tsv"}:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
            header = next(reader, [])
            rows = sum(1 for _ in reader)
        record.update(rows=rows, columns=len(header))
    return record


def _state(store: TaskStore, task_id: str, status: str, **values) -> None:
    state = store.get(task_id)
    state.update(status=status, **values)
    store.save(state)
    store.event(task_id, "system", f"Task status: {status}")


def run(root: Path, task_id: str, client=None) -> int:
    from .temp_paths import cleanup
    try:
        return _run(root, task_id, client=client)
    except Exception as exc:
        _record_failure(root, task_id, exc)
        raise
    finally:
        cleanup(root, task_id)


def _run(root: Path, task_id: str, client=None) -> int:
    os.umask(0o077)
    root = root.resolve()
    store = TaskStore(root)
    state = store.get(task_id)
    if state["status"] != "approved":
        raise ValueError("Task is not approved")
    plan = store.plan(task_id)
    if state["approved_hash"] != canonical_hash(plan):
        raise ValueError("Approval does not match the frozen plan")
    from .llm import load_tui_config
    snapshot = store.directory(task_id) / 'model_config.json'
    cfg = load_tui_config(root, snapshot=snapshot if snapshot.is_file() else None)
    request = store.user_text(task_id)
    validate_plan(root, {**plan, "inputs": [x["path"] for x in plan["inputs"]]}, request, cfg)
    for item in plan["inputs"]:
        path = (root / item["path"]).resolve()
        if file_hash(path) != item["sha256"]:
            raise ValueError(f"Input changed after approval: {item['path']}")
    guard_available()  # No unsafe fallback.
    if sys.platform != "linux":
        from native_dist.doctor import require_probe
        require_probe(root)
    if client is None:
        from .model_config import ready
        if not ready(cfg):
            raise RuntimeError("LLM key is missing for code generation")
        client = build_client(root, cfg, artifacts.registry_ids(root),
                              audit_dir=store.directory(task_id) / "llm_calls")
    task_dir = root / "analysis" / task_id
    task_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    task_dir.chmod(0o700)
    for folder in ("scripts", "tables", "objects", "figures/source_data", "reports", "logs", "tmp"):
        (task_dir / folder).mkdir(parents=True, exist_ok=True)
    _state(store, task_id, "generating_code", current_stage="generation", current_step=None)
    print(f"Generating scripts for {task_id}", flush=True)
    env = _step_environment(root, task_dir, cfg, plan["inputs"])
    scripts, output_contracts, attempts = [], [], []
    def save_manifest(status="generating_code", **extra):
        _atomic_json(task_dir / "code_manifest.json", {"plan_hash": state["plan_hash"],
            "status":status, "scripts":scripts, "inputs":plan["inputs"],
            "output_contracts":output_contracts, "attempts":attempts, **extra})

    def validate_candidate(step, content):
        _state(store, task_id, "generating_code", current_stage="preflight", current_step=step["id"])
        suffix = ".py" if step["language"] == "python" else ".R"
        path = task_dir / "scripts/attempts" / step["id"] / (f"{len(attempts)+1:03d}" + suffix)
        _write_new(path, content)
        entry = {"step_id":step["id"], "path":path.relative_to(task_dir).as_posix(),
                 "sha256":file_hash(path), "syntax_passed":False}
        attempts.append(entry)
        try:
            _check_code(root, path, step["language"], env=env, dependencies=True)
        except MissingDependencyError as exc:
            # Correctable: the model is asked to rewrite with base R or installed
            # packages. Recorded as a dependency problem, never as a policy abort.
            entry["error_kind"] = "dependency"
            entry["error"] = str(exc).replace(str(path), path.name).replace(str(root), "PROJECT_ROOT")
            raise ValueError(f"Missing dependency in step {step['id']}: {entry['error']}") from exc
        except (SyntaxError, GeneratedCodeError) as exc:
            # Only parser diagnostics are sent back; do not send runtime data or absolute host paths.
            entry["error_kind"] = "syntax" if isinstance(exc,SyntaxError) else "code_validation"
            entry["error"] = str(exc).replace(str(path), path.name).replace(str(root), "PROJECT_ROOT")
            raise ValueError(f"Code validation error in step {step['id']}: {entry['error']}") from exc
        except Exception as exc:
            entry["error_kind"] = "dependency" if isinstance(exc,ImportError) else "policy" if isinstance(exc,LLMNonRetryableValidationError) else "environment"
            entry["error"] = str(exc)
            raise LLMNonRetryableValidationError(str(exc)) from exc
        else:
            entry["syntax_passed"] = True
        finally:
            save_manifest()


    def checkpoint(step, content, produced):
        suffix = ".py" if step["language"] == "python" else ".R"
        path = task_dir / "scripts" / (step["id"] + suffix)
        _write_new(path, content)
        scripts.append({"step_id": step["id"], "path": path.relative_to(task_dir).as_posix(),
                        "sha256": file_hash(path), "preflight_passed": False})
        output_contracts.extend(produced)
        def save_checkpoint():
            save_manifest()
        save_checkpoint()
        try:
            _check_code(root, path, step["language"], env=env)
        except Exception as exc:
            raise RuntimeError(f"Preflight failed for step {step['id']}: {type(exc).__name__}: {exc}") from exc
        scripts[-1]["preflight_passed"] = True
        save_checkpoint()

    _codegen(client, plan, on_step=checkpoint, validate_code=validate_candidate, cfg=cfg,
             on_generation=lambda step: _state(store, task_id, "generating_code", current_stage="generation", current_step=step["id"]))
    save_manifest("preflight_complete")
    _state(store, task_id, "running", current_stage="execution")
    print(f"Running {len(plan['steps'])} approved step(s)", flush=True)
    outputs = []
    for step in plan["steps"]:
        _state(store, task_id, "running", current_stage="execution", current_step=step["id"])
        suffix = ".py" if step["language"] == "python" else ".R"
        script = task_dir / "scripts" / (step["id"] + suffix)
        interpreter = sys.executable if step["language"] == "python" else rscript_path(root)
        argv = [sys.executable, str(Path(__file__).with_name("task_guard.py")), str(task_dir), "--",
                interpreter, *( ["--vanilla"] if step["language"] == "r" else []), str(script)]
        log_path = task_dir / "logs" / (step["id"] + ".log")
        store.event(task_id, "system", f"Starting step {step['id']}; log {log_path}")
        print(f"Starting step {step['id']}; log {log_path}", flush=True)
        with log_path.open("wb") as log:
            result = subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=24 * 3600,
                                    check=False, close_fds=True)
        if result.returncode:
            raise RuntimeError(f"Step {step['id']} failed (exit {result.returncode}); see {log_path}")
        _state(store, task_id, "running", current_stage="verification", current_step=step["id"])
        from .output_contracts import verify_output
        for relative in step["outputs"]:
            record = _output_record(task_dir / relative, task_dir)
            contract = next((c for c in output_contracts if c["path"]==relative), None)
            if contract and "format" in contract:
                record["contract_validation"] = verify_output(root, task_dir, relative, contract, env, plan)
            outputs.append(record)
        store.event(task_id, "system", f"Completed step {step['id']} with {len(step['outputs'])} outputs")
        print(f"Completed step {step['id']}", flush=True)
    _state(store, task_id, "verifying", current_stage="verification", current_step=None)
    declared_outputs = {output for step in plan["steps"] for output in step["outputs"]}
    unexpected = []
    for folder in ("tables", "objects", "figures", "reports"):
        for path in (task_dir / folder).rglob("*"):
            if path.is_file() and path.relative_to(task_dir).as_posix() not in declared_outputs:
                unexpected.append(path.relative_to(task_dir).as_posix())
    if unexpected:
        raise ValueError(f"Undeclared outputs: {unexpected[:20]}")
    for item in plan["inputs"]:
        if file_hash((root / item["path"]).resolve()) != item["sha256"]:
            raise ValueError(f"Input changed during execution: {item['path']}")
    try:
        r_info = subprocess.run([rscript_path(root), "--vanilla", "-e", "cat(R.version.string)"],
                                capture_output=True, text=True, timeout=30, check=False)
        r_version = r_info.stdout.strip() if r_info.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        r_version = "unavailable"
    manifest = {"task_id": task_id, "plan_hash": state["plan_hash"], "completed_at": utc(),
                "inputs": plan["inputs"], "scripts": scripts, "outputs": outputs,
                "python": sys.version.split()[0], "python_executable": sys.executable,
                "r_version": r_version,
                "model_connection": {name: cfg['llm'].get(name) for name in
                                     ('provider', 'base_url', 'model', 'auth_mode', 'json_mode')}}
    _atomic_json(task_dir / "result_manifest.json", manifest)
    lines = [f"# Task {task_id}", "", f"Goal: {plan['goal']}",
             f"Independent unit: {plan['analysis_unit']}", f"Method: {plan['method']}",
             "", "## Inputs", ""]
    lines += [f"- {item['path']} (SHA256 {item['sha256']})" for item in plan["inputs"]]
    lines += ["", "## Outputs", ""]
    lines += [f"- {item['path']} ({item['bytes']} bytes; SHA256 {item['sha256']}; "
              f"rows {item.get('rows', 'n/a')}, columns {item.get('columns', 'n/a')})" for item in outputs]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in plan["limitations"]]
    lines += ["", "This report inventories outputs; scientific claims require review of complete tables.", ""]
    (task_dir / "reports/task_report.md").write_text("\n".join(lines), encoding="utf-8")
    save_manifest("succeeded")
    _state(store, task_id, "succeeded", current_stage="complete", current_step=None)
    store.event(task_id, "assistant", f"Task completed. Results: {task_dir / 'reports/task_report.md'}")
    return 0


def _record_failure(root, task_id, exc):
    store = TaskStore(root)
    state = store.get(task_id)
    if state["status"] not in {"approved","generating_code","running","verifying"}: return
    stage = state.get("current_stage", "generation")
    cause = exc
    while cause.__cause__ is not None: cause = cause.__cause__
    kind = ("syntax" if isinstance(cause,SyntaxError) else "dependency" if isinstance(cause,ImportError)
            else "policy" if isinstance(exc,LLMNonRetryableValidationError) else "validation" if isinstance(cause,ValueError)
            else "runtime")
    details = {"failure_stage":stage, "failed_step":state.get("current_step"), "failure_kind":kind}
    state.update(status="failed", error=f"{type(exc).__name__}: {exc}", **details)
    store.save(state)
    store.event(task_id, "system", f"Task failed at {stage}: {type(exc).__name__}: {exc}")
    task_dir = Path(root)/"analysis"/task_id
    if task_dir.is_dir():
        (task_dir/"reports").mkdir(exist_ok=True)
        (task_dir/"reports/failure.txt").write_text(traceback.format_exc(),encoding="utf-8")
        path=task_dir/"code_manifest.json"
        manifest=json.loads(path.read_text()) if path.is_file() else {"scripts":[],"attempts":[]}
        if stage=="preflight" and manifest.get("attempts") and manifest["attempts"][-1].get("error_kind") in {"syntax","code_validation","dependency","policy","environment"}:
            details["failure_kind"]=manifest["attempts"][-1]["error_kind"]
            state.update(details); store.save(state)
        manifest.update(status="failed",error=state["error"],**details)
        _atomic_json(path,manifest)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        raise SystemExit("Usage: python -m multiomics_tui.task_runner ROOT TASK_ID")
    root, task_id = Path(args[0]), args[1]
    try:
        return run(root, task_id)
    except Exception as exc:
        LOG.exception("Task failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
