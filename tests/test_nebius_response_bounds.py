"""Bound the real SDK response path using in-memory, lazily consumed HTTP streams."""
import asyncio
import gzip
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from northstar_codereview.providers.nebius import NebiusModel, ProviderRequestError
from northstar_codereview.evidence_tools import build_snapshot_tools
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import ReviewRequest
from .test_nebius import settings, completion


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, wait=False):
        self.chunks, self.wait = chunks, wait
        self.yielded = 0
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        if self.wait:
            await asyncio.Event().wait()
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def wire(size=512, content='OK'):
    raw = json.dumps(completion({'content': content})).encode()
    assert len(raw) <= size
    return raw + b' ' * (size - len(raw))


async def invoke(stream, *, headers=None, status=200, timeout=1):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, stream=stream, headers=headers)
    async with NebiusModel(settings(), allow_live=True, transport=httpx.MockTransport(respond),
                           timeout_seconds=timeout) as model:
        try:
            return await model.bind_tools(()).ainvoke([HumanMessage(content='Fictional input')], max_tokens=64)
        finally:
            assert len(requests) == 1  # failures cannot cause an SDK retry


@pytest.mark.parametrize('headers', [{}, {'content-length': '1'}, {'transfer-encoding': 'chunked'},
                                     {'content-length': '131072'}, {'content-encoding': 'Identity'}])
@pytest.mark.parametrize('size', [512, 131072, 131073])
def test_actual_bytes_bound_independent_of_length_or_chunked_headers(headers, size):
    raw = wire(size)
    stream = Stream([raw[i:i+4096] for i in range(0, len(raw), 4096)])
    async def run():
        if size > 131072:
            with pytest.raises(ProviderRequestError) as error:
                await invoke(stream, headers=headers)
            assert error.value.diagnostic.category == 'response_size_limit'
        else:
            result = await invoke(stream, headers=headers)
            assert result.content == 'OK'
            assert result.response_metadata['finish_reason'] == 'stop'
            assert result.usage_metadata['input_tokens'] == 100
    asyncio.run(run())
    assert stream.closed


def test_overflow_stops_consumption_before_remaining_body():
    stream = Stream([b'x'*65536, b'x'*65536, b'x', b'must-not-be-consumed'])
    async def run():
        with pytest.raises(ProviderRequestError) as error:
            await invoke(stream)
        assert error.value.diagnostic.category == 'response_size_limit'
    asyncio.run(run())
    assert stream.yielded == 3
    assert stream.closed


def test_excessive_declared_length_rejected_without_consuming_body():
    stream = Stream([wire()])
    async def run():
        with pytest.raises(ProviderRequestError) as error:
            await invoke(stream, headers={'content-length': '131073'})
        assert error.value.diagnostic.category == 'response_size_limit'
    asyncio.run(run())
    assert stream.yielded == 0 and stream.closed


@pytest.mark.parametrize('encoding', ['gzip', 'deflate', 'br', 'zstd', 'identity, gzip', 'unknown'])
@pytest.mark.parametrize('expanded_size', [512, 131073])
def test_content_encoding_rejected_before_any_body_read_or_decompression(encoding, expanded_size):
    stream = Stream([gzip.compress(wire(expanded_size))])
    async def run():
        with pytest.raises(ProviderRequestError) as error:
            await invoke(stream, headers={'content-encoding': encoding})
        assert error.value.diagnostic.category == 'unsupported_content_encoding'
    asyncio.run(run())
    assert stream.yielded == 0 and stream.closed


@pytest.mark.parametrize('status,category', [(400,'invalid_request'), (401,'authentication_failure'),
    (403,'authorization_failure'), (429,'rate_limit'), (500,'server_error')])
def test_http_error_classification_precedes_body_and_encoding(status, category):
    stream = Stream([b'synthetic-test-secret private provider error'])
    async def run():
        with pytest.raises(ProviderRequestError) as error:
            await invoke(stream, status=status, headers={'content-encoding': 'gzip'})
        assert error.value.diagnostic.category == category
        assert error.value.diagnostic.http_status == status
        assert error.value.__context__ is None and error.value.__cause__ is None
    asyncio.run(run())
    assert stream.yielded == 0 and stream.closed


def test_body_timeout_keeps_safe_category_and_closes_stream():
    stream = Stream([httpx.ReadTimeout('synthetic-test-secret')])
    async def run():
        with pytest.raises(ProviderRequestError) as error:
            await invoke(stream)
        assert error.value.diagnostic.category == 'timeout'
        assert 'synthetic-test-secret' not in str(error.value)
    asyncio.run(run())
    assert stream.closed


def test_total_timeout_during_body_consumption():
    stream = Stream([], wait=True)
    async def run():
        with pytest.raises(ProviderRequestError) as error:
            await invoke(stream, timeout=.02)
        assert error.value.diagnostic.category == 'timeout'
    asyncio.run(run())
    assert stream.closed


def test_cancellation_during_body_consumption():
    stream = Stream([], wait=True)
    async def run():
        task = asyncio.create_task(invoke(stream))
        await stream.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert stream.closed


def test_credential_split_across_chunks_is_never_exposed(caplog):
    raw = wire(content='synthetic-test-secret')
    stream = Stream([raw[i:i+7] for i in range(0,len(raw),7)])
    async def run():
        with pytest.raises(ProviderRequestError) as error:
            await invoke(stream)
        assert error.value.diagnostic.category == 'credential_guard_rejection'
        assert 'synthetic-test-secret' not in str(error.value)
        assert error.value.__context__ is None and error.value.__cause__ is None
    asyncio.run(run())
    assert 'synthetic-test-secret' not in caplog.text


def test_streaming_limit_preserves_tool_replay_and_json_synthesis(tmp_path):
    (tmp_path/'fictional.py').write_text('fictional_count = 2\n')
    snapshot = collect_local_snapshot(ReviewRequest(mode='local',local_path='.'),source_root=tmp_path)
    tools = build_snapshot_tools(snapshot,approved_synthetic=True)
    requests=[];streams=[]
    def respond(request):
        data=json.loads(request.content);requests.append(data)
        assert request.headers['accept-encoding']=='identity'
        if len(requests)==1:
            assert 'tools' in data and 'response_format' not in data
            payload=completion({'content':None,'tool_calls':[{'id':'call-1','type':'function',
                'function':{'name':'list_snapshot_files','arguments':'{}'}}]})
        else:
            assert 'tools' not in data and 'tool_choice' not in data
            assert data['response_format']=={'type':'json_object'}
            assert data['messages'][-2]['tool_calls'][0]['id']=='call-1'
            assert data['messages'][-1]['tool_call_id']=='call-1'
            payload=completion({'content':'{"findings": []}'})
        raw=json.dumps(payload).encode();stream=Stream([raw[:20],raw[20:]]);streams.append(stream)
        return httpx.Response(200,stream=stream)
    async def run():
        async with NebiusModel(settings(),allow_live=True,transport=httpx.MockTransport(respond)) as model:
            history=[HumanMessage(content='Inspect fictional source and return JSON')]
            first=await model.bind_tools(tools).ainvoke(history,max_tokens=64)
            observation=tools[0].invoke({**first.tool_calls[0],'type':'tool_call'})
            result=await model.bind_tools(()).ainvoke([*history,first,observation],max_tokens=64,synthesis_json=True)
            assert json.loads(result.content)=={'findings':[]}
    asyncio.run(run())
    assert len(requests)==2 and all(s.closed for s in streams)
