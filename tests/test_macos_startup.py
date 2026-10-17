"""Literal-root startup permission and privacy-preserving Mac diagnostics."""
import json
import time
from pathlib import Path

from multiomics_tui.macos_guard import ROOT_DIRECTORY_READ, profile
from native_dist.macos_startup import root_read_ab, parse_crash, summarize_crash, crash_summaries


def test_root_rule_is_literal_readonly_and_keeps_sensitive_sysctls_denied(tmp_path):
    task=tmp_path/'task';task.mkdir()
    policy=profile(task,[])
    assert policy.splitlines().count(ROOT_DIRECTORY_READ)==1
    assert '(subpath "/")' not in policy and '(allow default)' not in policy
    assert '(deny network*)' in policy and '(deny sysctl*)' in policy
    assert 'security.mac.lockdown_mode_state' not in policy and 'kern.bootargs' not in policy
    # Cross-process argument inspection stays denied.
    assert 'kern.procargs' not in policy
    # Keychain services are reachable by mach-lookup only; they must never gain
    # any file read/write grant (the stored item stays protected by its own ACL).
    for line in policy.splitlines():
        if 'securityd' in line or 'trustd' in line:
            assert 'mach-lookup' in line and 'file-read' not in line and 'file-write' not in line
    assert not any('file-write' in line and '(literal "/")' in line for line in policy.splitlines())


def test_ab_profiles_differ_only_in_literal_root_rule(tmp_path):
    task=tmp_path/'task';task.mkdir();policy=profile(task,[]);calls=[]
    def collect(label,argv,**kwargs):
        calls.append(argv)
        return {'pid':len(calls),'returncode':0 if ROOT_DIRECTORY_READ in argv[2] else -6}
    result=root_read_ab(policy,{'python':['/fixture/python','-c','print(1)'],'r':['/fixture/Rscript','--vanilla','-e','cat(1)']},{},task,collect)
    assert len(calls)==4
    for i in [0,2]:
        a,b=calls[i],calls[i+1]
        assert a[:2]==b[:2] and a[3:]==b[3:]
        assert b[2].replace(ROOT_DIRECTORY_READ+'\n','')==a[2]
    assert all(r['root_allowance_isolates_startup_failure'] for r in result.values())


def crash(pid):
    return {'pid':pid,'procName':'python3.14','exception':{'type':'EXC_CRASH','signal':'SIGABRT'},
        'termination':{'namespace':'SIGNAL','code':6},'faultingThread':0,
        'threads':[{'triggered':True,'frames':[{'symbol':'abort','imageIndex':0}], 'threadState':{'secret':'do not collect'}}],
        'usedImages':[{'name':'libsystem_c.dylib','path':'unrelated private path'}],
        'environment':{'API_KEY':'do not collect'},'asi':{'libsystem_c.dylib':['abort() called'],'other_app':['do not collect']}}


def test_ips_two_json_objects_and_summary_excludes_private_fields():
    body=crash(42)
    parsed=parse_crash(json.dumps({'app_name':'python'})+'\n'+json.dumps(body))
    summary=summarize_crash(parsed)
    text=json.dumps(summary)
    assert summary['pid']==42 and summary['frames'][0]['symbol']=='abort'
    assert 'do not collect' not in text and 'unrelated private path' not in text
    assert summary['exception']['signal']=='SIGABRT'


def test_crash_collection_only_emits_matching_child_pid(tmp_path):
    (tmp_path/'python-child.ips').write_text(json.dumps(crash(42)))
    (tmp_path/'python-other.ips').write_text(json.dumps(crash(99)))
    (tmp_path/'OtherApp.ips').write_text(json.dumps(crash(42)))
    result=crash_summaries({42},time.time()-1,[tmp_path])
    assert result['status']=='found' and len(result['summaries'])==1
    assert result['summaries'][0]['pid']==42


def test_crash_collection_reports_unavailable_without_claiming_success(tmp_path):
    result=crash_summaries({42},time.time(),[tmp_path])
    assert result['status']=='not_available_yet' and result['summaries']==[]
