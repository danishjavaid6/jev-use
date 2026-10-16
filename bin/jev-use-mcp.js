#!/usr/bin/env node
'use strict';

/**
 * The stdio MCP server entry point a harness spawns.
 *
 * A Node shim rather than a path to the venv's python, for one reason: a harness
 * config stores a single absolute command string, and the venv python's path
 * differs by platform (`bin/python` vs `Scripts/python.exe`) and moves whenever
 * the runtime is rebuilt. Node is guaranteed to be present — npm is running us —
 * so `node .../bin/jev-use-mcp.js` is a command that keeps working.
 *
 * Loads saved credentials without putting them in harness configuration, then
 * starts an already-installed runtime. Provisioning belongs to `jev-use install`,
 * not an MCP connection with a short timeout.
 *
 * stdout is the JSON-RPC channel and stays untouched: everything this file logs
 * goes to stderr.
 */

const { spawn } = require('child_process');

const config = require('../lib/config');
const log = require('../lib/log');
const paths = require('../lib/paths');
const runtime = require('../lib/runtime');
const lifecycle = require('../lib/lifecycle');

async function main() {
  const release = lifecycle.beginSession();
  process.on('exit', release);
  let python;
  try {
    // A harness connection must not trigger downloads or environment rebuilds:
    // those exceed connection timeouts and can invalidate another live session.
    python = runtime.resolve();
    if (!python) throw new Error('Python runtime unavailable. Run: jev-use install');
  } catch (error) {
    log.fail(error.message);
    process.exit(1);
  }

  const env = { ...process.env, ...config.readEnvFile(), JEV_USE_NODE: process.execPath, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' };

  const child = spawn(python, ['-u', '-m', 'jev_use.mcp_server'], {
    stdio: ['pipe', 'inherit', 'inherit'],
    env,
    windowsHide: true,
  });
  // EOF is a graceful shutdown request. Python finishes its bounded action,
  // preserves the journal and saves its GoLogin session before exiting.
  process.stdin.pipe(child.stdin);
  child.stdin.on('error', error => {
    if (error.code !== 'EPIPE' && error.code !== 'ERR_STREAM_DESTROYED') log.fail(error.message);
  });

  child.on('error', (error) => {
    log.fail(`could not start ${python}: ${error.message}`);
    process.exit(1);
  });

  // The harness closes our stdin to shut the server down; make sure the python
  // process goes with us rather than being orphaned on every session end.
  let stopping = false;
  let shutdownTimer;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    process.stdin.unpipe(child.stdin);
    process.stdin.pause();
    child.stdin.end();
    shutdownTimer = setTimeout(() => {
      log.fail('MCP shutdown deadline exceeded; inspect the saved run before resuming.');
      child.kill('SIGTERM');
    }, 240000);
    shutdownTimer.unref();
  };
  process.stdin.on('end', stop);
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
  process.on('SIGHUP', stop);
  process.on('exit', () => { if (child.exitCode === null && !child.killed) child.kill('SIGTERM'); });

  child.on('exit', (code, signal) => {
    clearTimeout(shutdownTimer);
    if (signal || code) log.fail(`MCP runtime exited (${signal || code}); reconnect and inspect the saved run before resuming.`);
    process.exit(signal ? 1 : code === null ? 0 : code);
  });
}

main().catch((error) => {
  log.fail(`${error.message}\n${paths.mcpEntry()}`);
  process.exit(1);
});
