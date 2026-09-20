'use strict';

/** The API key path: how it arrives from npm, and how it is stored. */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test, beforeEach, afterEach } = require('node:test');

const config = require('../lib/config');

let dir;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jev-env-'));
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

// -- the npm flag -----------------------------------------------------------

test('npm maps --jev-key to npm_config_jev_key, and we read it', () => {
  const found = config.keyFromInstallFlags({ npm_config_jev_key: 'sk-abc123' });
  assert.equal(found.value, 'sk-abc123');
  assert.equal(found.source, '--jev-key');
});

test('--api-key is accepted too', () => {
  assert.equal(config.keyFromInstallFlags({ npm_config_api_key: 'k' }).value, 'k');
});

test('an exported TYPESAFE_API_KEY is the fallback', () => {
  const found = config.keyFromInstallFlags({ TYPESAFE_API_KEY: 'sk-live' });
  assert.equal(found.value, 'sk-live');
  assert.equal(found.source, 'TYPESAFE_API_KEY');
});

test('the flag wins over the environment variable', () => {
  const found = config.keyFromInstallFlags({
    npm_config_jev_key: 'from-flag',
    TYPESAFE_API_KEY: 'from-env',
  });
  assert.equal(found.value, 'from-flag');
});

test('nothing supplied means nothing found, not an empty string', () => {
  assert.equal(config.keyFromInstallFlags({}), null);
  assert.equal(config.keyFromInstallFlags({ npm_config_jev_key: '   ' }), null);
});

// -- the env file -----------------------------------------------------------

test('the env file round-trips and is not world-readable', () => {
  const file = path.join(dir, '.env');
  config.writeEnvFile({ TYPESAFE_API_KEY: 'sk-secret' }, file);

  assert.deepEqual(config.readEnvFile(file), { TYPESAFE_API_KEY: 'sk-secret' });
  if (process.platform !== 'win32') {
    const mode = fs.statSync(file).mode & 0o777;
    assert.equal(mode, 0o600, 'a key readable by other users is a leaked key');
  }
});

test('writing one key does not drop the others', () => {
  const file = path.join(dir, '.env');
  config.writeEnvFile({ TYPESAFE_API_KEY: 'sk-a' }, file);
  config.writeEnvFile({ CUA_DRIVER_COMMAND: '/opt/cua-driver' }, file);

  const values = config.readEnvFile(file);
  assert.equal(values.TYPESAFE_API_KEY, 'sk-a');
  assert.equal(values.CUA_DRIVER_COMMAND, '/opt/cua-driver');
});

test('quoted values are unwrapped, because keys get pasted', () => {
  const file = path.join(dir, '.env');
  fs.writeFileSync(file, 'TYPESAFE_API_KEY="sk-quoted"\n');
  assert.equal(config.readEnvFile(file).TYPESAFE_API_KEY, 'sk-quoted');
});

test('comments and blank lines are ignored', () => {
  const parsed = config.parse('# a note\n\nTYPESAFE_API_KEY=k\n');
  assert.deepEqual(parsed, { TYPESAFE_API_KEY: 'k' });
});

test('hasKey is false for a file that exists but holds no key', () => {
  const file = path.join(dir, '.env');
  config.writeEnvFile({ CUA_DRIVER_COMMAND: '/x' }, file);
  assert.equal(config.hasKey(file), false);
});

// -- display ----------------------------------------------------------------

test('a secret is masked down to something recognisable', () => {
  assert.equal(config.mask('sk-1234567890abcdef'), 'sk-1...cdef');
  assert.equal(config.mask('short'), '*****');
  assert.equal(config.mask(''), '');
});
