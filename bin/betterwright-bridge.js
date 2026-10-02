'use strict';
// Private JSON-lines transport. Snippets travel over stdin, never shell arguments.
const readline = require('node:readline');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { createRequire } = require('node:module');
const { watchNativeDialogs } = require('./native-dialogs');
const { WindowsChooser } = require('./windows-chooser');
const os = require('node:os');
const crypto = require('node:crypto');
const helpers = fs.readFileSync(path.join(__dirname, 'workflow-helpers.js'), 'utf8') + '\n' + fs.readFileSync(path.join(__dirname, '..', 'skills', 'facebook-create-pages', 'scripts', 'account-runner.js'), 'utf8');

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
  let windowsChooser;
  let checkpointFile;
  let checkpoints = {};
  let checkpointScope;
  const lines = readline.createInterface({ input: process.stdin });
  for await (const line of lines) {
    let request;
    try {
      request = JSON.parse(line);
      const scope = request.checkpoint_scope || request.ws;
      if (checkpointScope && checkpointScope !== scope) throw new Error('Checkpoint scope changed within one worker; keep the same profile scope');
      checkpointScope = scope;
      if (!checkpointFile) {
        const folder = process.env.JEV_USE_WORKFLOW_DIR || path.join(process.env.LOCALAPPDATA || process.env.XDG_STATE_HOME || os.homedir(), '.jev-use', 'workflow-checkpoints');
        fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
        checkpointFile = path.join(folder, crypto.createHash('sha256').update(scope).digest('hex') + '.json');
        if (fs.existsSync(checkpointFile)) checkpoints = JSON.parse(fs.readFileSync(checkpointFile, 'utf8'));
      }
      windowsChooser ||= new WindowsChooser(Number(new URL(request.ws).port));
      windowsChooser.toggle(request.dismiss_overlays !== false);
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
      // Reads never consume reservations. Only an explicit submission request
      // runs an actionability check, then journals the attempt before the click.
      const permits = [];
      const persist = () => {
        const temporary = checkpointFile + '.tmp';
        fs.writeFileSync(temporary, JSON.stringify(checkpoints), { mode: 0o600 });
        fs.renameSync(temporary, checkpointFile);
      };
      if (request.submission) {
        const submission = request.submission;
        if (!submission.run_id || !submission.account) throw new Error('submission requires run_id and account');
        const probeCode = prelude + `state.jevWorkflow = ${JSON.stringify(checkpoints)};\n` + helpers + `
workflow.begin(${JSON.stringify(submission.run_id)}, ${JSON.stringify(submission.account)});
await workflow.validateCreation(${JSON.stringify({selector: submission.selector, timeout: 5000})});
return JSON.parse(JSON.stringify(state.jevWorkflow));
`;
        const validated = await browser.run(probeCode, { session: selectedSession, timeout: request.timeout });
        if (!validated.ok) throw new Error(JSON.stringify(validated));
        checkpoints = validated.result;
        const key = JSON.stringify([String(submission.run_id), String(submission.account)]);
        checkpoints[key].stage = 'submitting';
        permits.push(key);
      }
      persist();
      const restore = `state.jevWorkflow = ${JSON.stringify(checkpoints)}; state.jevSubmissionPermits = ${JSON.stringify(permits)};\n`;
      const submitCode = request.submission ? `workflow.begin(${JSON.stringify(request.submission.run_id)}, ${JSON.stringify(request.submission.account)}); await workflow.submitCreation(${JSON.stringify({selector: request.submission.selector})});\n` : '';
      const wrapped = prelude + restore + helpers + `
let value;
let failure;
try { value = await (async () => { ${submitCode}${request.code}\n })(); }
catch (error) { failure = error.message; }
return { value, failure, checkpoints: JSON.parse(JSON.stringify(state.jevWorkflow || {})) };
`;
      const result = await browser.run(wrapped, { session: selectedSession, timeout: request.timeout });
      if (result.ok) {
        const execution = result.result;
        checkpoints = execution.checkpoints;
        // Atomic journal write before reporting creation success or a later failure.
        persist();
        result.result = execution.value;
        if (Object.keys(checkpoints).length) result.workflowCheckpoints = checkpoints;
        if (execution.failure) { result.ok = false; result.error = execution.failure; }
      }
      if (nativeDialogs?.dismissed.length) result.nativeDialogsDismissed = nativeDialogs.dismissed.splice(0);
      if (windowsChooser.dismissed.length) result.nativeDialogsDismissed = [...(result.nativeDialogsDismissed || []), ...windowsChooser.dismissed.splice(0)];
      const warning = nativeWarning || nativeDialogs?.warning || windowsChooser.warning;
      if (warning) result.warnings = [...(result.warnings || []), warning];
      process.stdout.write(JSON.stringify({ id: request.id, result }) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({ id: request?.id, error: error.message }) + '\n');
    }
  }
  // Do not call context.close(): the external GoLogin browser belongs to the user.
  // Python terminates this owned bridge and its worker on EOF/shutdown.
  windowsChooser?.toggle(false);
  process.exit(0);
}
if (require.main === module) serve().catch(error => { console.error(error.message); process.exit(1); });
module.exports = { resolveSDK };
