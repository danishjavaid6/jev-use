'use strict';
// Private JSON-lines transport. Snippets travel over stdin, never shell arguments.
const readline = require('node:readline');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { createRequire } = require('node:module');
const { watchNativeDialogs } = require('./native-dialogs');

function resolveSDK(cli) {
  const roots = [__dirname, path.dirname(process.execPath)];
  if (cli) {
    roots.push(path.dirname(cli));
    try { roots.push(path.dirname(fs.realpathSync(cli))); } catch {}
  }
  for (const key of ['APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'BUN_INSTALL']) {
    if (process.env[key]) roots.push(path.join(process.env[key], 'npm'), path.join(process.env[key], '.bun', 'install', 'global'), path.join(process.env[key], 'AppData', 'Roaming', 'npm'));
  }
  for (const root of roots) {
    try { return createRequire(path.join(root, '_jev_resolve.cjs')).resolve('betterwright'); } catch {}
    // Bun's CLI symlink resolves inside the package's dist/bin directory.
    const direct = path.resolve(root, '..', 'src', 'index.js');
    if (fs.existsSync(direct) && fs.existsSync(path.resolve(root, '..', '..', 'package.json'))) return direct;
  }
  throw new Error('BetterWright SDK not found. Install: npm install -g betterwright');
}

async function serve() {
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('browser_script requires Node.js 22 or newer. Update Node and restart the harness.');
  // Keep library diagnostics away from the protocol channel.
  console.log = (...args) => console.error(...args);
  let browser;
  let selectedSession;
  let selectedPage;
  const sessions = [];
  let nativeDialogs;
  let nativeWarning;
  const lines = readline.createInterface({ input: process.stdin });
  for await (const line of lines) {
    let request;
    try {
      request = JSON.parse(line);
      if (!browser) {
        if (request.target_id) {
          try { nativeDialogs = await watchNativeDialogs(request.ws, request.target_id, request.dismiss_overlays !== false); }
          catch (error) { nativeWarning = error.message; }
        }
        const { BetterWright } = await import(pathToFileURL(resolveSDK(request.cli)).href);
        browser = new BetterWright({ provider: { cdpUrl: request.ws }, adBlock: false, parkBackgroundPages: false, vault: false, credentialCapture: false, downloadPolicy: 'deny', defaultTimeout: 5 });
      }
      if (!selectedSession || request.page_url) {
        // BetterWright initially adopts only one existing tab per session.
        // Allocate read-only lanes for existing tabs; never navigate the wrong tab.
        const count = request.tab_count || 1;
        let selected;
        for (let index = 0; index < count; index++) {
          const session = sessions[index] || `jev-tab-${index}`;
          if (!sessions[index]) sessions.push(session);
          const probe = await browser.run('return page.url()', { session, timeout: request.timeout });
          if (!probe.ok) throw new Error(JSON.stringify(probe));
          for (const candidate of probe.pages || []) {
            if (candidate.url === request.page_url) selected = { session, page: candidate.pageId };
          }
          if (selected) break;
        }
        if (!selected) throw new Error('Workflow tab could not be adopted; inspect existing tabs before retrying');
        selectedSession = selected.session;
        selectedPage = selected.page;
      }
      const prelude = `await usePage(${JSON.stringify(selectedPage)});\n`;
      if (request.target_id && nativeDialogs && nativeDialogs.targetId !== request.target_id) {
        nativeDialogs.socket.close();
        nativeDialogs = await watchNativeDialogs(request.ws, request.target_id, request.dismiss_overlays !== false);
      }
      if (nativeDialogs) await nativeDialogs.toggle(request.dismiss_overlays !== false);
      const result = await browser.run(prelude + request.code, { session: selectedSession, timeout: request.timeout });
      if (nativeDialogs?.dismissed.length) result.nativeDialogsDismissed = nativeDialogs.dismissed.splice(0);
      const warning = nativeWarning || nativeDialogs?.warning;
      if (warning) result.warnings = [...(result.warnings || []), warning];
      process.stdout.write(JSON.stringify({ id: request.id, result }) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({ id: request?.id, error: error.message }) + '\n');
    }
  }
  // Do not call context.close(): the external GoLogin browser belongs to the user.
  // Python terminates this owned bridge and its worker on EOF/shutdown.
  process.exit(0);
}
if (require.main === module) serve().catch(error => { console.error(error.message); process.exit(1); });
module.exports = { resolveSDK };
