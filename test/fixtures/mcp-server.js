'use strict';
const readline = require('node:readline');
if (process.env.JEV_TEST_MODE === 'exit') {
  process.stderr.write(`startup failed ${process.env.JEV_TEST_SECRET || ''}\n`);
  process.exit(2);
}
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  if (process.env.JEV_TEST_MODE === 'timeout') return;
  if (process.env.JEV_TEST_MODE === 'garbage') return process.stdout.write('not JSON\n');
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  const result = request.method === 'initialize'
    ? { protocolVersion: request.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } }
    : { tools: [{ name: 'browser_read', inputSchema: { type: 'object', properties: {} } }] };
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }) + '\n');
});
