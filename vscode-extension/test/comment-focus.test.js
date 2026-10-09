// Plain node, no VS Code host: `node --test test/comment-focus.test.js`. Covers the pure half of
// comment-focus.js — where the caret goes before asking for the next thread, what the selection
// afterwards means, and when to stop asking.
const test = require('node:test');
const assert = require('node:assert');
const { probe, outcome, shouldRetry, RETRY_MS, STEP_MS, NEXT, PREVIOUS } = require('../comment-focus');

test('forward from the end of the line above the reference', () => {
  // V4__visit_vet.sql:4, thread on 4: caret at the end of line 3 (0-based 2).
  assert.deepStrictEqual(probe(4, 4, 10), { line0: 2, atEnd: true, command: NEXT });
  // VisitTest.java:445-448, GitHub's thread spans 445..448: above 445, not above 448.
  assert.deepStrictEqual(probe(445, 448, 500), { line0: 443, atEnd: true, command: NEXT });
});

test('on line 1 there is no line above: backward from the end of the comment line', () => {
  assert.deepStrictEqual(probe(1, 1, 10), { line0: 0, atEnd: true, command: PREVIOUS });
  assert.deepStrictEqual(probe(1, 3, 10), { line0: 2, atEnd: true, command: PREVIOUS });
});

test('a comment line past the end of a shrunk file is clamped', () => {
  assert.deepStrictEqual(probe(1, 40, 5), { line0: 4, atEnd: true, command: PREVIOUS });
  assert.deepStrictEqual(probe(30, 40, 20), { line0: 18, atEnd: true, command: NEXT });
});

test('no comment line: the reference line stands in for it', () => {
  assert.deepStrictEqual(probe(4, undefined, 10), probe(4, 4, 10));
});

const at = (l) => ({ startLine: l, startChar: 7, endLine: l, endChar: 7 });
const range = (a, b) => ({ startLine: a, startChar: 0, endLine: b, endChar: 30 });

test('a selection ending on the comment line is the thread, found and focused', () => {
  assert.strictEqual(outcome({ before: at(2), after: range(3, 3), commentLine: 4, command: NEXT }), 'landed');
  assert.strictEqual(outcome({ before: at(443), after: range(444, 447), commentLine: 448, command: NEXT }), 'landed');
});

test('nothing moved: no thread loaded yet', () => {
  assert.strictEqual(outcome({ before: at(2), after: at(2), commentLine: 4, command: NEXT }), 'none');
  assert.strictEqual(outcome({ before: at(2), after: null, commentLine: 4, command: NEXT }), 'none');
});

test('another thread short of the comment is stepped over, one past it is a miss', () => {
  assert.strictEqual(outcome({ before: at(2), after: range(2, 2), commentLine: 4, command: NEXT }), 'again');
  assert.strictEqual(outcome({ before: at(2), after: range(9, 9), commentLine: 4, command: NEXT }), 'missed');
  assert.strictEqual(outcome({ before: at(3), after: range(1, 1), commentLine: 4, command: PREVIOUS }), 'missed');
});

test('retry only while nothing was found and the budget lasts', () => {
  assert.strictEqual(shouldRetry('none', 0), true);
  assert.strictEqual(shouldRetry('none', RETRY_MS - STEP_MS), true);
  assert.strictEqual(shouldRetry('none', RETRY_MS - STEP_MS + 1), false);
  assert.strictEqual(shouldRetry('landed', 0), false);
  assert.strictEqual(shouldRetry('missed', 0), false);
});
