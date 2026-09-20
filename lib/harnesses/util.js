'use strict';

/**
 * Shared config-file plumbing for the harness adapters.
 *
 * Two rules, both about not being destructive:
 *
 *   * **Merge, never replace.** Every harness config already holds other
 *     servers and unrelated settings. We read it, add one key, write it back.
 *   * **Back up once, before the first change.** If a write is wrong, the
 *     original is one `cp` away, and a second run does not overwrite that backup
 *     with our own mangled output.
 */

const fs = require('fs');
const path = require('path');

const SERVER_NAME = 'jev-use';

function backup(file) {
  if (!fs.existsSync(file)) return null;
  const target = `${file}.jev-use.bak`;
  if (fs.existsSync(target)) return target;
  fs.copyFileSync(file, target);
  return target;
}

/**
 * Parse a JSON or JSONC config.
 *
 * A `.jsonc` file with real comments fails to parse, and guessing at stripping
 * them risks corrupting something a person wrote. So we refuse and say so.
 */
function readJson(file) {
  if (!fs.existsSync(file)) return {};
  const text = fs.readFileSync(file, 'utf8');
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch (error) {
    const hint = file.endsWith('.jsonc')
      ? 'It contains comments, which this installer will not risk rewriting. ' +
        `Add the server by hand (see README), or move the settings to ${file.replace(/\.jsonc$/, '.json')}.`
      : 'It is not valid JSON, so it will not be rewritten.';
    throw new Error(`${file}: ${error.message}. ${hint}`);
  }
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const existing = backup(file);
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
  return existing;
}

/**
 * Add or replace one `[mcp_servers.<name>]` TOML block, leaving everything else
 * byte-for-byte alone.
 *
 * Codex is the only harness here that is not JSON, and its config file is the
 * most hand-edited of the set (`model`, `projects.*`, provider auth), so this
 * surgically replaces just the block it owns.
 */
function setTomlBlock(file, name, lines) {
  const header = `[mcp_servers.${name}]`;
  const block = [header, ...lines].join('\n') + '\n';

  const text = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
  const existing = backup(file);

  // Scan line by line rather than regex: the block owns its own nested tables
  // (`[mcp_servers.x.env]`), and a regex that has to look ahead for "the next
  // top-level header or EOF" is easy to get subtly wrong on the last block in
  // the file. A header whose name merely *starts* with ours is a different
  // server and must be left alone, hence the explicit boundary check.
  const startedBy = (trimmed) => {
    if (!trimmed.startsWith(`[mcp_servers.${name}`)) return false;
    const next = trimmed[`[mcp_servers.${name}`.length];
    return next === ']' || next === '.';
  };

  const kept = [];
  let skipping = false;
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (trimmed.startsWith('[')) {
      if (startedBy(trimmed)) {
        skipping = true;
        continue;
      }
      skipping = false;
    }
    if (!skipping) kept.push(line);
  }

  const cleaned = kept.join('\n').replace(/\n{3,}/g, '\n\n').replace(/\s+$/, '');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${cleaned ? `${cleaned}\n\n` : ''}${block}`);
  return existing;
}

function tomlString(value) {
  return JSON.stringify(String(value));
}

/** The `{command, args, env}` triple every harness ultimately needs. */
function stdioEntry(entry) {
  return {
    command: entry.command,
    args: [...entry.args],
    ...(entry.env && Object.keys(entry.env).length ? { env: { ...entry.env } } : {}),
  };
}

module.exports = { SERVER_NAME, backup, readJson, writeJson, setTomlBlock, tomlString, stdioEntry };
