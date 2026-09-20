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
 * It also carries the two chores the Python side cannot do for itself:
 *
 *   * loading the API key from the state dir into the child's environment, so
 *     the key never has to be written into the package or a harness config;
 *   * provisioning the runtime on first use, which is the safety net for an
 *     install that ran with `--ignore-scripts` (or that failed halfway).
 *
 * stdout is the JSON-RPC channel and stays untouched: everything this file logs
 * goes to stderr.
 */

const { spawn } = require('child_process');

const config = require('../lib/config');
const log = require('../lib/log');
const paths = require('../lib/paths');
const runtime = require('../lib/runtime');

async function main() {
  let python;
  try {
    python = await runtime.ensure();
  } catch (error) {
    log.fail(error.message);
    process.exit(1);
  }

  const env = { ...process.env, ...config.readEnvFile() };

  const child = spawn(python, ['-m', 'jev_use.mcp_server'], {
    stdio: 'inherit',
    env,
    windowsHide: true,
  });

  child.on('error', (error) => {
    log.fail(`could not start ${python}: ${error.message}`);
    process.exit(1);
  });

  // The harness closes our stdin to shut the server down; make sure the python
  // process goes with us rather than being orphaned on every session end.
  const stop = (signal) => {
    if (!child.killed) child.kill(signal);
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
  process.on('SIGHUP', () => stop('SIGHUP'));
  process.on('exit', () => stop('SIGTERM'));

  child.on('exit', (code, signal) => {
    process.exit(signal ? 1 : code === null ? 0 : code);
  });
}

main().catch((error) => {
  log.fail(`${error.message}\n${paths.mcpEntry()}`);
  process.exit(1);
});
