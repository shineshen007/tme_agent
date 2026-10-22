"""User-owned native installation. No API keys, shell edits or admin actions."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_dist.environment import (inventory, inspect_python, requirements,
                                     inspect_r, r_candidates, r_packages, r_user_library_paths)

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("native_install")


def run(argv, **kwargs):
    from multiomics_tui.model_config import redact
    command = list(map(str, argv))
    LOG.info("执行: %s", redact(" ".join(command)))
    # Tee the child's output through the same redactor and FileHandler as the
    # installer. Inherited terminal output alone used to disappear from logs.
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", **kwargs) as child:
        for line in child.stdout:
            LOG.info("%s", redact(line.rstrip()))
        code = child.wait()
    LOG.info("退出码: %s", code)
    if code:
        raise subprocess.CalledProcessError(code, command)


def captured_run(argv, **kwargs):
    """Keep parsed stdout separate while retaining both streams in install.log."""
    from multiomics_tui.model_config import redact
    command = list(map(str, argv))
    LOG.info("执行: %s", redact(" ".join(command)))
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', check=False, **kwargs)
    for output in (result.stdout, getattr(result, 'stderr', '')):
        for line in (output or '').splitlines():
            LOG.info("%s", redact(line))
    LOG.info("退出码: %s", result.returncode)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    return result


def checkpoint(report, path, phase=None):
    if phase:
        if report.get('phase') and report['phase'] != phase:
            report['last_successful_phase'] = report['phase']
        report['phase'] = phase
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')
    temp.replace(path)


def detect_r():
    for candidate in r_candidates(ROOT):
        if inspect_r(candidate, ROOT)['compatible']:
            return Path(candidate).resolve()
    raise RuntimeError("未找到 R 4.4 或更新版本。请安装官方 R，再运行配置脚本。非默认位置可设置 MULTIOMICS_RSCRIPT。")


def python_exe(root):
    return root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def _write_reused_libraries(py, root, inspected):
    """Expose host/user libraries inside the project venv via a .pth file.

    This must also run for an existing venv: discovery adds the user site to its
    own check, but a venv normally disables user site at runtime.
    """
    venv = (Path(root) / ".venv").resolve()
    purelib = Path(captured_run(
        [py, '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))']).stdout.strip())
    if not purelib.resolve().is_relative_to(venv):
        raise RuntimeError('Project package directory is outside the project venv')
    reused = []
    for value in inspected.get('library_paths', []):
        path = Path(value)
        if (not path.is_absolute() or path.name not in {'site-packages', 'dist-packages'}
                or not path.is_dir() or '\n' in str(path) or '\r' in str(path)):
            continue
        resolved = path.resolve()
        if resolved.is_relative_to(venv):
            continue
        reused.append(str(resolved))
    reused = list(dict.fromkeys(reused))
    if not reused:
        return []
    target = purelib / 'multiomics_reused_libraries.pth'
    content = '\n'.join(reused) + '\n'
    try:
        if target.is_file() and target.read_text(encoding='utf-8') == content:
            return reused
        temp = target.with_name(target.name + '.tmp')
        temp.write_text(content, encoding='utf-8')
        temp.replace(target)
    except OSError as exc:
        raise RuntimeError(f'无法写入 Python 复用库配置：{target}: {exc}') from exc
    return reused


def dependency_report(py,root):
    result = captured_run([str(py),'-m','native_dist.package_copy','--report','--project-root',str(root)], cwd=root)
    return json.loads(result.stdout)


def setup(root: Path, offline=False, preserve_local_config=False, no_path=False, force_path=False):
    if not (3, 12) <= sys.version_info[:2] < (3, 15):
        raise RuntimeError("请安装 Python 3.13（支持 3.12–3.14），再重试。")
    root = root.resolve()
    from native_dist.public_config import initialize
    initialize(root)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # Installing dependencies while a controller is active can change its
    # imported packages. Fail conservatively rather than modifying that runtime.
    for record in (root/'workspace/tui/jobs').glob('*.json'):
        data = json.loads(record.read_text(encoding='utf-8'))
        if data.get('status') in {'starting','running','stopping','stalled','stop_failed'}:
            raise RuntimeError('项目仍有活动后台任务，请完成或停止后再配置环境。')
    report = inventory(root)
    if not report['python'] or not report['r']:
        raise RuntimeError('缺少兼容 Python/R。请阅读 docs/INSTALL_UPDATE_zh.md，先补齐检查报告中的软件。')
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    report['operations'] = []
    report['status'] = 'configuring'
    report_path = runtime/'install_report.json'
    checkpoint(report, report_path, 'environment_checked')
    try:
        rscript = Path(report['r']['path'])
        py = python_exe(root)
        checkpoint(report, report_path, "python_environment")
        if not py.is_file():
            LOG.info("创建本机 Python 环境")
            run([report['python']['path'], '-m', 'venv', '--system-site-packages', root/'.venv'])
            report['operations'].append({'action':'create_project_environment', 'system_packages_readonly':True})
            checkpoint(report, report_path)
        elif not inspect_python(py, requirements(root))['compatible']:
            raise RuntimeError('已有 .venv 不兼容，未覆盖。请在新解压目录安装当前版本。')
        requirement = root / "native_dist/requirements-native.txt"
        checkpoint(report, report_path, "python_dependencies")
        before = inspect_python(py, requirements(root))
        reused = _write_reused_libraries(py, root, before)
        if reused:
            report['operations'].append({'action':'reuse_selected_python_libraries',
                                         'read_only':True,'paths':reused})
            checkpoint(report, report_path)
        missing = [row['requirement'] for row in before['packages'] if row['status'] != 'reusable']
        if missing:
            run([py, '-m', 'pip', 'install', *(['--no-index'] if offline else []), *missing])
            report['operations'].append({'action':'install_missing_python_packages', 'requirements':missing})
            checkpoint(report, report_path)
        else:
            LOG.info('Python 依赖齐全，跳过 pip 安装')
        after = inspect_python(py, requirements(root))
        if any(row['status'] != 'reusable' for row in after['packages']):
            raise RuntimeError('Python 依赖版本或导入检查未通过，见环境检查报告。')
        dependencies = dependency_report(py,root)
        if dependencies['missing_requirements']:
            required = dependencies['missing_requirements']
            run([py,'-m','pip','install',*(['--no-index'] if offline else []),*requirements(root),*required])
            report['operations'].append({'action':'install_missing_python_dependencies','requirements':required})
            checkpoint(report, report_path)
            after = inspect_python(py,requirements(root))
            if any(row['status'] != 'reusable' for row in after['packages']):
                raise RuntimeError('补齐依赖后导入检查失败。')
        report['python_dependencies'] = dependency_report(py,root)
        # Check only the project's dependency closure, not unrelated global tools.
        run([py, '-m', 'native_dist.package_copy', '--check', '--project-root', root], cwd=root)
        checkpoint(report, report_path, "r_dependencies")
        library = runtime / "r-library"
        library.mkdir(exist_ok=True)
        cran_packages = [line.strip() for line in (root / "native_dist/r-packages.txt").read_text().splitlines() if line.strip()]
        bioc_packages = [line.strip() for line in (root / "native_dist/r-bioc-packages.txt").read_text().splitlines() if line.strip()] if (root / "native_dist/r-bioc-packages.txt").is_file() else []
        packages = r_packages(root)
        r_state = inspect_r(rscript, root)
        if not r_state.get('compatible'):
            raise RuntimeError(f'R 检查失败：{r_state.get("error") or r_state.get("stderr") or "unknown"}')
        missing = [row['name'] for row in r_state.get('packages', []) if row.get('status') != 'reusable']
        r_literal = lambda value: json.dumps(str(value), ensure_ascii=False)
        # Bioconductor dependency trees include large binary packages; the R
        # default download timeout (60s) is too short and causes spurious
        # "some files were not downloaded" failures, so raise it explicitly.
        code = 'options(timeout=1800); lib <- ' + r_literal(library) + '; .libPaths(unique(c(lib,.libPaths()))); '
        code += 'packages <- c(' + ','.join(r_literal(x) for x in packages if x) + '); '
        code += 'missing <- c(' + ','.join(r_literal(x) for x in missing) + '); '
        code += 'cran <- c(' + ','.join(r_literal(x) for x in cran_packages if x) + '); '
        code += 'bioc <- c(' + ','.join(r_literal(x) for x in bioc_packages if x) + '); '
        if offline:
            code += 'if(length(missing)) stop(paste("离线安装缺少 R 包",paste(missing,collapse=",")))'
        else:
            # CRAN packages come from the locked CRAN mirror; Bioconductor packages
            # (e.g. GSVA) are installed through BiocManager against the release
            # matching this R version. Never upgrade unrelated global packages.
            code += ('if(any(cran %in% missing)) install.packages(intersect(cran,missing),lib=lib,repos="https://cloud.r-project.org"); '
                     'bioc_missing <- intersect(bioc,missing); '
                     'if(length(bioc_missing)) { '
                     'if(!requireNamespace("BiocManager",quietly=TRUE)) install.packages("BiocManager",lib=lib,repos="https://cloud.r-project.org"); '
                     'BiocManager::install(bioc_missing,lib=lib,ask=FALSE,update=FALSE); } ; '
                     'if(any(!vapply(missing,requireNamespace,logical(1),quietly=TRUE))) stop("R 包安装不完整")')
        run([rscript, "--vanilla", "-e", code])
        report['operations'].append({'action':'install_missing_r_packages' if missing else 'reuse_r_packages',
                                     'packages':missing})
        checkpoint(report, report_path)
        base_library = captured_run([str(rscript), "--vanilla", "-e", 'cat(R.home("library"))']).stdout.strip()
        detected_libraries = [base_library, *r_user_library_paths()]
        private_r = rscript
        if os.name == "nt":
            checkpoint(report, report_path, "windows_private_runtime")
            from native_dist.windows_runtime import prepare
            private_r, operation = prepare(root, py, after, rscript, library, packages,
                                           report['python_dependencies'], run, captured_run)
            report['operations'].append(operation)
            checkpoint(report, report_path)
            target_r = root / '.runtime/sandbox_runtime/r'
            library = target_r / 'library'
            base_library = str(library)
            detected_libraries = [str(library)]
        checkpoint(report, report_path, "runtime_configuration")
        config_path = root / "config/runtime.local.yaml"
        # Runtime-only YAML; model settings and study thresholds remain unchanged.
        runtime_libraries = list(dict.fromkeys([str(library), base_library, *detected_libraries]))
        r_home = str(target_r) if os.name == 'nt' else report['r']['home']
        content = "runtime:\n  rscript: " + r_literal(private_r) + "\n  r_home: " + r_literal(r_home) + "\n  python_library_paths:\n" + ''.join('    - '+r_literal(p)+'\n' for p in after['library_paths']) + "  r_library_paths:\n" + "".join("    - " + r_literal(path) + "\n" for path in runtime_libraries)
        if preserve_local_config and config_path.exists():
            report["operations"].append({"action": "preserve_existing_runtime_config", "read_only": True})
        else:
            temp = config_path.with_suffix(".yaml.tmp")
            temp.write_text(content, encoding="utf-8")
            temp.replace(config_path)
        api_local = root / "config/api.local.yaml"
        if not api_local.exists():
            api_local.write_text("# 使用界面 /model 设置服务和模型；密钥通过 /key 输入，不能写入本文件。\nllm: {}\n", encoding="utf-8")
        checkpoint(report, report_path, "behavioral_selfcheck")
        run([py, "-m", "native_dist.doctor", "--project-root", root], cwd=root)
        checkpoint(report, report_path, "global_command")
        command_ready = False
        command_on_path = False
        launcher_dir = None
        add_path = True
        if no_path:
            add_path = False
        elif not force_path:
            from native_dist.command_link import previous_path_preference
            if previous_path_preference(root) is False:
                add_path = False
        try:
            from native_dist.command_link import install as install_command
            linked = install_command(root, venv_python=py, add_path=add_path)
            report['operations'].append({'action': 'install_global_command', **linked})
            LOG.info("已创建 multiomics 命令：%s", linked['launcher'])
            command_ready = True
            command_on_path = bool(linked.get('path_configured'))
            launcher_dir = linked.get('bin_dir')
            if linked.get('restart_required'):
                LOG.info("PATH 已更新；请打开新的终端后再运行 multiomics。")
            if linked.get('warning'):
                LOG.warning("multiomics 命令已创建，但 PATH 尚未生效：%s", linked['warning'])
        except Exception as exc:
            from multiomics_tui.model_config import redact
            message = redact(f'{type(exc).__name__}: {exc}')
            report['operations'].append({'action': 'install_global_command',
                                         'status': 'warning', 'error': message})
            LOG.warning("未创建全局 multiomics 命令：%s；仍可使用 start 脚本。", message)
        report.update(status='passed', python_after=after, r_after=inspect_r(rscript,root))
        checkpoint(report, report_path, "complete")
        if command_ready and command_on_path:
            LOG.info("配置完成。可在任意目录运行 multiomics，或双击 start_windows.cmd / start_macos.command 启动。")
        elif command_ready:
            LOG.info("配置完成。multiomics 启动器已创建，但 PATH 未配置；请将 %s 加入 PATH 后使用，或运行 start 脚本。", launcher_dir)
        else:
            LOG.info("配置完成。请双击 start_windows.cmd / start_macos.command 启动；全局 multiomics 命令未创建。")
    except Exception as exc:
        from multiomics_tui.model_config import redact
        report.update(status='failed', error=redact(f'{type(exc).__name__}: {exc}'))
        if isinstance(exc, subprocess.CalledProcessError):
            report['exit_code'] = exc.returncode
        checkpoint(report, report_path)
        raise


def launch(root: Path):
    py = python_exe(root)
    if not py.is_file():
        raise RuntimeError("尚未配置环境。先双击 setup_windows.cmd 或 setup_macos.command。")
    env = dict(os.environ)
    for name in ('MULTIOMICS_APP_ROOT', 'PYTHONHOME', 'PYTHONSTARTUP'):
        env.pop(name, None)
    env.update(PYTHONPATH=str(root), PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               MPLCONFIGDIR=str(root / ".cache/matplotlib"), XDG_CACHE_HOME=str(root / ".cache"),
               LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    # Probe again before entry: an OS upgrade must not silently remove restrictions.
    run([py, "-m", "native_dist.doctor", "--project-root", root], cwd=root, env=env)
    return subprocess.call([str(py), "-m", "multiomics_tui", "--project-root", str(root)], cwd=root, env=env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["check", "setup", "start"])
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--preserve-local-config", action="store_true",
                        help="Keep existing runtime.local.yaml byte-for-byte during a release update")
    parser.add_argument("--no-path", action="store_true",
                        help="Install the multiomics launcher without changing PATH")
    parser.add_argument("--path", action="store_true",
                        help="Add the launcher directory to PATH even if a previous setup used --no-path")
    args = parser.parse_args()
    if args.mode == 'check':
        report = inventory(args.project_root)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report['python'] and report['r'] else 2
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log_dir = args.project_root / ".runtime"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / "install.log", encoding="utf-8")])
    try:
        if args.mode == "start":
            return launch(args.project_root.resolve())
        return setup(args.project_root.resolve(), args.offline, args.preserve_local_config,
                     no_path=args.no_path, force_path=args.path) or 0
    except Exception as exc:
        from multiomics_tui.model_config import redact
        LOG.error("操作未完成：%s", redact(exc))
        report_path = log_dir/'install_report.json'
        if args.mode == 'setup' and report_path.is_file():
            report = json.loads(report_path.read_text(encoding='utf-8'))
            report.update(status='failed', error=redact(f'{type(exc).__name__}: {exc}'))
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        LOG.error("请把 .runtime/install.log 发给项目负责人；不要发送 API key。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
