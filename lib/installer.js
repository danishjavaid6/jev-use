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
const os = require('os');
const path = require('path');

const config = require('./config');
const driver = require('./driver');
const harnesses = require('./harnesses');
const log = require('./log');
const paths = require('./paths');
const runtime = require('./runtime');
const skills = require('./skills');

/**
 * Is this copy running from somewhere that will not exist tomorrow?
 *
 * `npm install -g git+https://…` clones the repository into npm's own cache and
 * runs the lifecycle scripts *inside that clone* — and the clone is what npm
 * deletes the next time it tidies its cache. Registering a harness against
 * `paths.mcpEntry()` from there records a doomed path.
 *
 * Measured on a real machine: all four harness configs (Command Code, Claude
 * Code, Codex, Cursor) held
 * `~/.npm/_cacache/tmp/git-cloneXXXX/bin/jev-use-mcp.js`, and the directory was
 * already gone. The symptom is not an error — it is a harness that simply has no
 * tools, in every project, with nothing anywhere saying why.
 *
 * A dev checkout is not in the cache and is not temporary, so it registers
 * normally. Only npm's own scratch space is refused.
 */
function isEphemeralRoot(root) {
  if (root.split(path.sep).includes('_cacache')) return true;
  const tmp = os.tmpdir();
  return root === tmp || root.startsWith(tmp + path.sep);
}

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
  const { auto = false, force = false, key = null, harness = null } = options;

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
    // Deliberately not fatal to the steps below. The harness entry is a node
    // shim that provisions python itself on first use, so registration and the
    // skills belong on disk whether or not the runtime came up. Returning early
    // here is what left a machine with a broken runtime with *no tools at all*
    // and nothing visible to say why: the harness never learned the server
    // existed. Only the reporting differs — thrown errors are reported by the
    // caller, so `auto` is the one that logs here.
    if (auto) log.fail(error.message);
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

  // Refusing beats registering a path npm will delete, which is silent: the
  // harness keeps starting normally and simply never has these tools.
  if (isEphemeralRoot(paths.packageRoot())) {
    log.warn(`not registering — this copy is in npm's cache (${paths.packageRoot()}),`);
    log.say('    which npm deletes. Install it somewhere durable, then register:');
    log.say('      npm install -g <package>   &&   jev-use install');
  } else {
    summary.harnesses = harnesses.registerAll(entry(), { only: harness });
  }
  summary.skills = skills.install({ force: false });

  writeMarker(summary);

  log.heading(summary.error ? 'Needs attention' : 'Ready');
  if (!summary.driver) log.say('  driver : install it, then rerun `jev-use install`');
  if (!summary.key) log.say('  key    : `jev-use install --key=<key>`');
  if (!summary.harnesses.length) log.say('  harness: register the server by hand (see above)');
  log.say('\nRestart your harness so it picks up the new MCP server.');

  // Manual installs still fail loudly — the user is watching, and a quiet exit
  // code would hide that the runtime is the one thing left to fix.
  if (!auto && summary.error) throw new Error(summary.error);
  return summary;
}

/**
 * What to say about one harness: whether it is registered, and whether what it
 * recorded still exists.
 *
 * The existence check is the whole point. Substring-matching the config for
 * `jev-use` reports a healthy install for a registration written from npm's
 * cache clone, or from a node that an nvm switch has since replaced — both of
 * which leave a harness that starts normally, has no tools, and says nothing.
 * Reading the entry back and stat-ing it is the only thing that tells those
 * apart from a working install.
 */
function registrationVerdict(harness) {
  let recorded = null;
  try {
    if (typeof harness.registered === 'function') recorded = harness.registered();
  } catch {
    recorded = null;
  }
  if (!recorded) return [false, `${harness.name} — not registered`];

  const recordedPaths = [recorded.command, ...recorded.args].filter(
    (value) => typeof value === 'string' && value
  );
  if (recordedPaths.some((value) => /_cacache|[\\/]git-clone/.test(value))) {
    return [false, `${harness.name} — registered at a temporary path; re-run \`jev-use install\``];
  }

  const gone = recordedPaths.filter((value) => !fs.existsSync(value));
  if (gone.length) {
    return [
      false,
      `${harness.name} — registered at ${gone[0]}, which is gone; re-run \`jev-use install\``,
    ];
  }
  return [true, `${harness.name} — registered`];
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
    const [verdict, detail] = registrationVerdict(harness);
    add('', verdict, detail);
  }

  // The slash commands are a separate install from the server, and a device that
  // lost them shows the same symptom — `/browser-use` simply does not exist.
  const skillRoots = skills.targetDirs().filter((root) => fs.existsSync(path.dirname(root)));
  const withSkill = skillRoots.filter((root) =>
    fs.existsSync(path.join(root, 'browser-use', 'SKILL.md'))
  );
  add(
    'skills',
    withSkill.length > 0,
    withSkill.length
      ? withSkill.map((root) => path.join(root, 'browser-use')).join(', ')
      : 'not installed — re-run `jev-use install`'
  );

  lines.push(`\n  ${log.dim(paths.mcpEntry())}`);
  log.say(lines.join('\n'));
  return lines.join('\n');
}

module.exports = { install, doctor, entry, isEphemeralRoot };
