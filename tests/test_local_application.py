"""Local launcher contract: no listening server, provider, or credential output."""
import importlib
import json
import pytest


def module():
    assert importlib.util.find_spec('northstar_codereview.local_application'), 'local launcher missing'
    return importlib.import_module('northstar_codereview.local_application')


def test_offline_check_does_not_read_dotenv_or_create_authority(capsys):
    app=module()
    assert app.main(['check'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['application_ready'] is True
    assert result['provider']=='not_checked'
    assert result['portfolio_risk']=='not_checked'
    assert result['inference_authorized'] is False


@pytest.mark.parametrize('env,expected', [({},False),
    ({'NEBIUS_API_KEY':'synthetic-test-secret','NEBIUS_MODEL':'Qwen/Qwen3-30B-A3B-Instruct-2507'},True),
    ({'NEBIUS_API_KEY':'PRIVATE_VALUE','NEBIUS_MODEL':'other'},False),
    ({'NEBIUS_API_KEY':'PRIVATE_VALUE','NEBIUS_MODEL':'Qwen/Qwen3-30B-A3B-Instruct-2507','NEBIUS_BASE_URL':'https://invalid.example'},False)])
def test_provider_readiness_is_configuration_only_and_redacted(env,expected):
    result=module().readiness(check_provider=True,environ=env)
    assert (result['provider']=='configured') is expected
    assert 'PRIVATE_VALUE' not in json.dumps(result) and 'synthetic-test-secret' not in json.dumps(result)
    assert result['inference_authorized'] is False


def test_launcher_forces_loopback_and_local_environment_without_printing_key(monkeypatch,tmp_path):
    app=module()
    monkeypatch.setenv('STREAMLIT_SERVER_ADDRESS','0.0.0.0')
    monkeypatch.setenv('LANGSMITH_TRACING','true')
    monkeypatch.setenv('NEBIUS_API_KEY','PRIVATE_VALUE')
    seen=[]
    def replace(path,args,env):
        seen.append((path,args,env))
    monkeypatch.setattr(app.os,'execve',replace)
    assert app.main(['start'])==0
    path,args,env=seen[0]
    assert path==str(app.PROJECT_ROOT/'.venv/bin/python')
    assert 'from northstar_codereview.local_application import serve' in args[-1]
    assert 'streamlit run' not in args[-1]
    assert env['LANGSMITH_TRACING']==env['LANGCHAIN_TRACING_V2']=='false'
    assert env['TMPDIR']==str(app.PROJECT_ROOT/'.cache/tmp')
    assert env['NEBIUS_API_KEY']=='PRIVATE_VALUE'


def test_unknown_cli_arguments_are_rejected_without_echo(capsys):
    assert module().main(['start','--host','PRIVATE_VALUE'])==2
    assert 'PRIVATE_VALUE' not in capsys.readouterr().out


def test_storage_symlink_blocks_start(monkeypatch,tmp_path):
    app=module()
    (tmp_path/'.runtime').symlink_to(tmp_path,target_is_directory=True)
    monkeypatch.setattr(app,'PROJECT_ROOT',tmp_path)
    monkeypatch.setattr(app.os,'execve',lambda *a:pytest.fail('server started'))
    assert app.readiness()['checks']['storage'] is False
    assert app.main(['start'])==2


def test_bootstrap_bypasses_streamlit_credential_activation_and_pins_flags(monkeypatch):
    app=module()
    from streamlit.web import bootstrap, cli
    monkeypatch.setattr(cli,'check_credentials',lambda:pytest.fail('global credential activation'))
    loaded=[];started=[]
    monkeypatch.setattr(bootstrap,'load_config_options',lambda flags:loaded.append(flags))
    def run(path,is_hello,args,flags):
        assert loaded==[flags]
        started.append((path,flags))
    monkeypatch.setattr(bootstrap,'run',run)
    assert app.serve()==0
    assert len(started)==1
    flags=started[0][1]
    assert flags['server.address']=='127.0.0.1'
    assert flags['server.headless'] is True and flags['browser.gatherUsageStats'] is False
    assert flags['server.enableXsrfProtection'] is True


def test_unwritable_storage_is_not_ready(monkeypatch,tmp_path):
    app=module()
    folder=tmp_path/'.runtime';folder.mkdir(mode=0o500)
    monkeypatch.setattr(app,'PROJECT_ROOT',tmp_path)
    try: assert app.readiness()['checks']['storage'] is False
    finally: folder.chmod(0o700)


def test_bootstrap_flags_are_accepted_by_installed_streamlit(monkeypatch):
    app=module()
    from streamlit import config
    from streamlit.web import bootstrap
    monkeypatch.setattr(config,'get_config_files',lambda name:[str(app.PROJECT_ROOT/'.streamlit'/name)])
    monkeypatch.setattr(config,'_config_options',None)
    monkeypatch.setenv('STREAMLIT_SERVER_ADDRESS','0.0.0.0')
    monkeypatch.setenv('STREAMLIT_BROWSER_GATHER_USAGE_STATS','true')
    calls=[]
    def run(*args):
        calls.append(True)
        assert config.get_option('server.address')=='127.0.0.1'
        assert config.get_option('server.port')==8501
        assert config.get_option('browser.gatherUsageStats') is False
        assert config.get_option('secrets.files')==[]
        assert config.get_option('server.enableStaticServing') is False
    monkeypatch.setattr(bootstrap,'run',run)
    assert app.serve()==0 and calls==[True]


def test_tracing_environment_blocks_readiness():
    result=module().readiness(environ={'LANGSMITH_TRACING':'true'})
    assert result['application_ready'] is False and result['checks']['tracing_disabled'] is False


from .test_real_review import captured
from .test_real_source import local_intake


def test_portfolio_readiness_verifies_generated_snapshot_without_new_authority(captured):
    from northstar_codereview import real_source as intake
    before={p.relative_to(intake._STORE):p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    result=module().readiness(portfolio_risk=True,environ={})
    assert result['portfolio_risk']['status']=='verified'
    assert result['portfolio_risk']['file_count']==2 and result['portfolio_risk']['source_lines']==2
    assert result['portfolio_risk']['snapshot_id']==str(captured.manifest.snapshot.snapshot_id)
    after={p.relative_to(intake._STORE):p.read_bytes() for p in intake._STORE.rglob('*') if p.is_file()}
    assert after==before and result['inference_authorized'] is False
    assert 'value = 1' not in json.dumps(result)


def test_missing_portfolio_does_not_report_ready(local_intake):
    result=module().readiness(portfolio_risk=True,environ={})
    assert result['portfolio_risk']=={'status':'blocked'}


def test_retention_expiry_blocks_portfolio_readiness(captured,monkeypatch):
    from northstar_codereview import real_source_contracts as contracts
    from datetime import datetime,timedelta
    class Later(datetime):
        @classmethod
        def now(cls,tz=None): return captured.manifest.retain_until+timedelta(seconds=1)
    monkeypatch.setattr(contracts,'datetime',Later)
    assert module().readiness(portfolio_risk=True,environ={})['portfolio_risk']=={'status':'blocked'}


def test_unwritable_private_reports_block_portfolio_readiness(captured):
    from northstar_codereview import real_source as intake
    folder=intake._STORE/'imports'/str(captured.approval_id)/'review_artifacts'
    folder.chmod(0o500)
    try:
        assert module().readiness(portfolio_risk=True,environ={})['portfolio_risk']=={'status':'blocked'}
    finally: folder.chmod(0o700)
