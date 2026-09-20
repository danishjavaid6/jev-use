'use strict';

/**
 * The installer's failure contract.
 *
 * This is the difference between a confusing `npm install` failure and a
 * message that says how to finish: in postinstall mode (`auto`) a provision
 * failure must be *reported* and not thrown, and in manual mode it must be a
 * real error with a nonzero exit.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test, beforeEach, afterEach } = require('node:test');

const installer = require('../lib/installer');
const paths = require('../lib/paths');
const runtime = require('../lib/runtime');

let dir;
let originalStateDir;
let originalEnsure;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-installer-'));
  originalStateDir = paths.stateDir;
  originalEnsure = runtime.ensure;
  // Redirect all state into the temp dir. `paths.envFile()`, `readyMarker()` and
  // the rest all resolve through `stateDir()`, so one override covers them.
  paths.stateDir = () => dir;
  runtime.ensure = async () => {
    throw new Error('no usable python');
  };
});

afterEach(() => {
  paths.stateDir = originalStateDir;
  runtime.ensure = originalEnsure;
  fs.rmSync(dir, { recursive: true, force: true });
});

test('postinstall mode reports an unprovisionable runtime instead of throwing', async () => {
  const summary = await installer.install({ auto: true, key: 'sk-test' });
  assert.match(summary.error, /no usable python/);
  assert.equal(summary.python, null);
  assert.equal(summary.harnesses.length, 0, 'nothing is registered when the runtime is missing');
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
