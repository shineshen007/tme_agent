"""CLI entry for ``multiomics``: TUI by default, thin pass-through for headless runs."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import __version__
from .root import ProjectRootError, resolve_root

PASSTHROUGH = {"run", "resume", "verify", "dossiers", "verify-dossiers"}


def _extract_flag(argv: list[str], flag: str):
    value = None
    rest = []
    index = 0
    while index < len(argv):
        if argv[index] == flag and index + 1 < len(argv):
            value = argv[index + 1]
            index += 2
            continue
        rest.append(argv[index])
        index += 1
    return value, rest


def _first_positional(argv: list[str]):
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            if token in {"--project-root"}:
                skip_next = True
            continue
        return token
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multiomics",
        description="CLI/TUI for the paired multiomics project. No command starts the TUI; "
                    "run/resume/verify/dossiers/verify-dossiers pass through to the framework.",
    )
    parser.add_argument("--project-root", help="Project root (auto-detected by default)")
    parser.add_argument("--repl", action="store_true", help="Use the REPL instead of the full-screen TUI")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM routing and Q&A summaries")
    parser.add_argument("--version", action="version", version=f"multiomics {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="Start the TUI (default)")
    sub.add_parser("update", help="Check, apply or roll back a public GitHub release (update --help)")
    module = sub.add_parser("module", help="Independent analysis module")
    module_sub = module.add_subparsers(dest="module_cmd", required=True)
    module_sub.add_parser("list", help="List available modules")
    run_module = module_sub.add_parser("run", help="Run a module")
    run_module.add_argument("name", help="Module name relative to analysis/")
    return parser


def launch_tui(root: Path, repl: bool = False, allow_llm: bool = True) -> int:
    from .controller import Session
    session = Session(root, allow_llm=allow_llm)
    if not repl:
        try:
            import textual  # noqa: F401
        except Exception:
            print('multiomics: textual is unavailable; starting the REPL. '
                  'Install the full-screen interface: .venv/bin/pip install "textual>=1,<2"', file=sys.stderr)
            repl = True
    if repl:
        from .fallback import run_repl
        return run_repl(session)
    from .app import MultiomicsApp
    MultiomicsApp(session).run()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    project_root, rest = _extract_flag(argv, "--project-root")

    positional = _first_positional(rest)
    if positional == "update":
        from native_dist.public_update import main as update_main
        update_args = rest[rest.index("update") + 1:]
        if project_root:
            update_args += ["--project-root", project_root]
        return update_main(update_args)

    if positional in PASSTHROUGH:
        if "--repl" in rest or "--no-llm" in rest:
            print("multiomics: --repl/--no-llm only apply to the interactive interface; "
                  "pass framework options directly to run/resume/verify/dossiers/verify-dossiers", file=sys.stderr)
            return 2
        try:
            root = resolve_root(project_root)
        except ProjectRootError as exc:
            print(f"multiomics: {exc}", file=sys.stderr)
            return 2
        from .platform_support import require_supported_action
        try:
            require_supported_action(root, positional)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        from orchestrator.runner import main as runner_main
        return runner_main(rest + ["--project-root", str(root)])

    args = build_parser().parse_args(rest)
    try:
        root = resolve_root(args.project_root or project_root)
    except ProjectRootError as exc:
        print(f"multiomics: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "module":
        from .actions import build_command, list_modules
        if args.module_cmd == "list":
            modules = list_modules(root)
            print("\n".join(modules) if modules else "(no modules under analysis/)")
            return 0
        try:
            command = build_command(root, "module_run", module=args.name)
        except Exception as exc:  # noqa: BLE001 - CLI surface
            print(f"multiomics: {exc}", file=sys.stderr)
            return 2
        print("Running:", " ".join(command))
        return subprocess.call(command, cwd=str(root))

    if args.cmd in (None, "tui"):
        if not sys.stdin.isatty() and not args.repl:
            build_parser().print_help()
            return 0
        return launch_tui(root, repl=args.repl, allow_llm=not args.no_llm)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
