'use strict';

/**
 * The install, in one place, so `postinstall`, `jev-use install` and the lazy
 * path in the MCP entry point all do exactly the same thing.
 *
 * Order matters: the key first (so a failed later step still leaves it saved),
 * then the runtime, then the driver, then registration. Anything that can fail
 * without making the install useless fails soft — a missing driver or a missing
 * key is a "finish this later" state, and the tools that need them say so.
 */

const fs = require('fs');

const config = require('./config');
const driver = require('./driver');
const harnesses = require('./harnesses');
const log = require('./log');
const paths = require('./paths');
const runtime = require('./runtime');
const skills = require('./skills');

function writeMarker(summary) {
  try {
    fs.mkdirSync(paths.stateDir(), { recursive: true });
    fs.writeFileSync(
      paths.readyMarker(),
      `${JSON.stringify({ ...summary, at: new Date().toISOString() }, null, 2)}\n`
    );
  } catch {
    /* a marker we cannot write is not worth failing over */
  }
}

function saveKey(explicit) {
  const supplied = explicit
    ? { value: explicit, source: '--key' }
    : config.keyFromInstallFlags();
  if (supplied) {
    config.writeEnvFile({ TYPESAFE_API_KEY: supplied.value });
    log.ok(`API key saved to ${paths.envFile()} (${config.mask(supplied.value)})`);
    return true;
  }
  if (config.hasKey()) {
    log.step('API key already configured');
    return true;
  }
  log.warn('no API key yet — browser_use cannot decide anything without one');
  log.say(
    `    set it later with:  jev-use install --key=<key>\n` +
      `    or edit:            ${paths.envFile()}`
  );
  return false;
}

/** The command every harness will spawn. */
function entry() {
  return { command: process.execPath, args: [paths.mcpEntry()] };
}

async function install(options = {}) {
  const { auto = false, force = false, key = null } = options;

  log.heading(`jev-use ${auto ? '(postinstall)' : ''}`.trim());
  log.say(`${log.dim('platform')} ${process.platform}-${process.arch}`);
  log.say(`${log.dim('state')}    ${paths.stateDir()}`);

  const summary = { key: false, python: null, driver: null, harnesses: [], skills: [], error: null };

  summary.key = saveKey(key);

  try {
    summary.python = await runtime.ensure({ force: force && !auto });
    log.ok(`python: ${summary.python}`);
  } catch (error) {
    summary.error = error.message;
    writeMarker(summary);
    // Thrown errors are reported by the caller; logging here too would print the
    // same failure twice and read as two separate problems.
    if (!auto) throw error;
    log.fail(error.message);
    // In `auto` mode npm must still succeed: a broken environment is something
    // the user can fix later, whereas a failed `npm install` blocks everything.
    return summary;
  }

  const driverPath = await driver.ensure({ force: false });
  if (driverPath) {
    summary.driver = driverPath;
    // Pin the absolute path: on Windows the installer appends to the User PATH,
    // which this process (and the harness) cannot see.
    config.writeEnvFile({ CUA_DRIVER_COMMAND: driverPath });
  } else {
    log.warn('cua-driver is not installed yet; run `jev-use install` to retry');
  }

  summary.harnesses = harnesses.registerAll(entry());
  summary.skills = skills.install({ force: false });

  writeMarker(summary);

  log.heading(summary.error ? 'Needs attention' : 'Ready');
  if (!summary.driver) log.say('  driver : install it, then rerun `jev-use install`');
  if (!summary.key) log.say('  key    : `jev-use install --key=<key>`');
  if (!summary.harnesses.length) log.say('  harness: register the server by hand (see above)');
  log.say('\nRestart your harness so it picks up the new MCP server.');
  return summary;
}

/** Everything that could be wrong, checked and reported. Never throws. */
async function doctor() {
  const lines = [];
  const add = (label, verdict, detail) =>
    lines.push(`  ${verdict ? log.green('ok  ') : log.yellow('!!  ')}${label.padEnd(12)} ${detail}`);

  lines.push(`${log.bold('jev-use doctor')}`);
  add('platform', true, `${process.platform}-${process.arch}, node ${process.version}`);
  add('state dir', fs.existsSync(paths.stateDir()), paths.stateDir());

  const python = runtime.resolve();
  add(
    'python',
    Boolean(python),
    python || 'not provisioned — run `jev-use install`'
  );

  const driverPath = driver.find();
  let driverDetail = driverPath || 'not found — run `jev-use install`';
  if (driverPath) {
    const { run } = require('./run');
    const version = run(driverPath, ['--version']);
    if (version.ok) driverDetail += `  (${version.stdout.trim().split('\n')[0]})`;
  }
  add('driver', Boolean(driverPath), driverDetail);

  const envelope = config.keyFromInstallFlags();
  const keyed = config.hasKey();
  add(
    'api key',
    keyed,
    keyed ? 'configured' : `missing — \`jev-use install --key=<key>\`${envelope ? ' (flag seen)' : ''}`
  );

  const chrome = require('./chrome').find();
  add('chrome', Boolean(chrome), chrome || 'not found — install Google Chrome');

  const found = harnesses.detected();
  add('harnesses', found.length > 0, found.map((h) => h.name).join(', ') || 'none detected');

  for (const harness of found) {
    let registered = false;
    try {
      const file = harness.configFile();
      registered = fs.existsSync(file) && fs.readFileSync(file, 'utf8').includes(harnesses.SERVER_NAME);
    } catch {
      registered = false;
    }
    add('', registered, `${harness.name}${registered ? '' : ' — not registered'}`);
  }

  lines.push(`\n  ${log.dim(paths.mcpEntry())}`);
  log.say(lines.join('\n'));
  return lines.join('\n');
}

module.exports = { install, doctor, entry };
