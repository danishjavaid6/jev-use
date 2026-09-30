'use strict';

/**
 * Provisioning the Python side.
 *
 * The engine is Python and the packaging is npm, so something has to bridge the
 * two. This does, in this order:
 *
 *   1. an existing dev venv in the package (`uv sync` / `pip install -e .`), if
 *      there is one — a checkout that already works should not get a second venv;
 *   2. a system Python 3.10+ — the fewest moving parts, no downloads;
 *   3. uv, which installs its own CPython. This is the branch that makes
 *      "just `npm install`" honest on a machine with Node and Chrome but no Python.
 *
 * The package itself has no dependencies; `typesafe-sdk` is what the `live` extra
 * adds, and it is attempted separately so that a failure there degrades to
 * click-and-read instead of a dead install.
 */

const fs = require('fs');
const path = require('path');

const log = require('./log');
const paths = require('./paths');
const uv = require('./uv');
const { run, which } = require('./run');

const MIN_PYTHON = [3, 10];
const DEFAULT_PYTHON = '3.12';
const LONG = 20 * 60 * 1000;

/**
 * Describe a failed command without ever producing a blank message.
 *
 * A shim or a killed process can fail with no output at all, and "failed: " with
 * nothing after it tells the user nothing; the exit code at least says whether it
 * refused or died.
 */
function failureDetail(result) {
  const text = (result.stderr || result.stdout || '').trim().slice(0, 300);
  if (text) return `: ${text}`;
  const reason = result.error && result.error.code ? `, ${result.error.code}` : '';
  return ` (exit ${result.status}${reason})`;
}

/** The venv a checkout would already have from `uv sync`. */
function devVenvPython() {
  return paths.IS_WINDOWS
    ? path.join(paths.packageRoot(), '.venv', 'Scripts', 'python.exe')
    : path.join(paths.packageRoot(), '.venv', 'bin', 'python');
}

function canImportServer(python) {
  if (!python || !fs.existsSync(python)) return false;
  return run(python, ['-c', 'import jev_use.mcp_server'], { timeout: 120000 }).ok;
}

function pythonCandidates() {
  // On Windows `py -3` is the launcher and the reliable way in; `python` may be
  // either the launcher stub or a store placeholder that does nothing.
  if (paths.IS_WINDOWS) {
    return [
      ['py', ['-3']],
      ['python', []],
      ['python3', []],
    ];
  }
  return [
    ['python3', []],
    ['python', []],
  ];
}

function probe(command, prefix) {
  const code = 'import sys;print("%d.%d"%sys.version_info[:2])';
  const result = run(command, [...prefix, '-c', code], { timeout: 30000 });
  if (!result.ok) return null;
  const match = /^(\d+)\.(\d+)/.exec(result.stdout.trim());
  if (!match) return null;
  const version = [Number(match[1]), Number(match[2])];
  const tooOld =
    version[0] < MIN_PYTHON[0] || (version[0] === MIN_PYTHON[0] && version[1] < MIN_PYTHON[1]);
  if (tooOld) {
    log.step(`ignoring ${command} ${version.join('.')} (need ${MIN_PYTHON.join('.')}+)`);
    return null;
  }
  return { command, prefix, version };
}

function findSystemPython() {
  for (const [name, prefix] of pythonCandidates()) {
    const resolved = which([name]);
    if (!resolved) continue;
    const found = probe(resolved, prefix);
    if (found) return { ...found, path: resolved };
  }
  return null;
}

/** Install the package into an existing interpreter's venv. */
function installPackage(python) {
  const base = run(
    python,
    ['-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-e', '.'],
    { cwd: paths.packageRoot(), timeout: LONG }
  );
  if (!base.ok) {
    throw new Error(`pip install failed${failureDetail(base)}`);
  }
  // Optional: the decision model. Without it the server runs but browser_use
  // cannot choose an action, so this is a warning and not a failure.
  const extra = run(
    python,
    ['-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-e', '.[live]'],
    { cwd: paths.packageRoot(), timeout: LONG }
  );
  if (!extra.ok) {
    log.warn('could not install the optional typesafe-sdk (browser_use needs it)');
  }
}

/**
 * Remove a virtual environment left behind by an earlier attempt.
 *
 * Both calls below are the *repair* path — `resolve()` only lets us get here when
 * no usable interpreter exists — and a directory from a half-finished run is
 * exactly what stops the retry, because `uv venv` refuses to touch an existing
 * directory ("A virtual environment already exists at ...") where python's venv
 * would quietly reuse it and inherit the damage. Removing it here does the job
 * for both, without depending on either tool's overwrite flag.
 */
function clearVenv() {
  try {
    fs.rmSync(paths.venvDir(), { recursive: true, force: true });
  } catch (error) {
    // A process may still hold a file open. Say so, then let the command that
    // follows fail with its own, more specific complaint.
    log.warn(`could not remove the old environment at ${paths.venvDir()}: ${error.message}`);
  }
}

function createWithSystemPython(found) {
  fs.mkdirSync(paths.stateDir(), { recursive: true });
  clearVenv();
  const created = run(
    found.path,
    [...found.prefix, '-m', 'venv', paths.venvDir()],
    { timeout: 5 * 60 * 1000 }
  );
  if (!created.ok) {
    log.warn(`python -m venv failed${failureDetail(created)}`);
    return null;
  }
  const python = paths.venvPython();
  if (!fs.existsSync(python)) return null;
  log.step(`installing the package into ${paths.venvDir()}`);
  installPackage(python);
  return python;
}

async function createWithUv() {
  const uvBin = await uv.ensure();
  if (!uvBin) return null;

  log.step(`uv python install ${DEFAULT_PYTHON}`);
  const installed = run(uvBin, ['python', 'install', DEFAULT_PYTHON], { timeout: LONG });
  if (!installed.ok) {
    log.warn(`uv python install failed${failureDetail(installed)}`);
    return null;
  }

  fs.mkdirSync(paths.stateDir(), { recursive: true });
  clearVenv();
  const venv = run(uvBin, ['venv', '--python', DEFAULT_PYTHON, paths.venvDir()], {
    timeout: LONG,
  });
  if (!venv.ok) {
    log.warn(`uv venv failed${failureDetail(venv)}`);
    return null;
  }

  const python = paths.venvPython();
  log.step('installing the package');
  const base = run(uvBin, ['pip', 'install', '--python', python, '-e', '.'], {
    cwd: paths.packageRoot(),
    timeout: LONG,
  });
  if (!base.ok) {
    log.warn(`uv pip install failed${failureDetail(base)}`);
    return null;
  }
  const extra = run(uvBin, ['pip', 'install', '--python', python, '-e', '.[live]'], {
    cwd: paths.packageRoot(),
    timeout: LONG,
  });
  if (!extra.ok) log.warn('could not install the optional typesafe-sdk (browser_use needs it)');

  return python;
}

/** The interpreter to run the server with, or null if there is not one yet. */
function resolve() {
  const dev = devVenvPython();
  if (canImportServer(dev)) return dev;
  const venv = paths.venvPython();
  if (canImportServer(venv)) return venv;
  return null;
}

function isReady() {
  return resolve() !== null;
}

/**
 * Install one optional extra (`.[name]`) into the interpreter we already built.
 *
 * The extras are installed by hand here rather than left to provisioning, because
 * a venv that already resolves is reused without reinstalling — so `install` would
 * otherwise silently skip an extra the user just asked for. Both installers are
 * supported: a system-python venv has pip, a uv venv does not.
 *
 * Soft-fails: an extra is a capability, not the install. A failure is a warning and
 * the tool that needs it says so when called.
 */
function installExtra(name, { quiet = false } = {}) {
  const python = resolve();
  if (!python) return null;

  const uvBin = uv.find();
  const spec = `.[${name}]`;
  const result = uvBin
    ? run(uvBin, ['pip', 'install', '--python', python, '--quiet', '-e', spec], {
        cwd: paths.packageRoot(),
        timeout: LONG,
      })
    : run(
        python,
        ['-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-e', spec],
        { cwd: paths.packageRoot(), timeout: LONG }
      );

  if (!result.ok) {
    if (!quiet) log.warn(`could not install the optional ${name} extra${failureDetail(result)}`);
    return null;
  }
  return python;
}

/**
 * Guarantee a working interpreter.
 *
 * Safe to call from the installer and again, lazily, from the server entry point:
 * the first thing it does is check whether the work is already done.
 */
async function ensure({ force = false } = {}) {
  if (!force) {
    const ready = resolve();
    if (ready) return ready;
  }

  const system = findSystemPython();
  if (system) {
    log.step(`using system python ${system.version.join('.')} (${system.path})`);
    try {
      const created = createWithSystemPython(system);
      if (created && canImportServer(created)) return created;
    } catch (error) {
      log.warn(error.message);
    }
  } else {
    log.step('no system python 3.10+ found; falling back to uv');
  }

  const viaUv = await createWithUv();
  if (viaUv && canImportServer(viaUv)) return viaUv;

  const existing = resolve();
  if (existing) return existing;
  throw new Error(
    'could not provision a Python runtime for jev-use. Install Python 3.10+ or uv, ' +
      'then run: jev-use install. If an earlier attempt left a half-created ' +
      `environment behind, removing ${paths.venvDir()} clears the way.`
  );
}

module.exports = {
  ensure,
  resolve,
  isReady,
  installExtra,
  clearVenv,
  findSystemPython,
  devVenvPython,
  canImportServer,
  MIN_PYTHON,
  DEFAULT_PYTHON,
};
