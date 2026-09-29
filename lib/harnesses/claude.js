'use strict';

/**
 * Claude Code — user-scope servers live in `~/.claude.json`, under `mcpServers`.
 *
 * The same shape as Cursor's file, so this and `cursor.js` differ only in which
 * file they touch and where skills go.
 */

const fs = require('fs');
const path = require('path');

const paths = require('../paths');
const { SERVER_NAME, readJson, readJsonServer, writeJson, stdioEntry } = require('./util');

const dir = () => path.join(paths.HOME, '.claude');
const configFile = () => path.join(paths.HOME, '.claude.json');

module.exports = {
  id: 'claude',
  name: 'Claude Code',
  configFile,
  skillsDir: () => path.join(dir(), 'skills'),
  detect: () => fs.existsSync(configFile()) || fs.existsSync(dir()),
  registered: () =>
    readJsonServer(configFile(), (config) => config.mcpServers && config.mcpServers[SERVER_NAME]),
  register(entry) {
    const file = configFile();
    const config = readJson(file);
    config.mcpServers = config.mcpServers || {};
    config.mcpServers[SERVER_NAME] = stdioEntry(entry);
    writeJson(file, config);
    return file;
  },
};
