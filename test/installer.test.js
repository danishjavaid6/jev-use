'use strict';

/**
 * The installer's failure contract.
 *
 * This is the difference between a confusing `npm install` failure and a
 * message that says how to finish: in postinstall mode (`auto`) a provision
 * failure must be *reported* and not thrown, and in manual mode it must be a
 * real error with a nonzero exit.
 *
 * A failed runtime must not cost the user the rest of the install, either. The
 * harness entry is a node shim that provisions python itself on first use, so
 * the server and the skills are written whether or not python came up —
 * otherwise a broken runtime leaves no tools at all and nothing that says why.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test, beforeEach, afterEach } = require('node:test');

const driver = require('../lib/driver');
const installer = require('../lib/installer');
const paths = require('../lib/paths');
const runtime = require('../lib/runtime');
const skills = require('../lib/skills');
const commandcode = require('../lib/harnesses/commandcode');

let dir;
let originalStateDir;
let originalHome;
let originalEnsure;
let originalDriverEnsure;
let originalPackageRoot;
let originalXdg;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-installer-'));
  originalStateDir = paths.stateDir;
  originalHome = paths.HOME;
  originalEnsure = runtime.ensure;
  originalDriverEnsure = driver.ensure;
  originalPackageRoot = paths.packageRoot;
  originalXdg = process.env.XDG_CONFIG_HOME;

  // `paths.envFile()`, `readyMarker()` and the rest resolve through `stateDir()`,
  // so one override covers the state. HOME covers the harness configs and skill
  // directories, which is what keeps these tests from writing into the
  // developer's real ~/.commandcode the moment registration happens.
  paths.stateDir = () => dir;
  paths.HOME = dir;
  delete process.env.XDG_CONFIG_HOME;

  runtime.ensure = async () => {
    throw new Error('no usable python');
  };
  // Never fetch the real cua-driver from a test.
  driver.ensure = async () => null;
});

afterEach(() => {
  paths.stateDir = originalStateDir;
  paths.HOME = originalHome;
  runtime.ensure = originalEnsure;
  driver.ensure = originalDriverEnsure;
  paths.packageRoot = originalPackageRoot;
  if (originalXdg === undefined) delete process.env.XDG_CONFIG_HOME;
  else process.env.XDG_CONFIG_HOME = originalXdg;
  fs.rmSync(dir, { recursive: true, force: true });
});

test('postinstall mode reports an unprovisionable runtime instead of throwing', async () => {
  const summary = await installer.install({ auto: true, key: 'sk-test' });
  assert.match(summary.error, /no usable python/);
  assert.equal(summary.python, null);
});

test('a failed runtime still registers the server, so the harness can reach it', async () => {
  // Detection asks only that the harness's config directory exists.
  fs.mkdirSync(path.join(dir, '.commandcode'), { recursive: true });

  const summary = await installer.install({ auto: true, key: 'sk-test' });

  assert.equal(summary.harnesses.length, 1, 'registering does not depend on python');
  const config = JSON.parse(fs.readFileSync(path.join(dir, '.commandcode', 'mcp.json'), 'utf8'));
  assert.ok(
    config.mcpServers['jev-use'],
    'the shim is the thing that provisions python on first use'
  );
});

test('a copy inside npm\'s cache is never registered, because npm deletes it', async () => {
  fs.mkdirSync(path.join(dir, '.commandcode'), { recursive: true });
  // What `npm install -g git+https://…` runs the lifecycle scripts from: a clone
  // inside npm's cache. Recording that path in a harness config produces no
  // error anywhere — just a harness with no tools, forever. Deliberately not
  // under os.tmpdir(), which is the guard's other branch and would mask this one.
  paths.packageRoot = () => path.join(os.homedir(), '.npm', '_cacache', 'tmp', 'git-cloneABC123');

  const summary = await installer.install({ auto: true, key: 'sk-test' });

  assert.equal(summary.harnesses.length, 0, 'a doomed path is not worth recording');
  assert.equal(
    fs.existsSync(path.join(dir, '.commandcode', 'mcp.json')),
    false,
    'the config is left alone'
  );
});

test('the temporary-root rule catches both npm scratch locations', () => {
  const cache = path.join(os.homedir(), '.npm', '_cacache', 'tmp', 'git-cloneABC123');
  assert.equal(installer.isEphemeralRoot(cache), true, "npm's git clone");
  assert.equal(installer.isEphemeralRoot(path.join(os.tmpdir(), 'unpacked')), true, 'a temp extract');
  assert.equal(installer.isEphemeralRoot(path.join(os.homedir(), 'Hamza', 'computer-use')), false, 'a checkout');
});

test('postinstall mode still records what happened, for doctor to read', async () => {
  await installer.install({ auto: true, key: 'sk-test' });
  const marker = JSON.parse(fs.readFileSync(path.join(dir, 'installed.json'), 'utf8'));
  assert.match(marker.error, /no usable python/);
  assert.equal(marker.key, true, 'the key was saved before the runtime failed');
});

test('manual mode raises, because the user is watching', async () => {
  await assert.rejects(
    () => installer.install({ auto: false, key: 'sk-test' }),
    /no usable python/
  );
});

test('the key is saved before anything that can fail', async () => {
  await installer.install({ auto: true, key: 'sk-keep-me' }).catch(() => {});
  const env = fs.readFileSync(path.join(dir, '.env'), 'utf8');
  assert.match(env, /TYPESAFE_API_KEY=sk-keep-me/);
});

// -- doctor ------------------------------------------------------------------
//
// Reporting "registered" because the file contains the word `jev-use` is the
// failure that reads as success: the harness starts fine and never has these
// tools. Doctor has to look at the path that was recorded, not the file's text.

test('doctor reports a registration whose command is gone, instead of ok', async () => {
  const file = path.join(dir, '.commandcode', 'mcp.json');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(
    file,
    JSON.stringify({
      mcpServers: {
        'jev-use': {
          transport: 'stdio',
          enabled: true,
          command: path.join(dir, 'vanished', 'node'),
          args: [path.join(dir, 'vanished', 'jev-use-mcp.js')],
        },
      },
    })
  );

  const report = await installer.doctor();

  assert.match(report, /Command Code — registered at .*which is gone/);
});

test('doctor reports a live registration as ok', async () => {
  fs.mkdirSync(path.join(dir, '.commandcode'), { recursive: true });
  commandcode.register({ command: process.execPath, args: [paths.mcpEntry()] });

  const report = await installer.doctor();

  assert.match(report, /Command Code — registered(\n|$)/);
  assert.ok(!/which is gone/.test(report), 'a path that exists is not reported as gone');
});

test('doctor reports the slash commands, not only the server', async () => {
  fs.mkdirSync(path.join(dir, '.commandcode'), { recursive: true });

  assert.match(await installer.doctor(), /skills\s+not installed/);

  skills.install({ force: true });
  const after = await installer.doctor();
  assert.ok(!/skills\s+not installed/.test(after), 'once copied, the skill is reported present');
});

test('skills install into the shared global agent directory on a fresh machine', () => {
  // No harness-specific directory exists here. The shared user-scoped location
  // must still receive both slash commands so a harness opened from any project
  // can discover them later.
  const results = skills.install({ force: true });
  const shared = path.join(dir, '.agents', 'skills');
  assert.ok(results.some((result) => result.file.startsWith(shared) && result.ok));
  for (const name of ['browser-use', 'mobile-use']) {
    assert.ok(fs.existsSync(path.join(shared, name, 'SKILL.md')));
  }
});

// -- targeting a harness ----------------------------------------------------
//
// `--harness=<id>` is the escape hatch for a machine where detection is wrong —
// a config directory that has not been created yet, or a harness this build has
// an adapter for but no evidence of. The flag has to force the write.

test('--harness registers a named harness even when detection did not fire', async () => {
  const summary = await installer.install({
    auto: true,
    key: 'sk-test',
    harness: ['windsurf'],
  });

  assert.equal(summary.harnesses.length, 1);
  assert.equal(summary.harnesses[0].id, 'windsurf');
  assert.ok(
    fs.existsSync(path.join(dir, '.codeium', 'windsurf', 'mcp_config.json')),
    'the named harness is written where it reads'
  );
});

test('--harness registers only what it names', async () => {
  fs.mkdirSync(path.join(dir, '.commandcode'), { recursive: true });

  const summary = await installer.install({ auto: true, key: 'sk-test', harness: ['cursor'] });

  assert.deepEqual(summary.harnesses.map((h) => h.id), ['cursor']);
  assert.equal(
    fs.existsSync(path.join(dir, '.commandcode', 'mcp.json')),
    false,
    'a detected harness is skipped when the flag names another'
  );
});

// -- GoLogin -----------------------------------------------------------------
//
// A GoLogin token is what stands between "these profiles exist" and "browser_open
// can launch one in its own browser" — so it is saved, and it is what triggers the
// optional SDK install, plus the browser-harness CDP client the profile is driven
// over. Without a token we must not drag requests + psutil + cdp-use in.

test('a GoLogin token is saved and pulls in the GoLogin SDK', async () => {
  const calls = [];
  const originalExtra = runtime.installExtra;
  const originalEnv = process.env.GOLOGIN_TOKEN;
  delete process.env.GOLOGIN_TOKEN;
  runtime.installExtra = (name) => {
    calls.push(name);
    return null;
  };

  try {
    const summary = await installer.install({
      auto: true,
      key: 'sk-test',
      gologinToken: 'gl-abc',
    });

    assert.equal(summary.gologin, true);
    assert.deepEqual(
      calls,
      ['gologin'],
      'the token asks for the SDK and the CDP client it is driven over'
    );
    const env = fs.readFileSync(path.join(dir, '.env'), 'utf8');
    assert.match(env, /GOLOGIN_TOKEN=gl-abc/);
    assert.match(env, /TYPESAFE_API_KEY=sk-test/, 'the Jev key is not dropped');
  } finally {
    runtime.installExtra = originalExtra;
    if (originalEnv === undefined) delete process.env.GOLOGIN_TOKEN;
    else process.env.GOLOGIN_TOKEN = originalEnv;
  }
});

test('the GoLogin SDK is not installed without a token', async () => {
  const calls = [];
  const originalExtra = runtime.installExtra;
  const originalEnv = process.env.GOLOGIN_TOKEN;
  delete process.env.GOLOGIN_TOKEN;
  runtime.installExtra = (name) => {
    calls.push(name);
    return null;
  };

  try {
    const summary = await installer.install({ auto: true, key: 'sk-test' });
    assert.equal(summary.gologin, false);
    assert.deepEqual(calls, [], 'nobody who does not use GoLogin pays for its dependency');
  } finally {
    runtime.installExtra = originalExtra;
    if (originalEnv === undefined) delete process.env.GOLOGIN_TOKEN;
    else process.env.GOLOGIN_TOKEN = originalEnv;
  }
});
