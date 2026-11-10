"""Fixed single-user launcher and offline preflight; never grants inference authority."""
import json
import os
import sys
from contextlib import ExitStack
from importlib.metadata import version

from .local_source import PROJECT_ROOT, _directory
from .real_source import _absolute_directory
from .real_source_cli import _Parser, _InvalidArguments

_REQUIRED = ('streamlit','langgraph','langchain-core','langchain-openai','pydantic','httpx','openai')


def _storage(*, create=False):
    # Reuse descriptor-relative no-follow traversal. Do not chmod existing data.
    with _absolute_directory(PROJECT_ROOT) as root:
        for parts in (('.runtime',),('.cache','tmp')):
            with ExitStack() as stack:
                parent=root
                for part in parts:
                    if create:
                        try: os.mkdir(part,0o700,dir_fd=parent)
                        except FileExistsError: pass
                    try:
                        parent=stack.enter_context(_directory(parent,part))
                    except FileNotFoundError:
                        if not create: break  # Launcher creates missing local directories.
                        raise
                    info=os.fstat(parent)
                    if info.st_uid!=os.getuid() or info.st_mode & 0o022 or info.st_mode & 0o700 != 0o700:
                        raise ValueError('Unsafe local storage')
        if not os.access(PROJECT_ROOT,os.W_OK):
            raise ValueError('Read-only checkout')


def readiness(*, check_provider=False, portfolio_risk=False, environ=None):
    """Read-only local checks. No dotenv, provider construction, claims or writes."""
    env=os.environ if environ is None else environ
    checks={}
    checks['python']=sys.version_info[:3]==(3,12,15) and sys.prefix==str(PROJECT_ROOT/'.venv')
    checks['checkout']=all((PROJECT_ROOT/p).is_file() and not (PROJECT_ROOT/p).is_symlink()
                          for p in ('streamlit_app.py','pyproject.toml','uv.lock','.streamlit/config.toml'))
    try:
        for package in _REQUIRED: version(package)
        checks['dependencies']=True
    except Exception: checks['dependencies']=False
    try:
        _storage()
        checks['storage']=True
    except Exception: checks['storage']=False
    from .real_source_authorization import ensure_private_execution
    try:
        ensure_private_execution()
        checks['debug_disabled']=True
    except Exception: checks['debug_disabled']=False
    checks['tracing_disabled']=all(env.get(key,'false').lower() in ('false','0','')
        for key in ('LANGSMITH_TRACING','LANGCHAIN_TRACING_V2','LANGCHAIN_TRACING'))
    result=dict(application_ready=all(checks.values()),checks=checks,provider='not_checked',
                portfolio_risk='not_checked',inference_authorized=False,remote_availability='not_checked')
    if check_provider:
        from .providers.nebius import load_nebius_settings
        from .real_source_contracts import MODEL
        try:
            settings=load_nebius_settings(env)
            result['provider']='configured' if settings.model==MODEL else 'configuration_blocked'
        except Exception: result['provider']='configuration_blocked'
    if portfolio_risk:
        from .real_review import snapshot_catalog, _selected, _area
        try:
            choices=snapshot_catalog('portfolio_risk')
            choice=next(c for c in choices if c.usable)
            snapshot=_selected('portfolio_risk',choice.import_id)
            with _area(choice.import_id,'review_artifacts'):
                pass  # Existing private-directory checks; no creation or writes.
            result['portfolio_risk']=dict(status='verified',import_id=str(choice.import_id),
                snapshot_id=str(snapshot.manifest.snapshot_id),manifest_digest=choice.manifest_digest,
                retention_expiry=choice.retain_until.isoformat(),import_approval_expiry=choice.import_expires_at.isoformat(),
                file_count=len(snapshot.manifest.files),
                source_bytes=sum(len(t.encode('utf-8')) for t in snapshot.sources.values()),
                source_lines=sum(t.count('\n')+int(bool(t) and not t.endswith('\n')) for t in snapshot.sources.values()))
        except Exception: result['portfolio_risk']={'status':'blocked'}
    return result


def _environment():
    env=dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(PROJECT_ROOT/'.cache/tmp'),
               XDG_CACHE_HOME=str(PROJECT_ROOT/'.cache'),LANGSMITH_TRACING='false',
               LANGCHAIN_TRACING_V2='false',LANGCHAIN_TRACING='false')
    env.pop('OPENAI_LOG',None)
    return env


def serve():
    """Pinned installed Streamlit bootstrap, without CLI credential activation."""
    try:
        if not readiness()['application_ready']:
            raise ValueError('Local preflight failed')
        _storage(create=True)
        os.chdir(PROJECT_ROOT)
        from streamlit.web import bootstrap
        flags={'server.address':'127.0.0.1','server.port':8501,'server.headless':True,
               'server.fileWatcherType':'none','browser.gatherUsageStats':False,
               'server.enableCORS':True,'server.enableXsrfProtection':True,
               'server.enableStaticServing':False,'global.developmentMode':False,
               'browser.serverAddress':'127.0.0.1','client.showErrorDetails':'none',
               'server.sslCertFile':'','server.sslKeyFile':'','secrets.files':[],
               'logger.level':'warning'}
        bootstrap.load_config_options(flags)
        bootstrap.run(str(PROJECT_ROOT/'streamlit_app.py'),False,[],flags)
        return 0
    except Exception:
        print('{"status":"blocked","category":"local_server_failed"}')
        return 2


def main(argv=None):
    parser=_Parser(prog='local_app.py',allow_abbrev=False)
    sub=parser.add_subparsers(dest='operation',required=True)
    check=sub.add_parser('check',allow_abbrev=False)
    check.add_argument('--check-provider',action='store_true')
    check.add_argument('--portfolio-risk',action='store_true')
    sub.add_parser('start',allow_abbrev=False)
    try:
        args=parser.parse_args(argv)
        if args.operation=='check':
            result=readiness(check_provider=args.check_provider,portfolio_risk=args.portfolio_risk)
            print(json.dumps(result,sort_keys=True))
            return 0 if (result['application_ready'] and result['provider']!='configuration_blocked'
                         and result['portfolio_risk']!={'status':'blocked'}) else 2
        env=_environment()
        if not readiness(environ=env)['application_ready']:
            print('{"status":"blocked","category":"local_preflight"}')
            return 2
        _storage(create=True)
        os.chdir(PROJECT_ROOT)
        python=str(PROJECT_ROOT/'.venv/bin/python')
        args=[python,'-B','-c',
              'from northstar_codereview.local_application import serve; raise SystemExit(serve())']
        # Foreground replacement: Ctrl-C reaches Streamlit directly, no orphan wrapper.
        os.execve(python,args,env)
        return 0
    except _InvalidArguments:
        category='invalid_arguments'
    except Exception:
        category='local_startup_failed'
    print(json.dumps({'status':'blocked','category':category}))
    return 2
