#!/usr/bin/env node
'use strict';

/**
 * `jev-use` — install, diagnose, and drive the profile launcher.
 *
 * The install is the same code path as `postinstall`, which is what makes it a
 * usable repair command: `--ignore-scripts`, a failed download and a moved
 * runtime all end with the user running this by hand.
 */

const installer = require('../lib/installer');
const log = require('../lib/log');
const paths = require('../lib/paths');
const runtime = require('../lib/runtime');
const { run } = require('../lib/run');

const USAGE = `jev-use — browser use where Jev makes the decisions

Usage
  jev-use install [--key=<key>] [--auto] [--force]
        Provision the runtime, install cua-driver, and register the MCP server
        and the /browser-use and /mobile-use skills with every harness it finds.

  jev-use doctor
        Report what is and is not working, and why.

  jev-use list [--filter=<text>]
        List the Chrome profiles browser_open can launch.

  jev-use open <profile> [--port=<n>] [--refresh]
        Copy a profile to a private directory and launch it with a CDP endpoint,
        which is the one-time step that makes browser_use possible.

Options
  --key=<key>    The Jev/Typesafe API key. Also read from --jev-key on
                 npm install, or the TYPESAFE_API_KEY environment variable.
`;

//: Flags that take a value, so `--key sk-abc` works as well as `--key=sk-abc`.
const VALUE_FLAGS = new Set(['key', 'jev-key', 'api-key', 'port', 'filter']);

function parseArgs(argv) {
  const flags = {};
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const match = /^--([a-z0-9-]+)(?:=(.*))?$/i.exec(arg);
    if (!match) {
      rest.push(arg);
      continue;
    }
    const name = match[1].toLowerCase();
    if (match[2] !== undefined) {
      flags[name] = match[2];
      continue;
    }
    // A space is what people actually type; without this `--key sk-abc` would
    // leave the key as a stray positional and silently install with no key.
    const next = argv[i + 1];
    if (VALUE_FLAGS.has(name) && next !== undefined && !next.startsWith('--')) {
      flags[name] = next;
      i += 1;
      continue;
    }
    flags[name] = true;
  }
  return { flags, rest };
}

function firstString(...values) {
  return values.find((value) => typeof value === 'string' && value.length > 0) || null;
}

/** Run a `python -m jev_use.profiles` command in the provisioned interpreter. */
async function profiles(args) {
  const python = await runtime.ensure();
  const result = run(python, ['-m', 'jev_use.profiles', ...args], { stdio: 'inherit' });
  return result.status === null ? 1 : result.status;
}

async function main() {
  const { flags, rest } = parseArgs(process.argv.slice(2));
  const command = rest[0] || 'help';

  switch (command) {
    case 'install': {
      const auto = Boolean(flags.auto);
      const summary = await installer.install({
        auto,
        force: Boolean(flags.force),
        key: firstString(flags.key, flags['jev-key'], flags['api-key']),
      });
      // Postinstall must not fail `npm install`. An unpickable Python or a
      // blocked download is a "finish this later" state, and the failure is
      // already printed with the command that finishes the job; exiting nonzero
      // here would abort the user's install and hide that message.
      if (auto) return 0;
      return summary.error ? 1 : 0;
    }

    case 'doctor': {
      await installer.doctor();
      return runtime.isReady() ? 0 : 1;
    }

    case 'list': {
      const args = ['--list'];
      if (flags.filter) args.push('--filter', String(flags.filter));
      return profiles(args);
    }

    case 'open': {
      const profile = rest[1];
      if (!profile) {
        log.fail('which profile? Try `jev-use list`, then `jev-use open "<name>"`.');
        return 2;
      }
      const args = ['--open', profile];
      if (flags.port) args.push('--port', String(flags.port));
      if (flags.refresh) args.push('--refresh');
      return profiles(args);
    }

    case 'env': {
      // Printing the path, never the contents: an API key must not land in a log.
      log.say(paths.envFile());
      return 0;
    }

    default: {
      log.say(USAGE);
      return rest.length ? 2 : 0;
    }
  }
}

main().then(
  (code) => process.exit(code),
  (error) => {
    log.fail(error.message);
    process.exit(1);
  }
);
