"""A lost response cannot tear down an in-flight workflow or replay Create."""
import json
import threading
import pytest

from jev_use import mcp_server as server


@pytest.mark.parametrize("read_error", [False, True])
def test_eof_pauses_then_drains_action_before_saving_profile(monkeypatch, read_error):
    started = threading.Event()
    paused = threading.Event()
    events = []
    monkeypatch.setattr(server, "load_env", lambda: None)
    monkeypatch.setattr(server, "_GOLOGIN", {"run_id": "fixture"})
    monkeypatch.setattr(server.facebook_task, "forget", lambda run: events.append("forget"))

    def pause(run, action):
        assert (run, action) == ("fixture", "pause")
        events.append("pause")
        paused.set()

    def handle(request):
        events.append("action_started")
        started.set()
        assert paused.wait(3)
        events.append("journal_committed")
        return {"id": request["id"], "result": {}}

    def send(response):
        raise BrokenPipeError("closed client")

    def close(args):
        assert "journal_committed" in events
        events.append("profile_saved")

    def incoming():
        yield json.dumps({"id": 1, "method": "tools/call"})
        assert started.wait(3)
        # EOF while action is still running.
        if read_error:
            raise OSError("input transport lost")

    monkeypatch.setattr(server.facebook_runs, "transition", pause)
    monkeypatch.setattr(server, "handle", handle)
    monkeypatch.setattr(server, "send", send)
    monkeypatch.setattr(server, "tool_browser_close", close)
    monkeypatch.setattr(server.sys, "stdin", incoming())
    if read_error:
        with pytest.raises(OSError, match="input transport lost"):
            server.main()
    else:
        assert server.main() == 0
    assert events.index("pause") < events.index("journal_committed") < events.index("profile_saved")
    assert events.count("action_started") == 1


def test_save_failure_is_reported_without_leaking_backend_payload(monkeypatch, capsys):
    def fail(args):
        raise RuntimeError("PRIVATE_BACKEND_PAYLOAD")
    monkeypatch.setattr(server, "tool_browser_close", fail)
    server._close_disconnected_profile()
    output = capsys.readouterr().err
    assert "closure is unconfirmed" in output
    assert "PRIVATE_BACKEND_PAYLOAD" not in output
