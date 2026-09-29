'use strict';

/** Command Code — `~/.commandcode/mcp.json`. */

const fs = require('fs');
const path = require('path');

const paths = require('../paths');
const { SERVER_NAME, readJson, readJsonServer, writeJson, stdioEntry } = require('./util');

const dir = () => path.join(paths.HOME, '.commandcode');
const configFile = () => path.join(dir(), 'mcp.json');

module.exports = {
  id: 'commandcode',
  name: 'Command Code',
  configFile,
  skillsDir: () => path.join(dir(), 'skills'),
  detect: () => fs.existsSync(dir()),
  registered: () =>
    readJsonServer(configFile(), (config) => config.mcpServers && config.mcpServers[SERVER_NAME]),
  register(entry) {
    const file = configFile();
    const config = readJson(file);
    config.mcpServers = config.mcpServers || {};
    // Command Code's schema carries two extra keys the other harnesses do not
    // have; without them the entry is accepted but never started.
    config.mcpServers[SERVER_NAME] = {
      transport: 'stdio',
      enabled: true,
      ...stdioEntry(entry),
    };
    writeJson(file, config);
    return file;
  },
};
