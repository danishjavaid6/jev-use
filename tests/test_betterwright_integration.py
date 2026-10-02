"""Opt-in actual SDK/CDP regression, using an isolated local page and browser."""
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from jev_use import betterwright

HTML = b'''<button id="account" onclick="setTimeout(()=>document.getElementById('login').hidden=false,100)">Test account</button>
<div id="login" role="dialog" hidden><label>Password<input id="pass" type="password"></label>
<button onclick="if(document.getElementById('pass').value==='fixture-only'){document.getElementById('done').textContent='Logged in';document.getElementById('login').hidden=true;alert('Welcome')}">Log in</button></div><div id="done"></div><div role="button" onclick="window.createClicks=(window.createClicks||0)+1;setTimeout(()=>document.getElementById('created').textContent=&quot;Success! You've created Fixture Page&quot;,250)">Create Page</div><div id="created"></div>'''


@pytest.mark.skipif(not os.environ.get('JEV_TEST_CHROME'), reason='set JEV_TEST_CHROME for actual SDK/CDP test')
def test_persistent_sdk_login_alert_and_hard_timeout(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(HTML)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        monkeypatch.setenv("JEV_USE_WORKFLOW_DIR", os.path.join(folder, "journal"))
        browser = subprocess.Popen([os.environ['JEV_TEST_CHROME'], '--headless=new', '--no-sandbox',
            f'--user-data-dir={folder}', f'--remote-debugging-port={port}', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    betterwright.ws_url_for(port, timeout=.1)
                    break
                except betterwright.BetterWrightError:
                    time.sleep(.05)
            url = f'http://127.0.0.1:{server.server_port}/'
            result = betterwright.run_script(port, f"await workflow.navigate({json.dumps(url)}); state.marker=42; return page.url()", timeout=30)
            assert result['ok'], result
            runner = betterwright.run_script(port, 'return {version:facebookPages.version, identity:await facebookPages.activeAccountId()}', timeout=30)
            assert runner['ok'], str(runner.get('error'))
            assert runner['result'] == {'version':'2', 'identity':None}
            pid = betterwright._BRIDGES[port][1].process.pid
            result = betterwright.run_script(port, "await page.getByRole('button',{name:'Test account',exact:true}).click(); await page.locator('#pass').fill('fixture-only'); await dialogs.acceptNext(); await page.getByRole('button',{name:'Log in',exact:true}).click(); await page.locator('#done').getByText('Logged in',{exact:true}).waitFor(); return state.marker", timeout=30)
            assert result['ok'], result
            assert result['result'] == 42
            assert betterwright._BRIDGES[port][1].process.pid == pid
            # A submission reservation survives bridge teardown, including timeouts.
            reserve = betterwright.run_script(port, "workflow.begin('fixture-run','fixture-account'); workflow.status().browsed=true; return workflow.beforeCreate('Fixture Page')", timeout=30)
            assert reserve['ok'], reserve
            assert reserve['result']['stage'] == 'submission_reserved'
            # Inspection does not consume submission permission; invalid selectors are safe.
            inspection = betterwright.run_script(port, "return workflow.status().stage", timeout=30)
            assert inspection['result'] == 'submission_reserved'
            with pytest.raises(betterwright.BetterWrightError):
                betterwright.run_script(port, 'return 1', timeout=30, submission={"run_id":"fixture-run", "account":"fixture-account", "selector":"#missing"})
            restored = betterwright.run_script(port, "workflow.begin('fixture-run','fixture-account'); return workflow.status().stage", timeout=30)
            assert restored['result'] == 'submission_reserved', restored
            created = betterwright.run_script(port, "return await workflow.confirmCreated()", timeout=30, submission={"run_id":"fixture-run", "account":"fixture-account"})
            assert created['ok'], created
            assert created['result']['stage'] == 'created'
            clicks = betterwright.run_script(port, 'return await page.evaluate(()=>window.createClicks)', timeout=30)
            assert clicks['result'] == 1
            with pytest.raises(betterwright.BetterWrightError):
                betterwright.run_script(port, 'return 1', timeout=30, submission={"run_id":"fixture-run", "account":"fixture-account"})
            reserve = betterwright.run_script(port, "workflow.begin('timeout-run','fixture-account'); workflow.status().browsed=true; return workflow.beforeCreate('Timeout Page')", timeout=30)
            assert reserve['ok'], reserve
            # A second CDP client is supported: do not diagnose single-websocket ownership.
            from jev_use.harness import Harness
            harness = Harness(port=port)
            harness.start()
            try:
                assert 'Logged in' in harness.evaluate('document.body.innerText')
            finally:
                harness.close()
            started = time.monotonic()
            with pytest.raises(betterwright.BetterWrightError, match='deadline'):
                betterwright.run_script(port, "await page.locator('#never').waitFor({timeout:60000})", timeout=1, submission={"run_id":"timeout-run", "account":"fixture-account", "selector":"#account"})
            assert time.monotonic() - started < 8
            assert port not in betterwright._BRIDGES
            assert betterwright.ws_url_for(port)  # Browser survives a failed script.
            result = betterwright.run_script(port, 'return page.url()', timeout=30)
            assert result['ok'], result
            assert result['result'] == url
            result = betterwright.run_script(port, "workflow.begin('timeout-run','fixture-account'); await workflow.submitCreation({selector:'#account'}); return 'must not run'", timeout=30)
            assert not result['ok']
            assert 'only be attempted once' in result['error']
            assert any(item['stage'] == 'submitting' for item in result['workflowCheckpoints'].values())
            harness = Harness(port=port)
            harness.start()
            try:
                harness.open_tab('about:blank')
            finally:
                harness.close()
            # The selected page persists even if another tab appears.
            result = betterwright.run_script(port, 'return page.url()', timeout=30)
            assert result['ok'] and result['result'] == url, result
            betterwright.close_sessions()
            with pytest.raises(betterwright.BetterWrightError, match='tabs'):
                betterwright.run_script(port, 'return page.url()', timeout=30)
            result = betterwright.run_script(port, 'return page.url()', timeout=30, page_url=url)
            assert result['ok'] and result['result'] == url, result
        finally:
            betterwright.close_sessions()
            browser.terminate()
            browser.wait(timeout=10)
            server.shutdown()
            server.server_close()
