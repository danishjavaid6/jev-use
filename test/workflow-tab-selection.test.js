'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { chooseWorkflowTab } = require('../bin/betterwright-bridge');
const { adoptWorkflowTab } = require('../bin/workflow-tabs');
test('saved chooser uniquely resolves a generic Facebook login tab', () => {
  const generic = { session: 'first', page: 'generic', savedChooser: false };
  const chooser = { session: 'second', page: 'chooser', savedChooser: true };
  assert.equal(chooseWorkflowTab([generic, chooser]), chooser);
  assert.equal(chooseWorkflowTab([generic]), generic);
});

test('duplicate URLs correlate active SDK pages with exact native-dialog targets and remove markers', async () => {
  const watchers = new Map();
  const connect = async (ws, id) => {
    const watcher = { targetId: id, sessionId: id, socket: { close() { watcher.closed = true; } },
      calls: [], async send(method, params) { this.calls.push(params.expression); return {}; },
      async toggle(value) { this.enabled = value; } };
    watchers.set(id, watcher);
    return watcher;
  };
  const browser = { async run(code, options) {
    const second = options.session.endsWith('1');
    return { ok: true, result: { targetId: second ? 'chooser' : 'generic', savedChooser: second }, pages:[{active:true,pageId:second?'sdk2':'sdk1'}] };
  } };
  const selected = await adoptWorkflowTab(browser, {ws: 'ws://localhost:1', targets: [{id:'generic',url:'same'}, {id:'chooser',url:'same'}], tab_count:2}, [], connect);
  assert.equal(selected.page, 'sdk2');
  assert.equal(selected.targetId, selected.watcher.targetId);
  assert.equal(watchers.get('generic').closed, true);
  assert.equal(selected.watcher.closed, undefined);
  assert.equal(selected.watcher.enabled, true);
  for (const watcher of watchers.values()) assert.match(watcher.calls.at(-1), /^delete globalThis/);
});

test('ambiguous tab adoption cleans every watcher before refusing actions', async () => {
  const closed = [];
  const connect = async (ws, id) => ({sessionId:id, async send() {return {};}, socket:{close(){closed.push(id);}}});
  const browser = { async run(code, {session}) {return {ok:true,result:{targetId:session.endsWith('0')?'one':'two',savedChooser:true},pages:[{active:true,pageId:session}]};} };
  await assert.rejects(adoptWorkflowTab(browser,{targets:[{id:'one'},{id:'two'}],tab_count:2},[],connect),/Multiple saved-account chooser/);
  assert.deepEqual(closed.sort(), ['one','two']);
});
test('duplicate choosers or missing matches never choose arbitrarily', () => {
  assert.throws(() => chooseWorkflowTab([{ savedChooser: true }, { savedChooser: true }]), /Multiple saved-account chooser/);
  assert.throws(() => chooseWorkflowTab([{ savedChooser: false }, { savedChooser: false }]), /without a unique/);
  assert.throws(() => chooseWorkflowTab([]), /could not be adopted/);
});
