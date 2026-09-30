'use strict';

/**
 * A harness whose user-scope MCP config is a JSON file holding a map of servers.
 *
 * Claude Code, Cursor and Command Code each got their own file because each has a
 * quirk worth its own comments. Most harnesses do not: they all read
 * `{ "<key>": { "<name>": { "command", "args"[, "env"] } } }` and differ only in
 * which file they read and what that key is called. This factory is what lets a
 * new one be a few lines of data instead of a copy of the same adapter.
 *
 * Every field that varies is an option; everything destructive is inherited from
 * `util.js`, so a harness added through here gets the same merge-not-clobber and
 * one-time-backup guarantees as the hand-written ones.
 */

const fs = require('fs');
const path = require('path');

const { SERVER_NAME, readJson, readJsonServer, writeJson, stdioEntry } = require('./util');

/**
 * @param {object} spec
 * @param {string} spec.id            lowercase id, used by `--harness=`
 * @param {string} spec.name          display name for logs and doctor
 * @param {() => string} spec.configFile
 * @param {string} [spec.key]         the servers map key (default `mcpServers`)
 * @param {string} [spec.type]        a `type: <value>` added to the entry (VS Code)
 * @param {() => string|null} [spec.skillsDir]
 * @param {() => boolean} [spec.detect]
 */
function create(spec) {
  const { id, name, configFile } = spec;
  const key = spec.key || 'mcpServers';
  const skillsDir = spec.skillsDir || (() => null);

  // A config *directory* existing is enough to register, so a harness that has
  // been installed but never configured still gets picked up — the same loose
  // rule the hand-written adapters use.
  const detect =
    spec.detect ||
    (() => {
      const file = configFile();
      return fs.existsSync(file) || fs.existsSync(path.dirname(file));
    });

  return {
    id,
    name,
    configFile,
    skillsDir,
    detect,
    registered: () => readJsonServer(configFile(), (config) => config[key] && config[key][SERVER_NAME]),
    register(entry) {
      const file = configFile();
      const config = readJson(file);
      config[key] = config[key] || {};
      config[key][SERVER_NAME] = {
        ...(spec.type ? { type: spec.type } : {}),
        ...stdioEntry(entry),
      };
      writeJson(file, config);
      return file;
    },
  };
}

module.exports = { create };
