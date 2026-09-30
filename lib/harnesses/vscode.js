'use strict';

/**
 * VS Code (and anything built on its MCP support, Copilot included) — the user
 * profile's `mcp.json`.
 *
 * Two things differ from the rest:
 *
 *   * the servers map is called `servers`, not `mcpServers`;
 *   * each entry carries an explicit `"type": "stdio"`, which is how VS Code
 *     decides whether the command is a process or a URL.
 *
 * The profile directory is not XDG anywhere: macOS keeps it under Application
 * Support, Windows under `%APPDATA%`, and only Linux matches the `~/.config`
 * default. Hence the explicit branch rather than `paths.configBase()`.
 */

const path = require('path');

const paths = require('../paths');
const { create } = require('./generic');

function userDir() {
  if (paths.IS_WINDOWS) return path.join(paths.appData(), 'Code', 'User');
  if (paths.IS_MACOS) {
    return path.join(paths.HOME, 'Library', 'Application Support', 'Code', 'User');
  }
  return path.join(paths.configBase(), 'Code', 'User');
}

module.exports = create({
  id: 'vscode',
  name: 'VS Code',
  configFile: () => path.join(userDir(), 'mcp.json'),
  key: 'servers',
  type: 'stdio',
});
