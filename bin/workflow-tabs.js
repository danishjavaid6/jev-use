'use strict';
const crypto = require('node:crypto');
const { watchNativeDialogs } = require('./native-dialogs');

function chooseWorkflowTab(matches) {
  const choosers = matches.filter(match => match.savedChooser);
  if (choosers.length === 1) return choosers[0];
  if (choosers.length > 1) throw new Error('Multiple saved-account chooser tabs match; inspect tabs and supply page_target_id');
  if (matches.length === 1) return matches[0];
  throw new Error(matches.length ? 'Multiple workflow tabs match without a unique saved-account chooser; supply page_target_id' : 'Workflow tab could not be adopted; inspect existing tabs before retrying');
}

// A temporary non-secret marker correlates SDK page IDs with real CDP targets.
// URL equality alone cannot distinguish duplicate Facebook tabs.
async function adoptWorkflowTab(browser, request, sessions, connect = watchNativeDialogs) {
  const key = '__jev_tab_' + crypto.randomUUID().replaceAll('-', '');
  const targets = request.targets || [{ id: request.target_id, url: request.page_url }];
  const watchers = new Map();
  let selected;
  let adopted = false;
  try {
    for (const target of targets) {
      const watcher = await connect(request.ws, target.id, targets.length === 1 && request.dismiss_overlays !== false);
      watchers.set(target.id, watcher);
      const result = await watcher.send('Runtime.evaluate', {
        expression: `Object.defineProperty(globalThis,${JSON.stringify(key)},{value:${JSON.stringify(target.id)},configurable:true});true`,
        returnByValue: true,
      }, watcher.sessionId);
      if (result.exceptionDetails) throw new Error('Workflow tab marker unavailable; inspect browser dialogs');
    }
    const matches = [];
    const seen = new Set();
    for (let index = 0; index < (request.tab_count || 1); index++) {
      const session = sessions[index] || `jev-tab-${index}`;
      if (!sessions[index]) sessions.push(session);
      const probe = await browser.run(`return {targetId:await page.evaluate(()=>globalThis[${JSON.stringify(key)}]), savedChooser:await page.getByText("Use another profile",{exact:true}).isVisible().catch(()=>false)}`, { session, timeout: request.timeout });
      if (!probe.ok) throw new Error('Workflow tab inspection failed; inspect existing tabs before retrying');
      const info = probe.result;
      const pageId = probe.pages?.find(page => page.active)?.pageId;
      if (pageId && watchers.has(info?.targetId) && !seen.has(info.targetId)) {
        matches.push({ session, page: pageId, targetId: info.targetId, savedChooser: Boolean(info.savedChooser) });
        seen.add(info.targetId);
      }
      if (seen.size === targets.length) break;
    }
    selected = chooseWorkflowTab(matches);
    const watcher = watchers.get(selected.targetId);
    await watcher.toggle(request.dismiss_overlays !== false);
    adopted = true;
    return { ...selected, watcher };
  } finally {
    for (const [id, watcher] of watchers) {
      try { await watcher.send('Runtime.evaluate', { expression: `delete globalThis[${JSON.stringify(key)}]` }, watcher.sessionId); } catch {}
      if (!adopted || id !== selected?.targetId) watcher.socket.close();
    }
  }
}
module.exports = { chooseWorkflowTab, adoptWorkflowTab };
