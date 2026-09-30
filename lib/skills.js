'use strict';

/**
 * Installing the slash-command skills: `/browser-use` and `/mobile-use`.
 *
 * The MCP server provides the tools; the skills are what tell a harness *how* to
 * use them (discover, open, drive, read, report). Harnesses that surface skills
 * find them in a per-harness directory, so the same SKILL.md is copied into each
 * one that exists.
 *
 * This is deliberately independent of MCP registration: being registered and
 * being documented are separate things, and a skills directory can exist for a
 * harness whose MCP config we do not manage.
 */

const fs = require('fs');
const path = require('path');

const log = require('./log');
const paths = require('./paths');

//: Every skill shipped as a slash command. Both go to every harness — a `mobile-use`
//: user must not have to discover that the phone skill exists under another name.
const SKILLS = ['browser-use', 'mobile-use'];

/** Every per-harness skills directory we know of. */
function targetDirs() {
  const home = paths.HOME;
  const dirs = [
    path.join(home, '.commandcode', 'skills'),
    path.join(home, '.claude', 'skills'),
    path.join(home, '.codex', 'skills'),
    // The shared agent-skills location several harnesses read from.
    path.join(home, '.agents', 'skills'),
    // POSIX reads `$XDG_CONFIG_HOME/opencode`; Windows uses `%APPDATA%`.
    path.join(paths.configBase(), 'opencode', 'skills'),
  ];
  return dirs;
}

/**
 * Copy SKILL.md into every skills root that already exists.
 *
 * Only into roots that exist: creating `~/.codex/skills` on a machine without
 * Codex would be clutter, and would make the next harness install think it had
 * already been configured.
 */
function install({ force = false } = {}) {
  const results = [];

  for (const name of SKILLS) {
    const source = paths.skillSource(name);
    if (!fs.existsSync(source)) {
      log.warn(`skill source missing: ${source}`);
      continue;
    }

    for (const root of targetDirs()) {
      if (!fs.existsSync(path.dirname(root))) continue;
      const destination = path.join(root, name, 'SKILL.md');
      if (fs.existsSync(destination) && !force) {
        results.push({ skill: name, file: destination, ok: true, skipped: true });
        continue;
      }
      try {
        fs.mkdirSync(path.dirname(destination), { recursive: true });
        fs.copyFileSync(source, destination);
        log.ok(`skill /${name} -> ${destination}`);
        results.push({ skill: name, file: destination, ok: true, skipped: false });
      } catch (error) {
        log.warn(`could not install /${name} into ${root}: ${error.message}`);
        results.push({ skill: name, file: destination, ok: false, error: error.message });
      }
    }
  }

  if (!results.some((r) => r.ok)) {
    log.warn(
      `no skills directory found; the tools still work, only ` +
        `${SKILLS.map((name) => `/${name}`).join(' and ')} are missing`
    );
  }
  return results;
}

module.exports = { install, targetDirs, SKILLS };
