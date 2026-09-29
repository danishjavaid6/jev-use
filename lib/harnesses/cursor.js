'use strict';

/**
 * Cursor — `~/.cursor/mcp.json`, merged with a project-level `.cursor/mcp.json`
 * if one exists. This writes the global one.
 *
 * Cursor has no CLI for MCP, so the file is the only route. It also has no
 * SKILL.md convention (its equivalent is `.cursor/rules`), so there is nothing
 * to install for skills.
 */

const fs = require('fs');
const path = require('path');

const paths = require('../paths');
const { SERVER_NAME, readJson, readJsonServer, writeJson, stdioEntry } = require('./util');

const dir = () => path.join(paths.HOME, '.cursor');
const configFile = () => path.join(dir(), 'mcp.json');

module.exports = {
  id: 'cursor',
  name: 'Cursor',
  configFile,
  skillsDir: () => null,
  detect: () => fs.existsSync(dir()),
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
