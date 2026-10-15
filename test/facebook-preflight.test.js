'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { facebookPreflight } = require('../lib/facebook-preflight');

test('missing runtime stops dependency checks before any process starts', () => {
  const result = facebookPreflight(null, { execute: () => assert.fail('must not launch') });
  assert.equal(result.betterwright.ok, false);
  assert.match(result.betterwright.detail, /Python runtime missing/);
});

test('CLI discovery alone is insufficient when the Betterwright SDK cannot resolve', () => {
  let calls = 0;
  const result = facebookPreflight('python', { execute: (command, args, options) => {
    assert.equal(options.timeout, 10000);
    calls++;
    if (command === 'python') return { ok: true, stdout: JSON.stringify({ cli: 'a spaced path/Betterwright.cmd', typesafe_sdk: true }) };
    assert.equal(args.at(-1), 'a spaced path/Betterwright.cmd');
    assert.ok(!args.some(arg => arg.includes('browser.run')));
    return { ok: false, stderr: 'secret-bearing diagnostic' };
  } });
  assert.equal(calls, 2);
  assert.equal(result.betterwright.ok, false);
  assert.equal(result.jev.ok, true);
  assert.ok(!JSON.stringify(result).includes('secret-bearing diagnostic'));
});

test('readiness reports missing Jev separately from available structured execution', () => {
  const result = facebookPreflight('python', { execute: command => command === 'python'
    ? { ok: true, stdout: JSON.stringify({ cli: 'betterwright', typesafe_sdk: false }) }
    : { ok: true } });
  assert.equal(result.betterwright.ok, true);
  assert.equal(result.jev.ok, false);
});

test('malformed or failed dependency probes return actionable errors without backend output', () => {
  for (const response of [{ ok: true, stdout: 'private diagnostic' }, { ok: false, stdout: 'private diagnostic' }]) {
    const result = facebookPreflight('python', { execute: () => response });
    assert.equal(result.betterwright.ok, false);
    assert.ok(!JSON.stringify(result).includes('private diagnostic'));
  }
});
