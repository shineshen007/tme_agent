"""Synthetic Seatbelt startup A/B and minimal, PID-scoped crash summaries."""
from __future__ import annotations
import json
from pathlib import Path
import time


def root_read_ab(policy, commands, env, task, collect):
    from multiomics_tui.macos_guard import ROOT_DIRECTORY_READ
    lines = policy.splitlines()
    if lines.count(ROOT_DIRECTORY_READ) != 1 or '(allow default)' in lines:
        raise ValueError('A/B requires exactly one literal root grant in the restricted policy')
    baseline = '\n'.join(line for line in lines if line != ROOT_DIRECTORY_READ)
    results = {}
    for name, argv in commands.items():
        pair = {}
        for variant, candidate in [('without_root_read', baseline), ('literal_root_read', policy)]:
            step = collect('ab_' + name + '_' + variant,
                           ['/usr/bin/sandbox-exec', '-p', candidate, *argv], env=env, cwd=task)
            pair[variant] = {k: step[k] for k in ('pid','returncode','signal','timed_out','error') if k in step}
        pair['root_allowance_isolates_startup_failure'] = (pair['without_root_read'].get('returncode') not in (None,0)
                                                        and pair['literal_root_read'].get('returncode') == 0)
        results[name] = pair
    return results


def parse_crash(text):
    # Apple .ips may contain one metadata JSON line followed by a JSON body.
    decoder = json.JSONDecoder()
    offset = 0
    result = None
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        value, offset = decoder.raw_decode(text, offset)
        if isinstance(value, dict) and 'pid' in value:
            result = value
    return result


def summarize_crash(body):
    # No environment, arguments, registers, memory dumps or full .ips are emitted.
    from multiomics_tui.model_config import redact
    result = {key: body[key] for key in ('pid','procName','captureTime') if key in body}
    result['exception'] = {k: body.get('exception', {}).get(k) for k in ('type','signal','codes') if k in body.get('exception', {})}
    result['termination'] = {k: body.get('termination', {}).get(k) for k in ('namespace','code','indicator','reasons') if k in body.get('termination', {})}
    images = body.get('usedImages', [])
    index = body.get('faultingThread')
    threads = body.get('threads', [])
    thread = threads[index] if isinstance(index,int) and 0 <= index < len(threads) else next((t for t in threads if t.get('triggered')), {})
    result['frames'] = []
    for frame in thread.get('frames', [])[:16]:
        item = {k: frame[k] for k in ('symbol','symbolLocation','imageOffset') if k in frame}
        i = frame.get('imageIndex')
        if isinstance(i,int) and 0 <= i < len(images):
            item['image_name'] = images[i].get('name')
        result['frames'].append(item)
    # Known system abort reasons can identify _dirhelper / dyld failure without
    # including arbitrary application state or other processes' crash data.
    asi = body.get('asi', {})
    result['system_abort_reasons'] = {k: asi[k] for k in ('dyld','libsystem_c.dylib','libsystem_secinit.dylib') if k in asi}
    return json.loads(redact(json.dumps(result, ensure_ascii=False)))


def crash_summaries(pids, since, directories=None):
    directories = directories if directories is not None else [Path.home()/'Library/Logs/DiagnosticReports',
                                                               Path('/Library/Logs/DiagnosticReports')]
    summaries, errors = [], []
    for directory in directories:
        try:
            paths = [p for p in Path(directory).glob('*.ips') if p.name.lower().startswith(('python','rscript','r-'))
                     and not p.is_symlink() and p.stat().st_mtime >= since-10 and p.stat().st_size <= 10_000_000]
        except OSError as exc:
            errors.append(type(exc).__name__)
            continue
        for path in sorted(paths, key=lambda p:p.stat().st_mtime, reverse=True)[:12]:
            try:
                body = parse_crash(path.read_text(encoding='utf-8'))
                if body and body.get('pid') in pids:
                    summaries.append(summarize_crash(body))
            except (OSError, ValueError, TypeError) as exc:
                errors.append(type(exc).__name__)
    return {'status':'found' if summaries else 'not_available_yet', 'summaries':summaries, 'error_types':sorted(set(errors)),
            'note':'Only diagnostic child PIDs; crash reports may appear later. No complete crash files collected.'}
