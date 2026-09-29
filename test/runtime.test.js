'use strict';

/**
 * The repair path for a half-created virtual environment.
 *
 * A retry after a failed install used to be impossible: `uv venv` refuses to
 * touch a directory that already exists ("A virtual environment already exists
 * at ..."), so the leftover from the first attempt wedged every later one and
 * the only way forward was deleting the directory by hand. `ensure()` clears it
 * before recreating; these pin that down.
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test, beforeEach, afterEach } = require('node:test');

const paths = require('../lib/paths');
const runtime = require('../lib/runtime');

let dir;
let originalStateDir;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-runtime-'));
  originalStateDir = paths.stateDir;
  // Everything under the environment resolves through stateDir(), so one
  // override keeps the real machine's venv out of reach.
  paths.stateDir = () => dir;
});

afterEach(() => {
  paths.stateDir = originalStateDir;
  fs.rmSync(dir, { recursive: true, force: true });
});

test('a leftover environment is removed, so the retry can proceed', () => {
  const venv = paths.venvDir();
  fs.mkdirSync(path.join(venv, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(venv, 'bin', 'python'), '');
  assert.ok(fs.existsSync(venv), 'the leftover is there to begin with');

  runtime.clearVenv();

  assert.equal(fs.existsSync(venv), false);
});

test('removing an environment that is not there is not an error', () => {
  assert.equal(fs.existsSync(paths.venvDir()), false);
  assert.doesNotThrow(() => runtime.clearVenv());
});
