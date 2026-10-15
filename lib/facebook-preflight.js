'use strict';

// Read-only dependency checks. Never launch a browser or contact Facebook.
const path = require('node:path');
const { run } = require('./run');

function facebookPreflight(python, { execute = run, env = process.env } = {}) {
  const missing = detail => ({ ok: false, detail });
  if (!python) return {
    betterwright: missing('Python runtime missing — run jev-use install'),
    jev: missing('Python runtime missing — run jev-use install'),
  };
  const dependencies = execute(python, ['-c', [
    'import importlib, json',
    'result = {}',
    'for name in ("typesafe_sdk", "jev_use.betterwright"):',
    '    try:',
    '        module = importlib.import_module(name)',
    '        result[name] = True',
    '        if name == "jev_use.betterwright": result["cli"] = module.executable()',
    '    except Exception: result[name] = False',
    'print(json.dumps(result))',
  ].join('\n')], { timeout: 10000, env });
  let observed;
  try { observed = dependencies.ok && JSON.parse(dependencies.stdout); } catch {}
  const jev = observed?.typesafe_sdk
    ? { ok: true, detail: 'SDK installed; unfamiliar UI inspection also needs a configured API key' }
    : missing('typesafe-sdk unavailable; install jev-use with its live extra before unfamiliar UI inspection');
  if (!observed?.cli) return {
    betterwright: missing('Betterwright unavailable — pnpm add -g betterwright; requires Node.js 22+'), jev,
  };
  const bridge = path.resolve(__dirname, '..', 'bin', 'betterwright-bridge.js');
  const sdk = execute(process.execPath, ['-e',
    'try { require(process.argv[1]).resolveSDK(process.argv[2]); } catch { process.exitCode = 1; }',
    bridge, observed.cli], { timeout: 10000, env });
  return {
    betterwright: sdk.ok
      ? { ok: true, detail: 'CLI and SDK resolve; no browser launched' }
      : missing('CLI exists but SDK cannot resolve — pnpm add -g betterwright, then reload MCP'),
    jev,
  };
}

module.exports = { facebookPreflight };
