"""One receiving-machine entry point, with no API calls or environment repair."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import sys
from .doctor import run_check


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project-root',type=Path,required=True)
    root=parser.parse_args().project_root.resolve()
    (root/'.runtime').mkdir(exist_ok=True)
    report={'schema_version':1,'status':'failed','platform':platform.platform(),
            'machine':platform.machine(),'python':sys.version.split()[0],'stages':{},
            'real_api_tested':False}
    env=dict(os.environ);env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
    try:
        for label,module in [('doctor','native_dist.doctor'),('synthetic_task','native_dist.v8_acceptance'),
                             ('lifecycle','native_dist.acceptance')]:
            print('Checking '+label,flush=True)
            run_check(label,[sys.executable,'-B','-m',module,'--project-root',root],report,env=env,cwd=root,timeout=600)
        report.update(status='passed',phase='complete')
    except Exception as exc:
        from multiomics_tui.model_config import redact
        report.update(error_type=type(exc).__name__,error=redact(str(exc)))
    path=root/'.runtime/native_diagnostic.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('诊断结果：'+report['status']+'；记录：'+str(path))
    print('回传 native_diagnostic.json、doctor.json、install_report.json 和 install.log；Mac 另附 macos_diagnostic.json。不要发送密钥或整个 .runtime。')
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
