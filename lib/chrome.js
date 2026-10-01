'use strict';

/**
 * Locating Chrome. The JS twin of `host.find_browser_binary()`.
 *
 * Used only for diagnostics ("is a browser even installed?"), which is why it
 * stops at finding a path rather than doing anything with it. It exists because
 * "the tools return nothing" is very often "Chrome is not installed", and saying
 * that outright saves a long detour.
 */

const fs = require('fs');
const path = require('path');

const paths = require('./paths');
const { run, which } = require('./run');

const POSIX_NAMES = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome'];

const WINDOWS_RELATIVE = [
  path.join('Google', 'Chrome', 'Application', 'chrome.exe'),
  path.join('Google', 'Chrome Beta', 'Application', 'chrome.exe'),
  path.join('Google', 'Chrome SxS', 'Application', 'chrome.exe'),
];

const MACOS_RELATIVE = [
  path.join('Google Chrome.app', 'Contents', 'MacOS', 'Google Chrome'),
  path.join('Google Chrome Canary.app', 'Contents', 'MacOS', 'Google Chrome Canary'),
  path.join('Chromium.app', 'Contents', 'MacOS', 'Chromium'),
];

/** Chrome's own App Paths entry — the one place it always registers on Windows. */
function fromRegistry() {
  const keys = [
    'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe',
    'HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe',
  ];
  for (const key of keys) {
    const result = run('reg', ['query', key, '/ve']);
    if (!result.ok) continue;
    const match = /REG_SZ\s+(.+)/.exec(result.stdout);
    if (match && fs.existsSync(match[1].trim())) return match[1].trim();
  }
  return null;
}

function find() {
  if (process.env.JEV_USE_BROWSER_BINARY) {
    return fs.existsSync(process.env.JEV_USE_BROWSER_BINARY) ? process.env.JEV_USE_BROWSER_BINARY : null;
  }
  for (const name of paths.IS_WINDOWS ? [] : POSIX_NAMES) {
    const found = which([paths.IS_WINDOWS ? `${name}.exe` : name]);
    if (found) return found;
  }

  const candidates = [];
  if (paths.IS_WINDOWS) {
    for (const base of [process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA]) {
      if (base) candidates.push(...WINDOWS_RELATIVE.map((rel) => path.join(base, rel)));
    }
  } else if (paths.IS_MACOS) {
    for (const rel of MACOS_RELATIVE) {
      candidates.push(path.join('/Applications', rel), path.join(paths.HOME, 'Applications', rel));
    }
  }

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }

  if (paths.IS_WINDOWS) return fromRegistry();
  return null;
}

module.exports = { find };
