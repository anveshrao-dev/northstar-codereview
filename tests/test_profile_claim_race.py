"""Cross-process authority uses the same existing O_EXCL claim storage."""
import json
import os
import subprocess
import sys

import pytest
from northstar_codereview import real_source as intake
from .test_real_source import local_intake
from .test_real_review import service,captured


@pytest.mark.parametrize('name',['extended_csv','extended_bm25'])
def test_named_claim_race_and_restart(service,captured,tmp_path,name):
    permit=service.authorize_transmission(captured.manifest.sample,captured.approval_id,
        expected_digest=captured.manifest.digest,allow_transmission=True,execution_profile=name)
    worker='''import sys,socket
from pathlib import Path
from uuid import UUID,uuid4
def deny(*a,**k):raise RuntimeError('network forbidden')
socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny;socket.getaddrinfo=deny
from northstar_codereview import real_source as intake
from northstar_codereview.real_review import read_transmission
from northstar_codereview.transmission_claims import execution_scope
from northstar_codereview.real_source_contracts import IntakeRejected
intake._STORE=Path(sys.argv[1])
s=intake.load_import(UUID(sys.argv[2]))
p=read_transmission(UUID(sys.argv[2]),UUID(sys.argv[3]))
if len(sys.argv)>5:
 print('ready',flush=True);sys.stdin.readline()
try:
 with execution_scope(s,p,run_id=uuid4(),identity=('nebius',p.model),specialists=('code_quality',),execution_profile=sys.argv[4]): print('claimed')
except IntakeRejected:print('rejected')
'''
    command=[sys.executable,'-B','-c',worker,str(intake._STORE),str(captured.approval_id),str(permit.approval_id),name]
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','TMPDIR':str(tmp_path),'LANGSMITH_TRACING':'false','LANGCHAIN_TRACING_V2':'false'}
    processes=[subprocess.Popen([*command,'race'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env) for _ in range(2)]
    assert all(p.stdout.readline().strip()=='ready' for p in processes)
    for p in processes:p.stdin.write('go\n');p.stdin.flush()
    outputs=[p.communicate(timeout=30) for p in processes]
    assert all(p.returncode==0 for p in processes)
    assert sorted(o.strip() for o,_ in outputs)==['claimed','rejected']
    restarted=subprocess.run(command,capture_output=True,text=True,env=env,timeout=30)
    assert restarted.returncode==0 and restarted.stdout.strip()=='rejected'
    record=json.loads((intake._STORE/'transmission_claims'/f'{permit.approval_id}.json').read_text())
    assert record['execution_profile']['name']==name
