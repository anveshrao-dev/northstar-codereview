"""Only synthetic credentials and in-memory HTTP; no live probe execution."""
import asyncio
import json
import logging

import httpx
import pytest

from northstar_codereview import structured_probe as probe
from .test_nebius import completion

ENV={'NEBIUS_API_KEY':'synthetic-test-secret','NEBIUS_MODEL':probe.MODEL}

def run(handler, **kwargs):
    calls=[]
    def respond(request):
        calls.append(request)
        return handler(request)
    result=asyncio.run(probe.run_probe(allow_live=True,environ=ENV,
        transport=httpx.MockTransport(respond),**kwargs))
    assert len(calls)==result.requests_attempted==1
    return result,calls


def test_opt_in_before_credentials_or_transport(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('must not load credentials')
    monkeypatch.setattr(probe,'load_nebius_settings',forbidden)
    result=asyncio.run(probe.run_probe())
    assert result.status=='disabled' and result.requests_attempted==0


def test_fixed_request_serialization_and_success():
    result,calls=run(lambda _:httpx.Response(200,json=completion({'content':'{"status":"ready","count":2}'})))
    body=json.loads(calls[0].content)
    assert body['response_format']=={'type':'json_object'} and body['model']==probe.MODEL
    assert body['max_tokens']==128 and body['n']==1 and body['stream'] is False
    assert 'tools' not in body and 'tool_choice' not in body
    assert len(calls[0].content)<=4096
    assert calls[0].headers['accept-encoding']=='identity'
    assert result.status=='succeeded' and result.json_valid and result.schema_valid
    assert result.finish_reason=='stop' and result.input_tokens==100 and result.output_tokens==20
    assert result.unknown_usage_attempts==0


@pytest.mark.parametrize('content,syntax,schema',[
    ('{"status":"ready","count":2}',True,True),
    ('```json\n{"status":"ready","count":2}\n```',False,False),
    ('PRIVATE synthetic-test-secret',False,False),('',False,False),
    ('{}',True,False),('[]',True,False),
    ('{"status":"ready","count":"2"}',True,False),
    ('{"status":"ready","count":2,"extra":1}',True,False),
    ('{"status":"ready","count":NaN}',False,False),
    ('{"status":"ready","count":1,"count":2}',False,False),
])
def test_local_validation_no_repair(content,syntax,schema,caplog):
    with caplog.at_level(logging.DEBUG):
        result,_=run(lambda _:httpx.Response(200,json=completion({'content':content})))
    assert result.json_valid is syntax and result.schema_valid is schema
    assert result.status==('succeeded' if schema else 'failed')
    for text in (result.model_dump_json(),caplog.text):
        assert 'PRIVATE' not in text and 'synthetic-test-secret' not in text


@pytest.mark.parametrize('status,category',[(400,'invalid_request'),(401,'authentication'),(403,'authorization'),
    (404,'model_unavailable'),(422,'invalid_request'),(429,'rate_limit'),(500,'server_error'),(503,'server_error'),(302,'http_error')])
def test_http_errors_no_retry_or_redirect(status,category,caplog):
    with caplog.at_level(logging.DEBUG):
        result,_=run(lambda _:httpx.Response(status,text='PRIVATE synthetic-test-secret',
            headers={'location':'https://example.invalid/secret'}))
    assert result.category==category and result.http_status==status
    assert result.unknown_usage_attempts==1
    assert 'PRIVATE' not in result.model_dump_json()+caplog.text
    assert 'synthetic-test-secret' not in result.model_dump_json()+caplog.text


@pytest.mark.parametrize('error,category',[(httpx.ConnectError,'connection'),(httpx.ReadTimeout,'timeout')])
def test_transport_errors_no_retry(error,category):
    def fail(request):raise error('PRIVATE synthetic-test-secret',request=request)
    result,_=run(fail)
    assert result.category==category and result.unknown_usage_attempts==1
    assert 'PRIVATE' not in result.model_dump_json()


@pytest.mark.parametrize('finish,expected',[('length','truncated'),('PRIVATE','unknown_finish'),(None,'unknown_finish')])
def test_finish_reason_is_closed_and_required_for_success(finish,expected):
    body=completion({'content':'{"status":"ready","count":2}'})
    body['choices'][0]['finish_reason']=finish
    result,_=run(lambda _:httpx.Response(200,json=body))
    assert result.category==expected and result.schema_valid and result.json_valid
    assert 'PRIVATE' not in result.model_dump_json()


def test_no_usage_is_unknown():
    result,_=run(lambda _:httpx.Response(200,json=completion({'content':'{"status":"ready","count":2}'},usage=False)))
    assert result.unknown_usage_attempts==1 and result.input_tokens is None


def test_reject_tool_response_without_execution():
    result,_=run(lambda _:httpx.Response(200,json=completion({'content':None,'tool_calls':[{'PRIVATE':'secret'}]})))
    assert result.category=='unexpected_tool_call'


@pytest.mark.parametrize('body',[b'PRIVATE',b'[]',b'x'*65537])
def test_malformed_and_oversize_wire_body(body):
    result,_=run(lambda _:httpx.Response(200,content=body))
    assert result.status=='failed' and result.category in ('protocol_error','response_limit')


def test_wrong_model_stops_before_request():
    result=asyncio.run(probe.run_probe(allow_live=True,environ={**ENV,'NEBIUS_MODEL':'different'}))
    assert result.category=='configuration' and result.requests_attempted==0


def test_total_deadline_no_retry(monkeypatch):
    monkeypatch.setattr(probe,'TIMEOUT',0.02)
    calls=[]
    async def slow(request):
        calls.append(True)
        await asyncio.sleep(1)
        raise AssertionError('deadline must cancel')
    result=asyncio.run(probe.run_probe(allow_live=True,environ=ENV,transport=httpx.MockTransport(slow)))
    assert result.category=='timeout' and result.requests_attempted==len(calls)==1


def test_cli_default_disabled_without_environment(monkeypatch,capsys):
    def forbidden(*a,**k):raise AssertionError('must not load credentials')
    monkeypatch.setattr(probe,'load_nebius_settings',forbidden)
    probe.main([])
    assert json.loads(capsys.readouterr().out)['status']=='disabled'


@pytest.mark.parametrize('option',['--mode=json_schema','--max-tokens=9999','--allow-liv','--model=PRIVATE'])
def test_cli_cannot_override_profile(option,capsys):
    with pytest.raises(SystemExit) as error:probe.main([option])
    assert error.value.code==2
    assert 'PRIVATE' not in capsys.readouterr().err


@pytest.mark.parametrize('usage',[{'prompt_tokens':True,'completion_tokens':20},
    {'prompt_tokens':'PRIVATE','completion_tokens':20}, {'prompt_tokens':100,'completion_tokens':-1}])
def test_untrusted_usage_is_not_emitted(usage):
    body=completion({'content':'{"status":"ready","count":2}'})
    body['usage']=usage
    result,_=run(lambda _:httpx.Response(200,json=body))
    assert result.unknown_usage_attempts==1 and result.input_tokens is None and result.output_tokens is None


def test_refusal_content_is_not_emitted():
    result,_=run(lambda _:httpx.Response(200,json=completion({'content':None,'refusal':'PRIVATE synthetic-test-secret'})))
    assert result.category=='refusal' and 'PRIVATE' not in result.model_dump_json()


def test_compressed_body_rejected_before_json_validation():
    import gzip
    body=json.dumps(completion({'content':'{"status":"ready","count":2}'})).encode()
    result,_=run(lambda _:httpx.Response(200,content=gzip.compress(body),headers={'content-encoding':'gzip'}))
    assert result.category=='protocol_error' and result.json_valid is None
