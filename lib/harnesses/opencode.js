'use strict';

/**
 * OpenCode — `mcp` block in its config, with a different shape from the rest:
 * `command` is a single array and the env key is `environment`.
 *
 *     "mcp": { "jev-use": { "type": "local", "command": ["node", "..."] } }
 *
 * OpenCode's config may be `opencode.json` or `opencode.jsonc`. We write whichever
 * already exists, and if it is the JSONC one and it contains comments we refuse
 * rather than risk destroying them.
 */

const fs = require('fs');
const path = require('path');

const paths = require('../paths');
const { SERVER_NAME, readJson, writeJson } = require('./util');

function configDir() {
  const xdg = process.env.XDG_CONFIG_HOME;
  const base = xdg && xdg.trim() ? xdg : path.join(paths.HOME, '.config');
  return path.join(base, 'opencode');
}

function configFile() {
  const dir = configDir();
  const json = path.join(dir, 'opencode.json');
  const jsonc = path.join(dir, 'opencode.jsonc');
  if (fs.existsSync(json)) return json;
  if (fs.existsSync(jsonc)) return jsonc;
  return json;
}

module.exports = {
  id: 'opencode',
  name: 'OpenCode',
  configFile,
  skillsDir: () => null,
  detect: () => fs.existsSync(configDir()),
  register(entry) {
    const file = configFile();
    const config = readJson(file);
    config.mcp = config.mcp || {};
    config.mcp[SERVER_NAME] = {
      type: 'local',
      command: [entry.command, ...entry.args],
      enabled: true,
      ...(entry.env && Object.keys(entry.env).length
        ? { environment: { ...entry.env } }
        : {}),
    };
    if (!config.$schema && file.endsWith('.json')) {
      config.$schema = 'https://opencode.ai/config.json';
    }
    writeJson(file, config);
    return file;
  },
};
