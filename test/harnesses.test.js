'use strict';

/**
 * Harness adapters.
 *
 * The thing worth testing here is not "does it write an entry" but "does it
 * leave everyone else's config alone". Every one of these files is shared with
 * other MCP servers and hand-written settings, so a clobbering bug is both easy
 * to write and expensive.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test, beforeEach, afterEach } = require('node:test');

const paths = require('../lib/paths');
const commandcode = require('../lib/harnesses/commandcode');
const claude = require('../lib/harnesses/claude');
const codex = require('../lib/harnesses/codex');
const cursor = require('../lib/harnesses/cursor');
const opencode = require('../lib/harnesses/opencode');

const ENTRY = { command: '/usr/bin/node', args: ['/opt/jev-use/bin/jev-use-mcp.js'] };

let home;
let originalHome;

beforeEach(() => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-home-'));
  originalHome = paths.HOME;
  paths.HOME = home;
  delete process.env.XDG_CONFIG_HOME;
});

afterEach(() => {
  paths.HOME = originalHome;
  fs.rmSync(home, { recursive: true, force: true });
});

function write(file, contents) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, contents);
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

// -- commandcode ------------------------------------------------------------

test('commandcode adds the transport and enabled keys its schema needs', () => {
  fs.mkdirSync(path.join(home, '.commandcode'), { recursive: true });
  const file = commandcode.register(ENTRY);
  const entry = readJson(file).mcpServers['jev-use'];
  assert.equal(entry.transport, 'stdio');
  assert.equal(entry.enabled, true);
  assert.equal(entry.command, ENTRY.command);
  assert.deepEqual(entry.args, ENTRY.args);
});

test('commandcode preserves other servers already configured', () => {
  const file = path.join(home, '.commandcode', 'mcp.json');
  write(
    file,
    JSON.stringify(
      { mcpServers: { 'cua-driver': { transport: 'stdio', enabled: true, command: '/bin/cua' } } },
      null,
      2
    )
  );
  commandcode.register(ENTRY);
  const config = readJson(file);
  assert.ok(config.mcpServers['cua-driver'], 'the existing server is still there');
  assert.ok(config.mcpServers['jev-use'], 'and ours was added');
});

// -- claude + cursor --------------------------------------------------------

test('claude writes a plain mcpServers entry', () => {
  fs.mkdirSync(path.join(home, '.claude'), { recursive: true });
  const file = claude.register(ENTRY);
  const entry = readJson(file).mcpServers['jev-use'];
  assert.equal(entry.command, ENTRY.command);
  assert.deepEqual(entry.args, ENTRY.args);
  assert.equal(entry.transport, undefined, 'Claude does not take the extra keys');
});

test('claude keeps the unrelated top-level settings in .claude.json', () => {
  const file = path.join(home, '.claude.json');
  write(file, JSON.stringify({ numStartups: 42, mcpServers: {} }));
  claude.register(ENTRY);
  const config = readJson(file);
  assert.equal(config.numStartups, 42, 'claude.json holds far more than MCP servers');
  assert.ok(config.mcpServers['jev-use']);
});

test('cursor writes the global mcp.json', () => {
  fs.mkdirSync(path.join(home, '.cursor'), { recursive: true });
  const file = cursor.register(ENTRY);
  assert.equal(file, path.join(home, '.cursor', 'mcp.json'));
  assert.ok(readJson(file).mcpServers['jev-use']);
});

test('a re-register replaces our entry instead of duplicating it', () => {
  fs.mkdirSync(path.join(home, '.cursor'), { recursive: true });
  cursor.register(ENTRY);
  cursor.register({ command: '/other/node', args: ['/other/mcp.js'] });
  const servers = Object.keys(readJson(cursor.configFile()).mcpServers);
  assert.deepEqual(servers, ['jev-use']);
  assert.equal(readJson(cursor.configFile()).mcpServers['jev-use'].command, '/other/node');
});

test('the first write leaves a backup of the original', () => {
  const file = path.join(home, '.cursor', 'mcp.json');
  write(file, JSON.stringify({ mcpServers: { other: { command: 'x' } } }));
  cursor.register(ENTRY);
  const backup = `${file}.jev-use.bak`;
  assert.ok(fs.existsSync(backup), 'a wrong write must be one cp away from fixed');
  assert.ok(JSON.parse(fs.readFileSync(backup, 'utf8')).mcpServers.other);
});

// -- codex (TOML) -----------------------------------------------------------

test('codex writes a block and leaves the rest of config.toml byte-identical', () => {
  const file = path.join(home, '.codex', 'config.toml');
  const original = [
    'approvals_reviewer = "user"',
    'model = "gpt-5.6-sol"',
    '',
    '[model_providers.tunly]',
    'name = "Tunly CLI Proxy"',
    'base_url = "https://ai.tunly.cloud/v1"',
    '',
    '[projects."/home/me"]',
    'trust_level = "trusted"',
    '',
  ].join('\n');
  write(file, original);

  codex.register(ENTRY);
  const text = fs.readFileSync(file, 'utf8');

  assert.ok(text.includes('model = "gpt-5.6-sol"'), 'top-level keys survive');
  assert.ok(text.includes('[model_providers.tunly]'), 'other tables survive');
  assert.ok(text.includes('[projects."/home/me"]'), 'quoted table names survive');
  assert.ok(text.includes('[mcp_servers.jev-use]'));
  assert.ok(text.includes('command = "/usr/bin/node"'));
  assert.ok(text.includes('args = ["/opt/jev-use/bin/jev-use-mcp.js"]'));
  assert.ok(text.includes('startup_timeout_sec = 120'));
  assert.equal(text.match(/\[mcp_servers\.jev-use\]/g).length, 1);
});

test('codex replaces its own block on a second run rather than appending', () => {
  const file = path.join(home, '.codex', 'config.toml');
  write(file, 'model = "x"\n');
  codex.register(ENTRY);
  codex.register({ command: '/second/node', args: ['/second/mcp.js'] });

  const text = fs.readFileSync(file, 'utf8');
  assert.equal(text.match(/\[mcp_servers\.jev-use\]/g).length, 1);
  assert.ok(text.includes('/second/node'));
  assert.ok(!text.includes('/usr/bin/node'), 'the old block is gone, not duplicated');
  assert.equal(text.match(/model = "x"/g).length, 1, 'and the rest is untouched');
});

test('codex removes a nested env table along with the block it belongs to', () => {
  const file = path.join(home, '.codex', 'config.toml');
  write(
    file,
    [
      '[mcp_servers.jev-use]',
      'command = "/old/node"',
      '',
      '[mcp_servers.jev-use.env]',
      'OLD = "1"',
      '',
      '[mcp_servers.other]',
      'command = "/keep/node"',
      '',
    ].join('\n')
  );

  codex.register(ENTRY);
  const text = fs.readFileSync(file, 'utf8');

  assert.ok(!text.includes('/old/node'), 'the stale block is replaced');
  assert.ok(!text.includes('OLD = "1"'), 'its nested table goes too, or the TOML is ambiguous');
  assert.ok(text.includes('[mcp_servers.other]'), 'a different server is untouched');
  assert.ok(text.includes('/keep/node'));
});

test('codex does not touch a server whose name merely starts with ours', () => {
  const file = path.join(home, '.codex', 'config.toml');
  write(file, '[mcp_servers.jev-use-extra]\ncommand = "/keep/me"\n');
  codex.register(ENTRY);
  const text = fs.readFileSync(file, 'utf8');
  assert.ok(text.includes('[mcp_servers.jev-use-extra]'));
  assert.ok(text.includes('/keep/me'));
  assert.ok(text.includes('[mcp_servers.jev-use]'));
});

// -- opencode ---------------------------------------------------------------

test('opencode uses its own shape: command array plus environment', () => {
  fs.mkdirSync(path.join(home, '.config', 'opencode'), { recursive: true });
  const file = opencode.register({
    command: '/usr/bin/node',
    args: ['/opt/mcp.js'],
    env: { FOO: 'bar' },
  });
  const entry = readJson(file).mcp['jev-use'];
  assert.equal(entry.type, 'local');
  assert.deepEqual(entry.command, ['/usr/bin/node', '/opt/mcp.js']);
  assert.equal(entry.enabled, true);
  assert.deepEqual(entry.environment, { FOO: 'bar' });
});

test('opencode preserves other mcp entries and other config blocks', () => {
  const dir = path.join(home, '.config', 'opencode');
  const file = path.join(dir, 'opencode.json');
  write(
    file,
    JSON.stringify({ $schema: 'https://opencode.ai/config.json', mcp: { other: { type: 'local' } }, theme: 'dark' })
  );
  opencode.register(ENTRY);
  const config = readJson(file);
  assert.equal(config.theme, 'dark');
  assert.ok(config.mcp.other);
  assert.ok(config.mcp['jev-use']);
});

test('opencode refuses a jsonc file with comments instead of eating them', () => {
  const dir = path.join(home, '.config', 'opencode');
  const file = path.join(dir, 'opencode.jsonc');
  write(file, '{\n  // my carefully written note\n  "theme": "dark"\n}\n');

  assert.throws(() => opencode.register(ENTRY), /comments/i);
  assert.ok(
    fs.readFileSync(file, 'utf8').includes('my carefully written note'),
    'the file must be left exactly as it was'
  );
});
