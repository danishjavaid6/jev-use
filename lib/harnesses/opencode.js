'use strict';

/**
 * OpenCode — `mcp` block in its config, with a different shape from the rest:
 * `command` is a single array and the env key is `environment`.
 *
 *     "mcp": { "servers": { "jev-use": { "type": "local", "command": ["node", "..."] } } }
 *
 * OpenCode's config may be `opencode.json` or `opencode.jsonc`. We write whichever
 * already exists, and if it is the JSONC one and it contains comments we refuse
 * rather than risk destroying them.
 */

const fs = require('fs');
const path = require('path');

const paths = require('../paths');
const { SERVER_NAME, readJson, readJsonServer, writeJson } = require('./util');

function configDir() {
  // OpenCode uses XDG paths on Windows too (`opencode debug paths`).
  // Do not use configBase(): that is the native APPDATA path for other apps.
  const xdg = process.env.XDG_CONFIG_HOME;
  return path.join(xdg && xdg.trim() ? xdg : path.join(paths.HOME, '.config'), 'opencode');
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
  configDir,
  skillsDir: () => null,
  detect: () => fs.existsSync(configDir()),
  registered: () => readJsonServer(configFile(), (config) => config.mcp?.servers?.[SERVER_NAME] || config.mcp?.[SERVER_NAME]),
  register(entry) {
    const file = configFile();
    const config = readJson(file);
    config.mcp = config.mcp || {};
    config.mcp.servers = config.mcp.servers || {};
    config.mcp.servers[SERVER_NAME] = {
      type: 'local',
      command: [entry.command, ...entry.args],
      disabled: false,
      ...(entry.env && Object.keys(entry.env).length
        ? { environment: { ...entry.env } }
        : {}),
    };
    // Replace only our legacy entry; leave unrelated user configuration intact.
    delete config.mcp[SERVER_NAME];
    if (!config.$schema && file.endsWith('.json')) {
      config.$schema = 'https://opencode.ai/config.json';
    }
    writeJson(file, config);
    return file;
  },
};
