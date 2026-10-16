'use strict';

// Shared across checkouts. No credentials, browser URLs or request arguments.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const paths = require('./paths');

const root = () => path.join(paths.stateDir(), 'mcp-lifecycle');
const maintenance = () => path.join(root(), 'maintenance.json');

function alive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code !== 'ESRCH'; }
}

function held(file) {
  try {
    const record = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (alive(record.pid)) return true;
    fs.unlinkSync(file);
    return false;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    // An incomplete record may belong to a process still acquiring its lock.
    throw new Error('Cannot verify the MCP lifecycle lock; leave it intact and inspect it before maintenance.');
  }
}

function record(file) {
  fs.mkdirSync(root(), { recursive: true });
  const value = JSON.stringify({ pid: process.pid, id: crypto.randomUUID() });
  fs.writeFileSync(file, value, { flag: 'wx', mode: 0o600 });
  return () => {
    try { if (fs.readFileSync(file, 'utf8') === value) fs.unlinkSync(file); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  };
}

function beginSession() {
  if (held(maintenance())) throw new Error('Installation is in progress. Retry this MCP connection after it finishes.');
  const release = record(path.join(root(), `session-${process.pid}-${crypto.randomUUID()}.json`));
  try {
    // Installer and connection startup can race. Only one may proceed.
    if (held(maintenance())) throw new Error('Installation is in progress. Retry this MCP connection after it finishes.');
    return release;
  } catch (error) { release(); throw error; }
}

function beginMaintenance() {
  if (held(maintenance())) throw new Error('Another installation is in progress; retry after it finishes.');
  let release;
  try { release = record(maintenance()); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error('Another installation is in progress; retry after it finishes.');
    throw error;
  }
  try {
    const connected = fs.readdirSync(root()).filter(name => name.startsWith('session-') && name.endsWith('.json'))
      .some(name => held(path.join(root(), name)));
    if (connected) throw new Error('Installation deferred: an MCP session is connected. Pause the job, save/close its profile, and disconnect jev-use before installing. No configuration or runtime was changed.');
    return release;
  } catch (error) { release(); throw error; }
}

module.exports = { beginSession, beginMaintenance };
