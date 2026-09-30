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
 *
 * A harness not in this list is not locked out. MCP is the whole interface, so
 * `snippet()` prints the one block any MCP-capable harness needs, and
 * `jev-use-mcp` is on PATH after a global install for the ones that take a bare
 * command.
 */

const log = require('../log');
const commandcode = require('./commandcode');
const claude = require('./claude');
const codex = require('./codex');
const cursor = require('./cursor');
const opencode = require('./opencode');
const windsurf = require('./windsurf');
const gemini = require('./gemini');
const vscode = require('./vscode');
const { SERVER_NAME } = require('./util');

const ALL = [
  commandcode,
  claude,
  codex,
  cursor,
  opencode,
  windsurf,
  gemini,
  vscode,
];

/** The adapter for one id, or null. Used by `--harness=<id>`. */
function byId(id) {
  return ALL.find((harness) => harness.id === id) || null;
}

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
 * The one block any MCP-capable harness needs, for the ones we have no adapter
 * for — a config we do not know the shape of, or a harness that has no
 * user-scope file at all.
 */
function snippet(entry) {
  const args = entry.args.map((arg) => JSON.stringify(arg)).join(', ');
  return [
    'Any MCP-capable harness can use this server. Add:',
    '',
    `    command: ${entry.command}`,
    `    args:    [${args}]`,
    '',
    'or, since the global install puts a launcher on PATH:',
    '',
    '    command: jev-use-mcp',
    '',
    'Most harnesses take a JSON block:',
    '',
    '    {',
    '      "mcpServers": {',
    `        "${SERVER_NAME}": { "command": ${JSON.stringify(entry.command)}, "args": [${args}] }`,
    '      }',
    '    }',
  ].join('\n');
}

/**
 * Register the MCP server everywhere it is welcome.
 *
 * A harness that throws (a config with comments, a permissions problem) is
 * reported and skipped rather than aborting the others — one broken config must
 * not cost you the rest.
 *
 * `only` restricts the run to a set of ids and *forces* them even if detection
 * did not fire, so `--harness=windsurf` works on a machine where we have not yet
 * seen Windsurf's directory.
 */
function registerAll(entry, { only = null } = {}) {
  let targets = detected();
  if (only) {
    targets = only.map((id) => byId(id)).filter(Boolean);
  }

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
    log.warn('no harness config found. Register the server by hand:');
    log.say(snippet(entry));
  }
  return results;
}

module.exports = { ALL, SERVER_NAME, byId, detected, registerAll, snippet };
