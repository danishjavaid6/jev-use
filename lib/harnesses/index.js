'use strict';

/**
 * The harness registry.
 *
 * Every one of these reads MCP servers from a user-scope config file, so a single
 * `npm install` can register into all of them at once — which is what makes "use
 * it from whatever harness you already have" work rather than forcing a choice.
 *
 * Detection is intentionally loose: a config *directory* existing is enough.
 * Refusing to register because a config file has not been created yet would mean
 * a fresh harness install never gets picked up.
 */

const log = require('../log');
const commandcode = require('./commandcode');
const claude = require('./claude');
const codex = require('./codex');
const cursor = require('./cursor');
const opencode = require('./opencode');
const { SERVER_NAME } = require('./util');

const ALL = [commandcode, claude, codex, cursor, opencode];

/** Harnesses whose config directory exists on this machine. */
function detected() {
  return ALL.filter((harness) => {
    try {
      return harness.detect();
    } catch {
      return false;
    }
  });
}

/**
 * Register the MCP server everywhere it is welcome.
 *
 * A harness that throws (a config with comments, a permissions problem) is
 * reported and skipped rather than aborting the others — one broken config must
 * not cost you the rest.
 */
function registerAll(entry, { only = null } = {}) {
  const targets = detected().filter(
    (harness) => !only || only.includes(harness.id)
  );
  const results = [];

  for (const harness of targets) {
    try {
      const file = harness.register(entry);
      log.ok(`${harness.name}: registered in ${file}`);
      results.push({ id: harness.id, name: harness.name, file, ok: true });
    } catch (error) {
      log.warn(`${harness.name}: could not register (${error.message})`);
      results.push({ id: harness.id, name: harness.name, ok: false, error: error.message });
    }
  }

  if (!results.length) {
    log.warn(
      'no harness config found. Register the server by hand — the command is:\n' +
        `    ${entry.command} ${entry.args.join(' ')}`
    );
  }
  return results;
}

module.exports = { ALL, SERVER_NAME, detected, registerAll };
