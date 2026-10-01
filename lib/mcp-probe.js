'use strict';

// Read-only initialize + tools/list check. Never calls an action or installs a
// runtime. Keep stdin open until discovery finishes, as a real harness does.
const { spawn, execFile } = require('child_process');

function probe(entry, { shell = false, timeout = 10000, env = process.env, cwd } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    let child, buffer = '', stderr = '', done = false;
    const secrets = Object.entries(env).filter(([key, value]) =>
      /KEY|TOKEN|SECRET|PASSWORD/i.test(key) && value && value.length >= 4
    ).map(([, value]) => value);
    const redact = (value) => secrets.reduce((text, secret) => text.split(secret).join('[redacted]'), value);
    const finish = (result) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child?.stdin?.end();
      // Give the EOF shutdown a chance, then reap only our own process tree.
      const cleanup = setTimeout(() => {
        if (child && child.exitCode === null && !child.signalCode) {
          if (process.platform === 'win32') execFile('taskkill', ['/pid', String(child.pid), '/t', '/f'], () => {});
          else child.kill('SIGTERM');
        }
      }, 500);
      cleanup.unref();
      resolve({ ...result, elapsedMs: Date.now() - started });
    };
    const fail = (message) => finish({ ok: false, error: redact(`${message}${stderr ? ` — ${stderr.trim()}` : ''}`).slice(0, 1200) });
    const timer = setTimeout(() => fail(`MCP handshake timed out after ${timeout}ms`), timeout);
    try {
      child = spawn(entry.command, entry.args || [], { shell, env, cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch (error) {
      fail(error.message);
      return;
    }
    const send = (payload) => child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...payload }) + '\n');
    child.on('error', (error) => fail(error.message));
    child.on('exit', (code, signal) => { if (!done) fail(`MCP launcher exited (${signal || code})`); });
    child.stdin.on('error', (error) => { if (!done) fail(error.message); });
    child.stderr.on('data', (chunk) => { stderr = (stderr + chunk.toString()).slice(-4000); });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      if (done) return;
      buffer += chunk;
      if (buffer.length > 1024 * 1024) return fail('MCP response exceeds 1MB');
      let newline;
      while (!done && (newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        let message;
        try { message = JSON.parse(line); } catch { fail('Non-JSON output on MCP stdout'); break; }
        if (!message || typeof message !== 'object') { fail('Invalid MCP response'); break; }
        if (message.error) { fail(message.error.message || 'MCP request failed'); break; }
        if (message.id === 1) {
          if (message.result?.protocolVersion !== '2025-06-18' || !message.result?.capabilities?.tools) {
            fail('Invalid initialize response or unsupported protocol'); break;
          }
          send({ method: 'notifications/initialized' });
          send({ id: 2, method: 'tools/list', params: {} });
        } else if (message.id === 2) {
          const tools = message.result?.tools;
          if (!Array.isArray(tools) || !tools.length || tools.some((tool) => typeof tool?.name !== 'string' || tool?.inputSchema?.type !== 'object')) {
            fail('Invalid tools/list response'); break;
          }
          finish({ ok: true, tools: tools.map((tool) => tool.name) });
        }
      }
    });
    send({ id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'jev-use-doctor', version: '1' } } });
  });
}

module.exports = { probe };
