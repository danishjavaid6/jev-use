'use strict';

/** Command Code — `~/.commandcode/mcp.json`. */

const fs = require('fs');
const path = require('path');

const paths = require('../paths');
const { SERVER_NAME, readJson, readJsonServer, writeJson, stdioEntry } = require('./util');

const dir = () => path.join(paths.HOME, '.commandcode');
const configFile = () => path.join(dir(), 'mcp.json');

// Command Code 1.53.x starts stdio with shell:true on Windows, concatenating
// command and args without quoting (createStdioMcpClient). Scope this workaround
// to its adapter: direct-spawn clients need the original, unquoted paths.
function quoteWindows(value) {
  if (/["%\r\n]/.test(value)) throw new Error('Unsupported character in Command Code launcher path');
  return `"${value}"`;
}

module.exports = {
  id: 'commandcode',
  name: 'Command Code',
  configFile,
  skillsDir: () => path.join(dir(), 'skills'),
  detect: () => fs.existsSync(dir()),
  registered: () =>
    readJsonServer(configFile(), (config) => config.mcpServers && config.mcpServers[SERVER_NAME]),
  usesShell: () => paths.IS_WINDOWS,
  quoteWindows,
  register(entry) {
    const file = configFile();
    const config = readJson(file);
    config.mcpServers = config.mcpServers || {};
    // Command Code's schema carries two extra keys the other harnesses do not
    // have; without them the entry is accepted but never started.
    const launch = stdioEntry(entry);
    if (paths.IS_WINDOWS) {
      launch.command = quoteWindows(launch.command);
      launch.args = launch.args.map(quoteWindows);
    }
    config.mcpServers[SERVER_NAME] = {
      transport: 'stdio',
      enabled: true,
      ...launch,
    };
    writeJson(file, config);
    return file;
  },
};
