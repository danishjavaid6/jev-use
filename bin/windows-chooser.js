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
    if (!enabled) { if (this.child) this.child.kill(); this.child = null; return; }
    if (this.child || this.failed) return;
    this.child = this.launch('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path.join(__dirname, 'dismiss-account-chooser.ps1'), '-DebugPort', String(this.port)], { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] });
    const child = this.child;
    readline.createInterface({ input: child.stdout }).on('line', line => {
      try {
        const result = JSON.parse(line);
        if (result.dismissed) this.dismissed.push(result.dismissed);
        if (result.warning) this.warning = result.warning;
      } catch {}
    });
    child.on('error', () => { this.failed = true; this.warning = 'Windows account chooser helper could not start'; });
    child.on('exit', code => {
      if (this.child === child) {
        this.child = null;
        if (code) { this.failed = true; this.warning ||= 'Windows account chooser helper stopped'; }
      }
    });
  }
}
module.exports = { WindowsChooser };
