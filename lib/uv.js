'use strict';

/**
 * `uv`, used only when the machine has no usable Python.
 *
 * The whole "just `npm install` it" promise rests on this: uv is a single static
 * binary that can install its own CPython, so a device with Node and Chrome but
 * no Python still ends up with a working server.
 *
 * When a system Python 3.10+ already exists we never get here — a plain
 * `python -m venv` is fewer moving parts, so it wins.
 */

const fs = require('fs');
const path = require('path');

const log = require('./log');
const paths = require('./paths');
const { run, which } = require('./run');

const RELEASE_BASE = 'https://github.com/astral-sh/uv/releases/latest/download';

/** uv's release asset names are keyed on target triple, not on `process.platform`. */
function assetName() {
  const arch = process.arch;
  if (process.platform === 'win32') {
    if (arch === 'arm64') return 'uv-aarch64-pc-windows-msvc.zip';
    return 'uv-x86_64-pc-windows-msvc.zip';
  }
  if (process.platform === 'darwin') {
    if (arch === 'arm64') return 'uv-aarch64-apple-darwin.tar.gz';
    return 'uv-x86_64-apple-darwin.tar.gz';
  }
  if (arch === 'arm64') return 'uv-aarch64-unknown-linux-gnu.tar.gz';
  return 'uv-x86_64-unknown-linux-gnu.tar.gz';
}

function binaryName() {
  return paths.IS_WINDOWS ? 'uv.exe' : 'uv';
}

/** Where `ensure()` puts it. Kept out of PATH so nothing else is disturbed. */
function managedPath() {
  return path.join(paths.toolsDir(), binaryName());
}

function find() {
  const onPath = which([binaryName()]);
  if (onPath) return onPath;
  const managed = managedPath();
  return fs.existsSync(managed) ? managed : null;
}

/** Recursively locate the extracted binary inside the archive's versioned folder. */
function locate(root, name) {
  const stack = [root];
  while (stack.length) {
    const current = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (entry.name === name) return full;
    }
  }
  return null;
}

async function download() {
  const name = assetName();
  const url = `${RELEASE_BASE}/${name}`;
  log.step(`downloading uv (${name})`);

  const response = await fetch(url, { redirect: 'follow' });
  if (!response.ok) {
    throw new Error(`GET ${url} -> ${response.status} ${response.statusText}`);
  }
  const archive = path.join(paths.toolsDir(), name);
  fs.mkdirSync(paths.toolsDir(), { recursive: true });
  fs.writeFileSync(archive, Buffer.from(await response.arrayBuffer()));

  // `tar` handles .tar.gz everywhere and .zip on Windows 10+ (bsdtar), which
  // avoids pulling in an extraction dependency just for one archive.
  const extractDir = path.join(paths.toolsDir(), 'uv-extract');
  fs.rmSync(extractDir, { recursive: true, force: true });
  fs.mkdirSync(extractDir, { recursive: true });

  const extracted = run('tar', ['-xf', archive, '-C', extractDir], { timeout: 5 * 60 * 1000 });
  if (!extracted.ok) {
    throw new Error(`could not extract ${archive}: ${extracted.stderr.trim()}`);
  }

  const binary = locate(extractDir, binaryName());
  if (!binary) throw new Error(`${name} did not contain ${binaryName()}`);

  fs.copyFileSync(binary, managedPath());
  if (!paths.IS_WINDOWS) fs.chmodSync(managedPath(), 0o755);
  fs.rmSync(archive, { force: true });
  fs.rmSync(extractDir, { recursive: true, force: true });
  return managedPath();
}

async function ensure() {
  const existing = find();
  if (existing) return existing;
  try {
    const installed = await download();
    log.ok(`uv ready: ${installed}`);
    return installed;
  } catch (error) {
    log.warn(`could not fetch uv: ${error.message}`);
    return null;
  }
}

module.exports = { find, ensure, assetName, managedPath, RELEASE_BASE };
