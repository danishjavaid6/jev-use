'use strict';

/**
 * Terminal output. Deliberately tiny and dependency-free, and deliberately
 * stderr-first for anything diagnostic: `jev-use-mcp` speaks JSON-RPC over
 * stdout, so a stray `console.log` there corrupts the MCP stream.
 */

const IS_TTY = process.stderr.isTTY === true;
const PLAIN = process.env.NO_COLOR ? true : !IS_TTY;

function paint(code, text) {
  return PLAIN ? text : `\u001b[${code}m${text}\u001b[0m`;
}

const dim = (t) => paint('2', t);
const bold = (t) => paint('1', t);
const green = (t) => paint('32', t);
const yellow = (t) => paint('33', t);
const red = (t) => paint('31', t);

/** Progress goes to stderr so it can never collide with an MCP stdio stream. */
function say(message) {
  process.stderr.write(`${message}\n`);
}

function step(message) {
  say(`${dim('>')} ${message}`);
}

function ok(message) {
  say(`${green('OK')} ${message}`);
}

function warn(message) {
  say(`${yellow('!')} ${message}`);
}

function fail(message) {
  say(`${red('x')} ${message}`);
}

function heading(message) {
  say(`\n${bold(message)}`);
}

module.exports = { say, step, ok, warn, fail, heading, dim, bold, green, yellow, red };
