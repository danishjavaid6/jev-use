import pytest
from jev_use import mcp_server, betterwright


@pytest.mark.parametrize('settle', [5000, 6000, -1, float('nan'), float('inf')])
def test_millisecond_wait_is_rejected_before_browser_access(monkeypatch, settle):
    monkeypatch.setattr(mcp_server, 'with_browser_session', lambda *a: pytest.fail('browser should not be touched'))
    result = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'browser_use', 'arguments': {'goal': 'login', 'settle': settle}}})
    assert result['result']['isError']
    assert 'seconds' in result['result']['content'][0]['text']


def test_script_failure_is_mcp_error_without_replay(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, 'reset_browser_session', lambda: None)
    monkeypatch.setattr(mcp_server, '_SESSION', {'target': None})
    def run(*a, **k):
        calls.append(1)
        return {'ok': False, 'error': 'locator timed out; submission may have committed'}
    monkeypatch.setattr(betterwright, 'run_script', run)
    result = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'browser_script', 'arguments': {'port': 1234, 'code': 'return 1'}}})
    assert result['result']['isError']
    assert len(calls) == 1


def test_server_answers_ping_and_refuses_overlapping_actions(monkeypatch):
    import json
    import threading
    started = threading.Event()
    finish = threading.Event()
    outputs = []
    def slow(arguments):
        started.set()
        finish.wait(3)
        return 'done'
    monkeypatch.setitem(mcp_server.HANDLERS, 'fixture_slow', slow)
    monkeypatch.setattr(mcp_server, 'send', outputs.append)
    monkeypatch.setattr(mcp_server, 'load_env', lambda: None)
    monkeypatch.setattr(betterwright, 'close_sessions', lambda: None)
    def input_lines():
        yield json.dumps({'id': 1, 'method': 'tools/call', 'params': {'name': 'fixture_slow'}})
        assert started.wait(1)
        yield json.dumps({'id': 2, 'method': 'ping'})
        assert any(r['id'] == 2 for r in outputs), 'ping blocked behind tool'
        yield json.dumps({'id': 3, 'method': 'tools/call', 'params': {'name': 'fixture_slow'}})
        assert next(r for r in outputs if r['id'] == 3)['result']['isError']
        finish.set()
    monkeypatch.setattr(mcp_server.sys, 'stdin', input_lines())
    assert mcp_server.main() == 0
