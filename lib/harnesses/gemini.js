'use strict';

/**
 * Gemini CLI — `~/.gemini/settings.json`, a plain `mcpServers` map.
 *
 * User-scope, so the same shape on every platform; the file also holds `theme`
 * and other settings, which the merge-never-replace write leaves alone.
 */

const path = require('path');

const paths = require('../paths');
const { create } = require('./generic');

const dir = () => path.join(paths.HOME, '.gemini');

module.exports = create({
  id: 'gemini',
  name: 'Gemini CLI',
  configFile: () => path.join(dir(), 'settings.json'),
});
