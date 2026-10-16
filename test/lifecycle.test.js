'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { test, beforeEach, afterEach } = require('node:test');
const paths = require('../lib/paths');
const lifecycle = require('../lib/lifecycle');
const installer = require('../lib/installer');
// Resolve before the tests redirect stateDir to an empty installation. A
// delivery worktree can legitimately use the machine's installed runtime.
const installedPython = require('../lib/runtime').resolve();

let dir, original;
beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-lifecycle-'));
  original = paths.stateDir;
  paths.stateDir = () => dir;
});
afterEach(() => {
  paths.stateDir = original;
  fs.rmSync(dir, { recursive: true, force: true });
});

test('connected session prevents installation before any credentials/runtime/config changes', async () => {
  const release = lifecycle.beginSession();
  try {
    await assert.rejects(installer.install({ force: true, key: 'PRIVATE_FIXTURE' }), /MCP session is connected/);
    assert.deepEqual(fs.readdirSync(dir), ['mcp-lifecycle']);
    assert.equal(fs.readdirSync(path.join(dir, 'mcp-lifecycle')).length, 1);
  } finally { release(); }
  const finish = lifecycle.beginMaintenance();
  assert.throws(() => lifecycle.beginSession(), /Installation is in progress/);
  assert.throws(() => lifecycle.beginMaintenance(), /Another installation/);
  finish();
  lifecycle.beginSession()();
});

test('postinstall leaves an existing installation byte-for-byte intact', async () => {
  for (const name of ['installed.json', '.env', 'opencode.json', 'SKILL.md']) fs.writeFileSync(path.join(dir, name), 'keep-' + name);
  const before = Object.fromEntries(fs.readdirSync(dir).map(name => [name, fs.readFileSync(path.join(dir, name), 'utf8')]));
  const result = await installer.install({ auto: true, force: true, key: 'PRIVATE_FIXTURE', gologinToken: 'PRIVATE_TOKEN' });
  assert.equal(result.deferred, true);
  assert.deepEqual(Object.fromEntries(fs.readdirSync(dir).map(name => [name, fs.readFileSync(path.join(dir, name), 'utf8')])), before);
});

test('session guard works across processes/checkouts and reclaims a dead launcher record', async () => {
  const code = `const paths=require(${JSON.stringify(require.resolve('../lib/paths'))});paths.stateDir=()=>process.argv[1];require(${JSON.stringify(require.resolve('../lib/lifecycle'))}).beginSession();process.stdout.write('ready\\n');setInterval(()=>{},1000);`;
  const child = spawn(process.execPath, ['-e', code, dir], { stdio: ['ignore', 'pipe', 'pipe'] });
  try {
    await once(child.stdout, 'data');
    assert.throws(() => lifecycle.beginMaintenance(), /MCP session is connected/);
  } finally {
    const exited = once(child, 'exit');
    child.kill();
    await exited;
  }
  lifecycle.beginMaintenance()();
  assert.deepEqual(fs.readdirSync(path.join(dir, 'mcp-lifecycle')), []);
});

test('actual Node/Python launcher owns its lifecycle record until graceful EOF', { timeout: 15000 }, async (t) => {
  if (!installedPython) return t.skip('Python runtime not installed');
  const code = `const paths=require(${JSON.stringify(require.resolve('../lib/paths'))});paths.stateDir=()=>process.argv[1];require(${JSON.stringify(require.resolve('../lib/runtime'))}).resolve=()=>${JSON.stringify(installedPython)};require(${JSON.stringify(require.resolve('../bin/jev-use-mcp.js'))});`;
  const child = spawn(process.execPath, ['-e', code, dir], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const exited = once(child, 'exit');
  try {
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'ping' }) + '\n');
    const [chunk] = await once(child.stdout, 'data');
    assert.equal(JSON.parse(chunk.toString()).id, 1, stderr);
    assert.throws(() => lifecycle.beginMaintenance(), /MCP session is connected/);
    child.stdin.end();
    const [code] = await exited;
    assert.equal(code, 0, stderr);
    assert.deepEqual(fs.readdirSync(path.join(dir, 'mcp-lifecycle')), []);
    lifecycle.beginMaintenance()();
  } finally {
    if (child.exitCode === null && !child.signalCode) { child.kill(); await exited; }
  }
});
