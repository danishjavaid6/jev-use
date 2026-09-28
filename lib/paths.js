'use strict';

/**
 * Every path that differs between Linux, macOS and Windows.
 *
 * The JS twin of `jev_use/host.py`, and it has to agree with it: the Python
 * engine reads profiles from here and writes its state to here, so a mismatch
 * would mean the installer prepares a venv the server never looks at.
 *
 * Everything is resolved through `paths.HOME` rather than a captured constant,
 * so overriding HOME on the exports object redirects *all* of it — a half-honoured
 * override would be a genuinely confusing bug to chase.
 */

const os = require('os');
const path = require('path');

const paths = {
  IS_WINDOWS: process.platform === 'win32',
  IS_MACOS: process.platform === 'darwin',
  HOME: os.homedir(),
};

/** Mirrors `host.work_root()`: our profiles live under <state>/profiles. */
paths.stateDir = function stateDir() {
  if (paths.IS_WINDOWS) return path.join(paths.localAppData(), 'jev-use');
  if (paths.IS_MACOS) return path.join(paths.HOME, 'Library', 'Application Support', 'jev-use');
  return path.join(paths.HOME, '.local', 'state', 'jev-use');
};

paths.localAppData = function localAppData() {
  return process.env.LOCALAPPDATA || path.join(paths.HOME, 'AppData', 'Local');
};

paths.venvDir = function venvDir() {
  return path.join(paths.stateDir(), 'venv');
};

paths.venvPython = function venvPython() {
  return paths.IS_WINDOWS
    ? path.join(paths.venvDir(), 'Scripts', 'python.exe')
    : path.join(paths.venvDir(), 'bin', 'python');
};

paths.toolsDir = function toolsDir() {
  return path.join(paths.stateDir(), 'tools');
};

/**
 * The Jev API key lives here, not in the package directory: that directory is
 * shared and world-readable, and a key must not be.
 */
paths.envFile = function envFile() {
  return path.join(paths.stateDir(), '.env');
};

paths.readyMarker = function readyMarker() {
  return path.join(paths.stateDir(), 'installed.json');
};

paths.profileRoot = function profileRoot() {
  if (paths.IS_WINDOWS) return path.join(paths.localAppData(), 'Google', 'Chrome', 'User Data');
  if (paths.IS_MACOS) {
    return path.join(paths.HOME, 'Library', 'Application Support', 'Google', 'Chrome');
  }
  return path.join(paths.HOME, '.config', 'google-chrome');
};

paths.packageRoot = function packageRoot() {
  return path.resolve(__dirname, '..');
};

/** The stdio entry point a harness spawns. Absolute, because harness configs
 *  store one command string and never a working directory. */
paths.mcpEntry = function mcpEntry() {
  return path.join(paths.packageRoot(), 'bin', 'jev-use-mcp.js');
};

/** The SKILL.md shipped for one slash command, e.g. `browser-use` / `mobile-use`. */
paths.skillSource = function skillSource(name = 'browser-use') {
  return path.join(paths.packageRoot(), 'skills', name, 'SKILL.md');
};

module.exports = paths;
