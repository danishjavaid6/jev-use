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

//: Every skill shipped as a slash command goes to every harness — a `mobile-use`
//: user must not have to discover that the phone skill exists under another name.
const SKILLS = ['browser-use', 'mobile-use', 'facebook-create-pages'];

/** Every per-harness skills directory we know of. */
function targetDirs() {
  const home = paths.HOME;
  // This is the cross-harness, user-scoped location. Unlike an individual
  // harness directory it may not exist yet on a fresh machine, so install it
  // unconditionally below. Harnesses that understand the shared agent-skills
  // convention can then expose both slash commands without a project folder.
  const shared = path.join(home, '.agents', 'skills');
  const dirs = [
    path.join(home, '.commandcode', 'skills'),
    path.join(home, '.claude', 'skills'),
    path.join(home, '.codex', 'skills'),
    // The shared agent-skills location several harnesses read from.
    shared,
    // POSIX reads `$XDG_CONFIG_HOME/opencode`; Windows uses `%APPDATA%`.
    path.join(paths.configBase(), 'opencode', 'skills'),
  ];
  return dirs;
}

function sameBundle(source, destination) {
  if (!fs.existsSync(destination)) return false;
  for (const item of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, item.name);
    const to = path.join(destination, item.name);
    if (item.isDirectory()) { if (!sameBundle(from, to)) return false; }
    else if (!item.isFile() || !fs.existsSync(to) || !fs.statSync(to).isFile() || !fs.readFileSync(from).equals(fs.readFileSync(to))) return false;
  }
  return true;
}

/**
 * Update complete skill bundles in the shared root and installed harnesses' global roots.
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
      // `.agents/skills` is the global shared location. Create it even when no
      // harness has made the directory yet; this is what makes installation
      // independent of the current project and usable from a later harness.
      const sharedRoot = root === path.join(paths.HOME, '.agents', 'skills');
      if (!sharedRoot && !fs.existsSync(path.dirname(root))) continue;
      const destination = path.join(root, name, 'SKILL.md');
      const sourceDir = path.dirname(source);
      const destinationDir = path.dirname(destination);
      if (!force && sameBundle(sourceDir, destinationDir)) {
        results.push({ skill: name, file: destination, ok: true, skipped: true });
        continue;
      }
      try {
        fs.mkdirSync(path.dirname(destination), { recursive: true });
        fs.cpSync(sourceDir, destinationDir, { recursive: true, force: true });
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
