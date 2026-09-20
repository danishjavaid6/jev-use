'use strict';

/** Minimal synchronous process helpers. No dependencies, no shell interpolation. */

const { spawnSync } = require('child_process');

/**
 * Run a command and capture it.
 *
 * `shell` is always false: every argument here is either a literal we control or
 * a path we resolved, and turning the shell on would make a path with a space
 * (every Windows "Program Files" path) a quoting bug waiting to happen.
 */
function run(command, args = [], options = {}) {
  const result = spawnSync(command, args, {
    encoding: 'utf8',
    stdio: options.stdio || 'pipe',
    cwd: options.cwd,
    env: options.env || process.env,
    timeout: options.timeout,
    windowsHide: true,
    input: options.input,
  });
  return {
    ok: result.status === 0,
    status: result.status,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    error: result.error,
  };
}

/** First of `names` that resolves on PATH, using the shell's own lookup. */
function which(names) {
  const list = Array.isArray(names) ? names : [names];
  const probe = process.platform === 'win32' ? 'where' : 'which';
  for (const name of list) {
    const found = run(probe, [name]);
    if (!found.ok) continue;
    // `where` can return several lines; the first is what the shell would pick.
    const first = found.stdout.split(/\r?\n/).map((l) => l.trim()).filter(Boolean)[0];
    if (first) return first;
  }
  return null;
}

module.exports = { run, which };
