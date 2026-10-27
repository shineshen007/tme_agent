"""Scientific Wiki commands with content-bound, explicit write confirmation.

Command preparation is read-only. The controller calls ``execute_wiki`` only
after its normal human confirmation flow; command text never grants approval.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import sqlite3

from orchestrator.knowledge import (
    KnowledgeError, KnowledgeConflictError, canonical_json, content_sha256, validate_payload,
)
from orchestrator.knowledge_context import knowledge_service


MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ENTRIES = 50
HELP = (
    'Scientific Wiki\n'
    '/wiki search <关键词>：检索已审查知识\n'
    '/wiki show <条目 ID> [版本号]：查看知识与来源\n'
    '/wiki pending：查看待审候选\n'
    '/wiki import knowledge/resources/<文件>.json：预览并导入候选\n'
    '/wiki review <候选 ID|all> [approve|reject]：预览人工审查\n'
    '/wiki retract <条目 ID>：预览撤回\n'
    '/wiki publish <条目 ID>：预览发布到共享库\n'
    '/wiki export knowledge/exports/<新目录> [关键词]：预览导出 JSON/Markdown\n'
    '写入均需确认；导入候选、批准知识和共享发布是独立操作。'
)


def _root(root):
    return Path(root).expanduser().resolve()


def _safe_path(root, raw, category, *, existing):
    """Confine command paths to one declared area and reject every symlink."""
    if not isinstance(raw, str) or not raw.strip() or '\\' in raw:
        raise KnowledgeError('请使用项目内的相对路径')
    relative = PurePosixPath(raw)
    expected = ('knowledge', category)
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:2] != expected or len(relative.parts) < 3:
        raise KnowledgeError(f'路径必须位于 knowledge/{category}/，不接受绝对路径或 ..')
    root = _root(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise KnowledgeError('Wiki 导入和导出路径不接受符号链接')
    if not current.resolve().is_relative_to(root / 'knowledge' / category):
        raise KnowledgeError('路径越过允许的项目目录')
    if existing:
        if not current.is_file():
            raise KnowledgeError('指定的 Wiki 资源文件不存在')
        if current.suffix.lower() != '.json':
            raise KnowledgeError('Wiki 导入只接受 JSON 文件')
    elif current.exists():
        raise KnowledgeError('导出目录已存在；请选择新的目录以保留旧快照')
    return current


def _read_import(root, raw):
    path = _safe_path(root, raw, 'resources', existing=True)
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise KnowledgeError('单个 Wiki 导入文件不得超过 2 MiB')
    content = path.read_bytes()
    if len(content) > MAX_IMPORT_BYTES:
        raise KnowledgeError('Wiki 资源文件在读取期间超过大小限制')
    parsed = json.loads(content.decode('utf-8'))
    if isinstance(parsed, dict) and set(parsed) == {'entries'}:
        parsed = parsed['entries']
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= MAX_IMPORT_ENTRIES:
        raise KnowledgeError(f'每次导入须包含 1–{MAX_IMPORT_ENTRIES} 条知识候选')
    values = [validate_payload(value) for value in parsed]
    return hashlib.sha256(content).hexdigest(), values


def _display(value):
    # Documents remain data. Remove control characters from terminal previews.
    return ''.join(c for c in str(value) if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)


def _entry_preview(item, *, previous=None):
    lines = [f'{_display(item["title"])} [{item["kind"]}]']
    if item.get('entry_id'):
        lines.append(f'ID: {item["entry_id"]}')
    if item.get('sha256'):
        lines.append(f'SHA256: {item["sha256"]}')
    if item.get('revision'):
        lines.append(f'版本: {item["revision"]}；范围: {item.get("scope", "project")}；状态: {item.get("status", "candidate")}')
    if previous is not None:
        lines.extend(['原陈述: ' + _display(previous['statement']), '新陈述: ' + _display(item['statement'])])
    else:
        lines.append('陈述: ' + _display(item['statement']))
    lines.append('背景: ' + _display(canonical_json(item['context'])))
    for source in item['sources']:
        role = '主要来源' if source['primary'] else '补充来源'
        lines.append(f'- {role} {source["source_id"]} ({source["source_type"]}): {_display(source["locator"])}')
        if source.get('quote'):
            lines.append('  原文定位: ' + _display(source['quote']))
    if not item['sources']:
        lines.append('来源: 无；该条目不得作为已验证事实。')
    if item['relations']:
        lines.append('关系: ' + _display(canonical_json(item['relations'])))
    return '\n'.join(lines)


def _confirmation(root, service, operation, arguments):
    frozen = {'schema_version': '1.0', 'workspace': str(_root(root)),
              'library_root': str(service.root),
              'shared_root': str(service.shared_root) if service.shared_root else None,
              'operation': operation, 'arguments': arguments}
    frozen['payload_sha256'] = content_sha256(frozen)
    return frozen


def _prepared(root, service, operation, arguments, message):
    return {'message': message + '\n\n确认后执行上述操作；取消不会修改知识库。',
            'confirmation': _confirmation(root, service, operation, arguments)}


def _local_entry(service, entry_id):
    if entry_id.startswith('shared:'):
        raise KnowledgeError('请在所属项目中操作本地条目；不能直接修改其他项目的共享知识')
    entry_id = entry_id.removeprefix('project:')
    entry = service.get('project:' + entry_id)
    if entry is None:
        raise KnowledgeError('没有找到本项目的知识条目')
    return entry


def handle_wiki(root, text, settings=None):
    """Prepare a command; expected input errors become explicit UI messages."""
    try:
        return _handle_wiki(root, text, settings)
    except (KnowledgeError, OSError, UnicodeError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        return {'message': f'Wiki 操作未执行：{exc}', 'error': True}


def _handle_wiki(root, text, settings):
    try:
        args = shlex.split(text.strip())
    except ValueError as exc:
        raise KnowledgeError('命令中的引号没有闭合') from exc
    if args and args[0] == '/wiki':
        args.pop(0)
    if not args or args[0] in {'help', '?'}:
        return {'message': HELP}
    if '--yes' in args or '-y' in args:
        raise KnowledgeError('不支持 --yes/-y；请先查看具体预览，再使用界面的确认操作')
    command, params = args[0], args[1:]
    service = knowledge_service(root, settings, read_only=True)
    if command == 'search':
        entries = service.search(' '.join(params))
        if not entries:
            return {'message': '未找到匹配的已审查知识。候选不会进入默认检索；可用 /wiki pending 查看。'}
        return {'message': '\n\n'.join(_entry_preview(item) for item in entries)}
    if command == 'show' and len(params) in {1, 2}:
        try:
            revision = int(params[1]) if len(params) == 2 else None
        except ValueError as exc:
            raise KnowledgeError('版本号须为正整数') from exc
        entry = service.get(params[0], revision)
        if entry is None:
            raise KnowledgeError('条目或指定版本不存在')
        message = _entry_preview(entry)
        if entry['backlinks']:
            message += '\n反向关联: ' + _display(canonical_json(entry['backlinks']))
        return {'message': message}
    if command == 'pending' and not params:
        proposals = service.pending()
        if not proposals:
            return {'message': '当前没有待审知识候选。'}
        return {'message': '\n\n'.join(
            f'{p["id"]}\nSHA256: {p["sha256"]}\n' + _entry_preview(p['payload']) for p in proposals)}
    if command == 'import' and len(params) == 1:
        file_sha, payloads = _read_import(root, params[0])
        previews = []
        for payload in payloads:
            old = service.get('project:' + payload['entry_id']) if payload.get('entry_id') else None
            if old and payload.get('base_sha256') != old['sha256']:
                raise KnowledgeConflictError('更新候选缺少匹配的 base_sha256；请先核对当前版本')
            if not old and payload.get('base_sha256'):
                raise KnowledgeConflictError('更新目标不存在')
            previews.append(('更新候选\n' if old else '新增候选\n') + _entry_preview(payload, previous=old))
        message = (f'将从 {params[0]} 导入 {len(payloads)} 条候选。\n'
                   f'文件 SHA256: {file_sha}\n导入后仍需独立人工审查。\n\n' + '\n\n'.join(previews))
        return _prepared(root, service, 'import', {'path': params[0], 'file_sha256': file_sha,
                                                  'payloads': payloads}, message)
    if command == 'review' and len(params) in {1, 2}:
        decision = params[1] if len(params) == 2 else 'approve'
        if decision not in {'approve', 'reject'}:
            raise KnowledgeError('审查决定只能是 approve 或 reject')
        pending = service.pending()
        selected = pending if params[0] == 'all' else [p for p in pending if p['id'] == params[0]]
        if not selected:
            raise KnowledgeError('没有匹配的待审候选')
        if len(selected) > 100:
            raise KnowledgeError('一次最多审查 100 条；请按候选 ID 分批审查')
        if decision == 'approve' and len({p['entry_id'] for p in selected}) != len(selected):
            raise KnowledgeError('同一条目存在多个候选版本，请按候选 ID 分别选择，不可同时批准')
        action = '批准进入项目知识库' if decision == 'approve' else '拒绝候选'
        previews = []
        for item in selected:
            old = service.get('project:' + item['entry_id'])
            if decision == 'approve' and (old['sha256'] if old else None) != item['base_sha256']:
                raise KnowledgeConflictError('候选依据的当前版本已改变，请重新生成候选')
            previews.append(f'{item["id"]}\nSHA256: {item["sha256"]}\n' + _entry_preview(item['payload'], previous=old))
        return _prepared(root, service, 'review',
                         {'decision': decision, 'items': [{'id': p['id'], 'sha256': p['sha256']} for p in selected]},
                         f'将{action}：{len(selected)} 条。不会自动发布到共享库。\n\n' + '\n\n'.join(previews))
    if command in {'retract', 'publish'} and len(params) == 1:
        entry = _local_entry(service, params[0])
        if entry['status'] != 'reviewed':
            raise KnowledgeError('只能操作当前已审查且未撤回的条目')
        if command == 'publish':
            if service.shared_root is None:
                raise KnowledgeError('共享 Wiki 已禁用')
            # A preflight only: the write method repeats all checks under lock.
            service._share_guard(entry)
            message = ('将以下已审查条目发布到明确配置的共享库，供其他项目检索。\n'
                       f'共享库: {service.shared_root}\n请核对来源及去标识情况。\n\n')
        else:
            message = '将撤回以下条目及本项目已发布的共享版本；历史版本保留，待审更新失效。\n\n'
        return _prepared(root, service, command,
                         {'entry_id': entry['entry_id'], 'expected_sha256': entry['sha256']},
                         message + _entry_preview(entry))
    if command == 'export' and params:
        _safe_path(root, params[0], 'exports', existing=False)
        query = ' '.join(params[1:])
        snapshot = service.snapshot(query)
        return _prepared(root, service, 'export',
                         {'path': params[0], 'query': query, 'snapshot_sha256': snapshot['sha256']},
                         f'将导出 {len(snapshot["entries"])} 条知识到 {params[0]}。\n'
                         f'快照 SHA256: {snapshot["sha256"]}\n产物: snapshot.json、snapshot.md')
    raise KnowledgeError('命令或参数不正确。\n' + HELP)


def execute_wiki(root, confirmation, settings=None):
    """Execute only the exact action accepted through the controller's review UI."""
    try:
        return _execute_wiki(root, confirmation, settings)
    except (KnowledgeError, OSError, UnicodeError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        return {'message': f'Wiki 操作失败：{exc}', 'error': True}


def _execute_wiki(root, confirmation, settings):
    if not isinstance(confirmation, dict):
        raise KnowledgeError('缺少有效的确认记录')
    frozen = {k: v for k, v in confirmation.items() if k != 'payload_sha256'}
    if content_sha256(frozen) != confirmation.get('payload_sha256'):
        raise KnowledgeConflictError('确认内容被修改，请重新预览')
    if frozen.get('schema_version') != '1.0' or frozen.get('workspace') != str(_root(root)):
        raise KnowledgeConflictError('确认记录不属于当前工作区')
    service = knowledge_service(root, settings, read_only=False)
    if frozen.get('library_root') != str(service.root) or frozen.get('shared_root') != (
            str(service.shared_root) if service.shared_root else None):
        raise KnowledgeConflictError('知识库配置已改变，请重新预览')
    operation = frozen.get('operation')
    args = frozen.get('arguments')
    if not isinstance(args, dict):
        raise KnowledgeError('确认记录缺少具体参数')
    required = {'import': {'path', 'file_sha256', 'payloads'},
                'review': {'items', 'decision'},
                'retract': {'entry_id', 'expected_sha256'},
                'publish': {'entry_id', 'expected_sha256'},
                'export': {'path', 'query', 'snapshot_sha256'}}
    if operation not in required or set(args) != required[operation]:
        raise KnowledgeError('确认记录的操作参数无效')
    if operation == 'import':
        file_sha, payloads = _read_import(root, args['path'])
        if file_sha != args['file_sha256'] or canonical_json(payloads) != canonical_json(args['payloads']):
            raise KnowledgeConflictError('导入文件在预览后发生变化，请重新预览')
        proposals = service.propose_many(args['payloads'])
        pending = [p for p in proposals if p['status'] == 'pending']
        return {'message': f'已处理 {len(proposals)} 条候选，其中 {len(pending)} 条待审；已审查的相同内容已复用。\n'
                           '使用 /wiki pending 查看；使用 /wiki review <候选 ID> 或 /wiki review all 审查。'}
    if operation == 'review':
        reviewed = service.review_many(args['items'], args['decision'], reviewer='user')
        action = '批准' if args['decision'] == 'approve' else '拒绝'
        return {'message': f'已{action} {len(reviewed)} 条知识候选。共享发布须单独使用 /wiki publish。'}
    if operation == 'retract':
        entry = service.retract(args['entry_id'], args['expected_sha256'])
        return {'message': f'已撤回 {entry["entry_id"]}，历史版本保留。'}
    if operation == 'publish':
        entry = service.publish(args['entry_id'], args['expected_sha256'], confirmed=True)
        return {'message': f'已发布 {entry["entry_id"]} 到共享库；版本 {entry["revision"]}。'}
    if operation == 'export':
        directory = _safe_path(root, args['path'], 'exports', existing=False)
        service.export_snapshot(directory, args['query'], expected_sha256=args['snapshot_sha256'])
        return {'message': f'已导出知识快照: {args["path"]}/snapshot.json 和 snapshot.md。'}
    raise KnowledgeError('未识别的 Wiki 确认操作')
