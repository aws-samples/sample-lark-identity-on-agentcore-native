// The guards are the point of this server, so they get tested. Unlike the lark-cli
// MCP server (a thin passthrough), this one decides whether a write to Lark happens
// at all — and Lark's approve/reject accept no user token, so nothing downstream
// will second-guess it. Node's built-in runner, no new dependencies:
//   node --test mcp-servers/approval/
//
// server.js is CommonJS and starts listening on import, so the guard functions are
// extracted and evaluated in isolation rather than imported.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

function loadGuards(env = {}) {
  const src = readFileSync(new URL('./server.js', import.meta.url), 'utf8');
  // Anchored on declarations, not on comment text — an earlier version keyed off a
  // comment and broke the moment that comment was reworded.
  const start = src.indexOf('const AGENT_DECIDE_MAX_AMOUNT');
  const end = src.indexOf('const TOOLS = [');
  assert.ok(start > 0 && end > start, 'guard block not found — did server.js move?');
  const fn = new Function('process', `${src.slice(start, end)}
    return { checkAutoDecisionAllowed, consentNote, AGENT_DECIDE_MAX_AMOUNT, AGENT_DECIDE_APPROVAL_CODES };`);
  return fn({ env });
}

test('fails closed: no configured approval codes means nothing is automated', () => {
  const g = loadGuards({});                       // AGENT_DECIDE_APPROVAL_CODES unset
  const refusal = g.checkAutoDecisionAllowed({ approval_code: 'ANY', amount: 1 });
  assert.match(refusal, /not in AGENT_DECIDE_APPROVAL_CODES/);
});

test('only allow-listed approval definitions are automated', () => {
  const g = loadGuards({ AGENT_DECIDE_APPROVAL_CODES: 'EXPENSE_V1,LEAVE_V1' });
  assert.equal(g.checkAutoDecisionAllowed({ approval_code: 'EXPENSE_V1', amount: 10 }), null);
  assert.match(g.checkAutoDecisionAllowed({ approval_code: 'PAYROLL_V1', amount: 10 }),
               /not in AGENT_DECIDE_APPROVAL_CODES/);
});

test('amount above the limit is refused, at the limit is allowed', () => {
  const g = loadGuards({ AGENT_DECIDE_APPROVAL_CODES: 'EXPENSE_V1', AGENT_DECIDE_MAX_AMOUNT: '1000' });
  assert.equal(g.checkAutoDecisionAllowed({ approval_code: 'EXPENSE_V1', amount: 1000 }), null);
  assert.match(g.checkAutoDecisionAllowed({ approval_code: 'EXPENSE_V1', amount: 1000.01 }),
               /exceeds the automatic limit/);
});

test('a kill switch exists: 0 disables automation even for allow-listed codes', () => {
  const g = loadGuards({ AGENT_DECIDE_APPROVAL_CODES: 'EXPENSE_V1', AGENT_DECIDE_MAX_AMOUNT: '0' });
  assert.match(g.checkAutoDecisionAllowed({ approval_code: 'EXPENSE_V1', amount: 1 }),
               /disabled/);
});

test('a missing or unparseable amount does not bypass the limit check', () => {
  // Forms without an amount still have to be allow-listed; what must not happen is
  // a non-numeric amount silently reading as "under the limit" on a form that has
  // one. Both cases fall through to the allow-list, which is the fail-closed gate.
  const g = loadGuards({ AGENT_DECIDE_APPROVAL_CODES: 'LEAVE_V1', AGENT_DECIDE_MAX_AMOUNT: '1000' });
  assert.equal(g.checkAutoDecisionAllowed({ approval_code: 'LEAVE_V1' }), null);
  assert.equal(g.checkAutoDecisionAllowed({ approval_code: 'LEAVE_V1', amount: 'lots' }), null);
  assert.match(g.checkAutoDecisionAllowed({ approval_code: 'OTHER_V1', amount: 'lots' }),
               /not in AGENT_DECIDE_APPROVAL_CODES/);
});

test('the consent note distinguishes an on-record grant from app authority alone', () => {
  const g = loadGuards({});
  assert.match(g.consentNote(true), /on-record 3LO grant/);
  assert.match(g.consentNote(false), /NO on-record grant/);
  // The wording must stay explicit: the record is what tells a reader afterwards
  // whether anyone had agreed to be acted for.
  assert.notEqual(g.consentNote(true), g.consentNote(false));
});
