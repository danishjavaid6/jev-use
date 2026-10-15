'use strict';
const { spawn } = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');
class WindowsChooser {
  constructor(port, launch = spawn, platform = process.platform) {
    this.port = port;
    this.launch = launch;
    this.platform = platform;
    this.dismissed = [];
  }
  toggle(enabled) {
    if (this.platform !== 'win32') return;
    if (!enabled) { if (this.child) this.child.kill(); this.child = null; this.ready = false; return; }
    if (this.child || this.failed) return;
    this.child = this.launch('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path.join(__dirname, 'dismiss-account-chooser.ps1'), '-DebugPort', String(this.port)], { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] });
    const child = this.child;
    readline.createInterface({ input: child.stdout }).on('line', line => {
      try {
        const result = JSON.parse(line);
        if (typeof result.exceptionType === 'string' && /^[A-Za-z][A-Za-z0-9_.]{0,180}$/.test(result.exceptionType)) this.exceptionType = result.exceptionType;
        if (result.inspected) { this.inspection = result; this.resolveInspection?.(result); }
        if (result.ready) { this.ready = true; if (this.warning === 'Windows popup inspection did not become ready; inspect browser chrome before login') this.warning = null; this.resolveReady?.(); }
        if (result.dismissed) this.dismissed.push(result.dismissed);
        if (result.warning) { this.warning = result.warning; this.resolveReady?.(); }
      } catch {}
    });
    child.on('error', () => { this.failed = true; this.warning = 'Windows account chooser helper could not start'; this.resolveReady?.(); });
    child.on('exit', code => {
      if (this.child === child) {
        this.child = null;
        this.ready = false;
        if (code) { this.failed = true; this.warning ||= 'Windows account chooser helper stopped'; }
        this.resolveReady?.();
      }
    });
  }
  async waitUntilReady(timeout = 15000) {
    if (this.platform !== 'win32' || this.ready || this.failed) return;
    await new Promise(resolve => {
      const timer = setTimeout(() => {
        this.warning ||= 'Windows popup inspection did not become ready; inspect browser chrome before login';
        resolve();
      }, timeout);
      this.resolveReady = () => { clearTimeout(timer); resolve(); };
    });
    this.resolveReady = null;
  }
  async ensureClear(timeout = 5000) {
    if (this.platform !== 'win32') return { inspected: false, blocked: false, supported: false };
    await this.waitUntilReady();
    if (!this.ready || this.failed) {
      const cause = this.exceptionType ? ` (UIA: ${this.exceptionType})` : '';
      throw new Error((this.warning || 'Browser popup inspection is unavailable; inspect browser chrome before acting') + cause);
    }
    // Require a NEW pass after this tool call, rather than trusting an old clear state.
    const inspection = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Browser popup inspection timed out; action stopped')), timeout);
      this.resolveInspection = value => { clearTimeout(timer); resolve(value); };
    }).finally(() => { this.resolveInspection = null; });
    if (inspection.blocked) throw new Error(`Browser popup blocks the action (${inspection.kinds.join(', ')}); inspect or dismiss it before continuing`);
    return inspection;
  }
}
module.exports = { WindowsChooser };
