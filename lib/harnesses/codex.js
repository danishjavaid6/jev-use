'use strict';

/**
 * Codex — `~/.codex/config.toml`, the one non-JSON config.
 *
 * Written as a surgical block replace rather than a TOML round-trip, because
 * this file holds hand-written `model`, `model_providers` and `projects` tables
 * that a parser/serializer pair would reflow.
 */

const fs = require('fs');
const path = require('path');

const paths = require('../paths');
const { SERVER_NAME, setTomlBlock, tomlString, readTomlServer } = require('./util');

const dir = () => path.join(paths.HOME, '.codex');
const configFile = () => path.join(dir(), 'config.toml');

module.exports = {
  id: 'codex',
  name: 'Codex',
  configFile,
  skillsDir: () => path.join(dir(), 'skills'),
  detect: () => fs.existsSync(dir()),
  registered: () => readTomlServer(configFile(), SERVER_NAME),
  register(entry) {
    const args = entry.args.map(tomlString).join(', ');
    const lines = [`command = ${tomlString(entry.command)}`];
    if (args) lines.push(`args = [${args}]`);
    // The first call in a fresh process starts uv/python and the driver; Codex's
    // default tool timeout is short enough to cut that off.
    lines.push('startup_timeout_sec = 120');
    setTomlBlock(configFile(), SERVER_NAME, lines);
    return configFile();
  },
};
