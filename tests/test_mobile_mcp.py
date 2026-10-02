"""Exercise the Android MCP entry point and serialized request behavior."""
import json
import threading
from types import SimpleNamespace

import pytest

from jev_use import android, mcp_server


@pytest.mark.parametrize('options,steps,settle', [({}, mcp_server.DEFAULT_MAX_STEPS, android.DEFAULT_SETTLE), ({'max_steps': 3, 'settle': 0.2}, 3, 0.2)])
def test_android_mcp_passes_validated_options(monkeypatch, options, steps, settle):
    calls = []
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: SimpleNamespace(serial='fixture-device'))
    monkeypatch.setattr(mcp_server, 'JevChooser', lambda: object())
    monkeypatch.setattr(mcp_server, 'TextModel', lambda: object())
    def run(serial, goal, chooser, **kwargs):
        calls.append(kwargs)
        return android.Result(goal=goal)
    monkeypatch.setattr(android, 'run', run)
    response = mcp_server.handle({'jsonrpc':'2.0', 'id':1, 'method':'tools/call', 'params':{'name':'android_use','arguments':{'goal':'open account menu', 'act':True, 'use_cache':False, **options}}})
    assert not response['result'].get('isError'), response
    assert calls[0]['max_steps'] == steps
    assert calls[0]['settle'] == settle
    assert calls[0]['act'] is True


@pytest.mark.parametrize('options', [{'max_steps':0}, {'max_steps':21}, {'settle':3000}, {'settle':float('nan')}])
def test_android_invalid_bounds_fail_before_device_access(monkeypatch, options):
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: pytest.fail('must validate before device access'))
    with pytest.raises(ValueError):
        mcp_server.tool_android_use({'goal':'open account menu', **options})


def test_busy_response_identifies_original_call_and_does_not_start_new_action(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    responses = []
    calls = []
    def handle(request):
        calls.append(request['id'])
        started.set()
        assert release.wait(3)
        return {'jsonrpc':'2.0', 'id':request['id'], 'result':{'content':[]}}
    def send(response):
        responses.append(response)
        if response['id'] == 1:
            completed.set()
    def incoming():
        yield json.dumps({'id':1,'method':'tools/call','params':{'name':'android_location'}})
        assert started.wait(3)
        yield json.dumps({'id':2,'method':'tools/call','params':{'name':'android_use'}})
        release.set()
        assert completed.wait(3)
    monkeypatch.setattr(mcp_server, 'handle', handle)
    monkeypatch.setattr(mcp_server, 'send', send)
    monkeypatch.setattr(mcp_server, 'load_env', lambda: None)
    monkeypatch.setattr(mcp_server.sys, 'stdin', incoming())
    monkeypatch.setattr(mcp_server.betterwright, 'close_sessions', lambda: None)
    assert mcp_server.main() == 0
    assert calls == [1]
    busy = next(response for response in responses if response['id'] == 2)['result']
    assert busy['isError']
    text = busy['content'][0]['text']
    assert 'Request not started' in text
    assert 'android_location (request ID 1)' in text
    assert any(response['id'] == 1 and not response['result'].get('isError') for response in responses)
