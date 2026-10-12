'use strict';
// File-based batch todo for the multi-profile Facebook Pages loop.
//
// Why this exists: the batch walks ~29 GoLogin profiles line by line, each
// with several saved accounts, each account needing prepare -> create ->
// logout across separate browser_script calls. Per-account workflow
// checkpoints (workflow.begin) remember one account, but nothing remembers
// which profile/account is next after a restart. This file does.
//
// State lives in runs/ (gitignored, local-only). It never stores passwords:
// accounts carry {account, page_name} plus status only.
//
// Usage:
//   node batch-todo.js init --file runs/facebook-batch-todo.json --batch B1 \
//     --profiles config/gologin-profiles.json
//   node batch-todo.js add-accounts --file runs/facebook-batch-todo.json \
//     --profile-id <id> --accounts '[{"account":"Name","page_name":"Page"}]'
//   node batch-todo.js next --file runs/facebook-batch-todo.json
//   node batch-todo.js mark --file runs/facebook-batch-todo.json \
//     --profile-id <id> --account Name --status logged_out
//   node batch-todo.js status --file runs/facebook-batch-todo.json

const fs = require('node:fs');

const TERMINAL = new Set(['logged_out', 'skipped']);
const ACCOUNT_STATUSES = new Set([
  'pending', 'prepared', 'submission_reserved', 'submitting',
  'created', 'logged_out', 'skipped', 'failed',
]);
const PROFILE_STATUSES = new Set(['pending', 'open', 'done', 'failed']);

const SAFE_DETAILS = new Set(['missing_password', 'mfa_required']);

function assertNoSecrets(value, where) {
  if (typeof value === 'string' && SAFE_DETAILS.has(value.trim())) return;
  const text = JSON.stringify(value || {});
  if (/(password|passwd|secret)/i.test(text)) {
    throw new Error(`${where} must never contain passwords or secrets`);
  }
}

function initTodo({ batch_id, profiles }) {
  if (typeof batch_id !== 'string' || !batch_id.trim()) throw new Error('batch_id must be a non-empty string');
  if (!Array.isArray(profiles) || !profiles.length) throw new Error('profiles must be a non-empty array');
  const seen = new Set();
  const list = profiles.map((entry, index) => {
    const profile_id = String(entry.id || entry.profile_id || '').trim();
    const profile_name = String(entry.name || entry.profile || `Profile ${index + 1}`).trim();
    if (!profile_id) throw new Error(`profiles[${index}] is missing an id`);
    if (seen.has(profile_id)) throw new Error(`duplicate profile id ${profile_id}`);
    seen.add(profile_id);
    return { profile_id, profile_name, status: 'pending', accounts: [] };
  });
  return { batch_id: batch_id.trim(), created: new Date().toISOString(), profiles: list };
}

function addAccounts(todo, profile_id, accounts) {
  const profile = (todo.profiles || []).find(p => p.profile_id === profile_id);
  if (!profile) throw new Error(`unknown profile id ${profile_id}`);
  if (!Array.isArray(accounts) || !accounts.length) throw new Error('accounts must be a non-empty array');
  assertNoSecrets(accounts, 'accounts');
  const known = new Set(profile.accounts.map(a => a.account));
  for (const entry of accounts) {
    const account = String(entry.account || '').trim();
    const page_name = String(entry.page_name || '').trim();
    if (!account || !page_name) throw new Error('each account needs {account, page_name}');
    if (known.has(account)) throw new Error(`duplicate account ${account} in profile ${profile_id}`);
    known.add(account);
    profile.accounts.push({ account, page_name, status: 'pending', detail: null, updated: null });
  }
  if (profile.status === 'pending') profile.status = 'open';
  return todo;
}

function nextWork(todo) {
  for (const profile of todo.profiles || []) {
    if (profile.status === 'done' || profile.status === 'failed') continue;
    for (const account of profile.accounts) {
      if (!TERMINAL.has(account.status) && account.status !== 'failed') {
        return { profile_id: profile.profile_id, profile_name: profile.profile_name, ...account };
      }
    }
    // Profile with no accounts yet: report it so the agent enumerates cards.
    if (!profile.accounts.length && profile.status !== 'done') {
      return { profile_id: profile.profile_id, profile_name: profile.profile_name, account: null, status: 'needs_enumeration' };
    }
  }
  return null;
}

function mark(todo, profile_id, account, status, detail = null) {
  if (!ACCOUNT_STATUSES.has(status)) throw new Error(`unknown account status ${status}`);
  assertNoSecrets(detail, 'detail');
  const profile = (todo.profiles || []).find(p => p.profile_id === profile_id);
  if (!profile) throw new Error(`unknown profile id ${profile_id}`);
  const item = profile.accounts.find(a => a.account === account);
  if (!item) throw new Error(`unknown account ${account} in profile ${profile_id}`);
  item.status = status;
  item.detail = detail;
  item.updated = new Date().toISOString();
  if (profile.status === 'pending') profile.status = 'open';
  if (profile.accounts.length && profile.accounts.every(a => TERMINAL.has(a.status))) {
    profile.status = 'done';
  }
  return todo;
}

function markProfile(todo, profile_id, status) {
  if (!PROFILE_STATUSES.has(status)) throw new Error(`unknown profile status ${status}`);
  const profile = (todo.profiles || []).find(p => p.profile_id === profile_id);
  if (!profile) throw new Error(`unknown profile id ${profile_id}`);
  profile.status = status;
  return todo;
}

function summary(todo) {
  const profiles = todo.profiles || [];
  const accounts = profiles.flatMap(p => p.accounts);
  const byStatus = {};
  for (const a of accounts) byStatus[a.status] = (byStatus[a.status] || 0) + 1;
  return {
    batch_id: todo.batch_id,
    profiles_total: profiles.length,
    profiles_done: profiles.filter(p => p.status === 'done').length,
    accounts_total: accounts.length,
    accounts_by_status: byStatus,
    complete: profiles.length > 0 && profiles.every(p => p.status === 'done'),
    next: nextWork(todo),
  };
}

function load(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function save(file, todo) {
  assertNoSecrets(todo, 'todo state');
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(todo, null, 2), { mode: 0o600 });
  fs.renameSync(temporary, file);
}

module.exports = { initTodo, addAccounts, nextWork, mark, markProfile, summary, load, save, TERMINAL: [...TERMINAL] };

if (require.main === module) {
  const args = process.argv.slice(2);
  const command = args[0];
  const flag = name => {
    const index = args.indexOf(`--${name}`);
    return index === -1 ? null : args[index + 1];
  };
  try {
    if (command === 'init') {
      const file = flag('file');
      const batch = flag('batch');
      const profilesFile = flag('profiles');
      if (!file || !batch || !profilesFile) throw new Error('init needs --file, --batch and --profiles');
      const raw = JSON.parse(fs.readFileSync(profilesFile, 'utf8'));
      const todo = initTodo({ batch_id: batch, profiles: raw.profiles || raw });
      save(file, todo);
      console.log(JSON.stringify(summary(todo), null, 2));
    } else if (command === 'add-accounts') {
      const file = flag('file');
      const profileId = flag('profile-id');
      const accounts = flag('accounts');
      if (!file || !profileId || !accounts) throw new Error('add-accounts needs --file, --profile-id and --accounts JSON');
      const todo = load(file);
      addAccounts(todo, profileId, JSON.parse(accounts));
      save(file, todo);
      console.log(JSON.stringify(summary(todo), null, 2));
    } else if (command === 'next') {
      const todo = load(flag('file'));
      console.log(JSON.stringify(nextWork(todo)));
    } else if (command === 'mark') {
      const file = flag('file');
      const todo = load(file);
      mark(todo, flag('profile-id'), flag('account'), flag('status'), flag('detail'));
      save(file, todo);
      console.log(JSON.stringify(summary(todo), null, 2));
    } else if (command === 'status') {
      console.log(JSON.stringify(summary(load(flag('file'))), null, 2));
    } else {
      throw new Error('usage: init|add-accounts|next|mark|status');
    }
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
