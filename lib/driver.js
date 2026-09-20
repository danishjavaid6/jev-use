'use strict';

/**
 * Installing `cua-driver`, the one piece that is not ours.
 *
 * It is a Rust binary published on GitHub Releases, and it has a *separate
 * installer per platform*: `install.sh` contains no Windows handling at all and
 * a Windows build gets `install.ps1` instead. Handing a Windows machine the
 * curl-pipe-bash command is a dead end, which is why this branches.
 *
 * After installing we re-resolve the absolute path and pin it as
 * `CUA_DRIVER_COMMAND`. That is not belt-and-braces: on Windows the installer
 * appends to the *User* PATH, a change an already-running process cannot see, so
 * relying on PATH alone would leave the harness unable to find a driver that is
 * definitely installed.
 */

const fs = require('fs');
const path = require('path');

const log = require('./log');
const paths = require('./paths');
const { run, which } = require('./run');

const INSTALL_SH = 'https://cua.ai/driver/install.sh';
const INSTALL_PS1 = 'https://cua.ai/driver/install.ps1';

/** Mirrors `host.cua_driver_candidates()` — where each installer puts it. */
function candidates() {
  const home = paths.HOME;
  if (paths.IS_WINDOWS) {
    const local = process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local');
    return [
      path.join(local, 'Programs', 'Cua', 'cua-driver', 'bin', 'cua-driver.exe'),
      path.join(local, 'Programs', 'trycua', 'cua-driver-rs', 'bin', 'cua-driver.exe'),
      path.join(home, '.cua-driver', 'packages', 'current', 'cua-driver.exe'),
    ];
  }
  return [
    path.join(home, '.local', 'bin', 'cua-driver'),
    path.join(home, '.cua-driver', 'packages', 'current', 'cua-driver'),
  ];
}

/**
 * Resolve the driver.
 *
 * `CUA_DRIVER_COMMAND` wins, then PATH, then the known install directories. The
 * same order the Python side uses, so both agree on which binary is in play.
 */
function find() {
  const override = (process.env.CUA_DRIVER_COMMAND || '').trim();
  if (override) return override;

  const name = paths.IS_WINDOWS ? 'cua-driver.exe' : 'cua-driver';
  const onPath = which([name]);
  if (onPath) return onPath;

  for (const candidate of candidates()) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

async function download(url, destination) {
  const response = await fetch(url, { redirect: 'follow' });
  if (!response.ok) {
    throw new Error(`GET ${url} -> ${response.status} ${response.statusText}`);
  }
  const buffer = Buffer.from(await response.arrayBuffer());
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, buffer);
  return destination;
}

/**
 * Run the platform installer.
 *
 * The script is downloaded to disk first and then executed, rather than piped
 * from the network straight into a shell — same result, but a failed download
 * cannot half-execute.
 */
async function install() {
  const script = paths.IS_WINDOWS ? 'install.ps1' : 'install.sh';
  const url = paths.IS_WINDOWS ? INSTALL_PS1 : INSTALL_SH;
  const local = path.join(paths.toolsDir(), script);

  log.step(`fetching the cua-driver ${script}`);
  await download(url, local);

  log.step(`running ${script}`);
  const result = paths.IS_WINDOWS
    ? run(
        'powershell',
        ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', local],
        { timeout: 10 * 60 * 1000 }
      )
    : run('/bin/bash', [local], { timeout: 10 * 60 * 1000 });

  if (!result.ok) {
    throw new Error(
      `${script} failed (exit ${result.status}): ` +
        `${(result.stderr || result.stdout || '').trim().slice(0, 400)}`
    );
  }
  return find();
}

/**
 * Ensure the driver is present.
 *
 * Returns the absolute path, or null when it could not be installed — the caller
 * reports that rather than throwing, because a missing driver is a "finish this
 * later" state, not a broken install.
 */
async function ensure({ force = false } = {}) {
  if (!force) {
    const existing = find();
    if (existing) return existing;
  }
  // Presence, not `--version`: PowerShell has no `--version` flag, so probing it
  // that way reports "no shell" on a Windows box that has one, and the driver
  // install would be skipped in silence.
  const shell = which(paths.IS_WINDOWS ? ['powershell', 'pwsh'] : ['/bin/bash', 'bash']);
  if (!shell) {
    log.warn('no shell available to run the driver installer');
    return null;
  }
  try {
    const installed = await install();
    if (installed) {
      log.ok(`cua-driver installed: ${installed}`);
      return installed;
    }
    log.warn('the installer finished but no cua-driver binary was found');
    return null;
  } catch (error) {
    log.warn(`could not install cua-driver: ${error.message}`);
    log.say(
      paths.IS_WINDOWS
        ? '    retry by hand:  irm https://cua.ai/driver/install.ps1 | iex'
        : `    retry by hand:  /bin/bash -c "$(curl -fsSL ${INSTALL_SH})"`
    );
    return null;
  }
}

module.exports = { find, ensure, install, candidates, INSTALL_SH, INSTALL_PS1 };
