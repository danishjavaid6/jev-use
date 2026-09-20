'use strict';

/**
 * The `.env` file the runtime reads, and where the API key comes from.
 *
 * The key is never written into the npm package directory and never logged. It
 * arrives either as an npm flag (`npm install jev-use --jev-key=...`, which npm
 * exposes to the installer as `npm_config_jev_key`) or as an already-exported
 * `TYPESAFE_API_KEY`.
 */

const fs = require('fs');
const path = require('path');

const { envFile } = require('./paths');

const KEY_NAMES = ['TYPESAFE_API_KEY'];

function parse(text) {
  const out = {};
  for (const rawLine of String(text || '').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const index = line.indexOf('=');
    const key = line.slice(0, index).trim();
    let value = line.slice(index + 1).trim();
    // Tolerate quoted values: a key pasted into a .env often arrives quoted.
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

function serialize(values) {
  const lines = [
    '# Written by the jev-use installer. One key per line, KEY=VALUE.',
    '# Read by the MCP server at startup; nothing here is logged.',
    '',
  ];
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === null || value === '') continue;
    lines.push(`${key}=${value}`);
  }
  return lines.join('\n') + '\n';
}

function readEnvFile(file = envFile()) {
  try {
    return parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return {};
  }
}

/** Merge into the existing file. Never drops a key the user already set. */
function writeEnvFile(values, file = envFile()) {
  const merged = { ...readEnvFile(file), ...values };
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, serialize(merged), { mode: 0o600 });
  try {
    fs.chmodSync(file, 0o600); // no-op on Windows, matters everywhere else
  } catch {
    /* best effort */
  }
  return merged;
}

/**
 * The API key the user supplied to `npm install`.
 *
 * npm maps an unknown flag to an environment variable for lifecycle scripts, so
 * `--jev-key=xyz` arrives as `npm_config_jev_key`. Both spellings are accepted
 * because `--api-key` reads naturally too.
 */
function keyFromInstallFlags(env = process.env) {
  const candidates = [
    'npm_config_jev_key',
    'npm_config_api_key',
    'npm_config_typesafe_api_key',
    'npm_config_jev_use_key',
  ];
  for (const name of candidates) {
    const value = (env[name] || '').trim();
    if (value) return { value, source: `--${name.replace(/^npm_config_/, '').replace(/_/g, '-')}` };
  }
  const exported = (env.TYPESAFE_API_KEY || '').trim();
  if (exported) return { value: exported, source: 'TYPESAFE_API_KEY' };
  return null;
}

/** Mask a secret for display: enough to recognise it, not enough to use it. */
function mask(secret) {
  const text = String(secret || '');
  if (text.length <= 8) return '*'.repeat(text.length);
  return `${text.slice(0, 4)}...${text.slice(-4)}`;
}

function hasKey(file = envFile()) {
  const values = readEnvFile(file);
  return KEY_NAMES.some((name) => Boolean(values[name]));
}

module.exports = {
  KEY_NAMES,
  parse,
  serialize,
  readEnvFile,
  writeEnvFile,
  keyFromInstallFlags,
  mask,
  hasKey,
};
