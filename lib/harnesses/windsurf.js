'use strict';

/**
 * Windsurf — `~/.codeium/windsurf/mcp_config.json`, a plain `mcpServers` map.
 *
 * A user-scope file, so it is the same shape on every platform (Windsurf's
 * per-user directory is `%USERPROFILE%\.codeium\windsurf` on Windows, which is
 * the same path with a different home).
 */

const path = require('path');

const paths = require('../paths');
const { create } = require('./generic');

const dir = () => path.join(paths.HOME, '.codeium', 'windsurf');

module.exports = create({
  id: 'windsurf',
  name: 'Windsurf',
  configFile: () => path.join(dir(), 'mcp_config.json'),
});
