"""Receiving-machine vault behavior probe, using a disposable synthetic item."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


def probe(root):
    from multiomics_tui.credentials import backend
    from multiomics_tui.task_runner import _step_environment
    from orchestrator.io import load_config
    from .layout import resource
    service = 'TME Multiomics Execution Probe ' + uuid.uuid4().hex
    account = uuid.uuid4().hex
    value = 'synthetic-probe-'+uuid.uuid4().hex
    created = False
    try:
        vault = backend()
        vault.set_password(service, account, value)
        created = True
        if vault.get_password(service, account) != value:
            raise RuntimeError('Credential readback failed')
    except Exception as exc:
        if created:
            vault.delete_password(service, account)
        return {'status':'unavailable', 'error_type':type(exc).__name__,
                'message':'系统凭据库未就绪，请解锁或启用凭据服务；不会明文保存密钥。'}
    try:
        with tempfile.TemporaryDirectory(prefix='credential_probe_',dir=Path(root)/'.runtime') as name:
            task = Path(name)
            (task/'tmp').mkdir()
            source = '''import sys,json,pathlib,errno
if sys.platform=='darwin':
 from keyring.backends.macOS import Keyring
elif sys.platform=='win32':
 from keyring.backends.Windows import WinVaultKeyring as Keyring
else:
 from keyring.backends.SecretService import Keyring
reason='not_found_or_isolated'
try:
 value=Keyring().get_password(SERVICE,ACCOUNT)
 readable=value is not None
except Exception as e:
 chain=[]; cursor=e
 while cursor is not None and cursor not in chain:
  chain.append(cursor); cursor=cursor.__cause__ or cursor.__context__
 denied=any(getattr(x,'errno',None) in (errno.EACCES,errno.EPERM) or getattr(x,'winerror',None) in (5,1312) for x in chain)
 if sys.platform=='darwin':
  from keyring.backends.macOS import api
  denied=denied or any(isinstance(x,api.KeychainDenied) or (isinstance(x,api.Error) and x.args and x.args[0] in (-25293,-25308,-34018)) for x in chain)
 if not denied: raise
 readable=False; reason='permission_denied'
pathlib.Path(RESULT).write_text(json.dumps({'readable':readable,'reason':reason}))
'''.replace('SERVICE',repr(service)).replace('ACCOUNT',repr(account)).replace('RESULT',repr(str(task/'result.json')))
            script=task/'probe.py'
            script.write_text(source,encoding='utf-8')
            # Baseline proves the backend/imports really access the known item.
            plain=subprocess.run([sys.executable,str(script)],capture_output=True,timeout=30)
            if plain.returncode or not json.loads((task/'result.json').read_text())['readable']:
                raise RuntimeError('Credential baseline failed')
            (task/'result.json').unlink()
            env=_step_environment(Path(root),task,load_config(root),[])
            # A known bus address avoids confusing missing configuration with
            # sandbox denial. This address contains no credential.
            if sys.platform=='linux' and os.environ.get('DBUS_SESSION_BUS_ADDRESS'):
                env['DBUS_SESSION_BUS_ADDRESS']=os.environ['DBUS_SESSION_BUS_ADDRESS']
            argv=[sys.executable,str(resource(root,'multiomics_tui/task_guard.py')),str(task),'--',sys.executable,str(script)]
            guarded=subprocess.run(argv,env=env,cwd=task,capture_output=True,timeout=30)
            if guarded.returncode or not (task/'result.json').is_file():
                from multiomics_tui.model_config import redact
                raise RuntimeError('Credential guarded probe did not complete: exit='+str(guarded.returncode)
                                   + '; stderr='+redact(guarded.stderr.decode('utf-8',errors='replace')[-2000:]))
            if json.loads((task/'result.json').read_text())['readable']:
                raise RuntimeError('生成脚本可读取系统凭据，禁止执行任务。')
            if vault.get_password(service,account)!=value:
                raise RuntimeError('Credential unexpectedly changed')
            return {'status':'passed','controller_vault_read_write':True,'generated_script_vault_denied':True}
    finally:
        from multiomics_tui.temp_paths import cleanup
        if 'task' in locals():
            cleanup(Path(root), task.name, task_dir=task)
        vault.delete_password(service,account)
