// Plain node, no VS Code host: `node --test test/range-focus.test.js`. Covers the pure half of range-focus.js —
// which lines get faded, and which selection events count as the reader taking over.
const test = require('node:test');
const assert = require('node:assert');
const { span, fadeSpans, readerMoved, GUARD_MS, MAX_SPAN } = require('../range-focus');

test('a 1-based range becomes 0-based inclusive', () => {
  assert.deepStrictEqual(span(49, 51, 300), [48, 50]);
  assert.deepStrictEqual(span('49', '51', 300), [48, 50]);
});

test('no range: no end, a single line, reversed, not a number', () => {
  assert.strictEqual(span(49, undefined, 300), null);
  assert.strictEqual(span(49, 49, 300), null);
  assert.strictEqual(span(51, 49, 300), null);
  assert.strictEqual(span(49, 'x', 300), null);
  assert.strictEqual(span(0, 3, 300), null);
  assert.strictEqual(span(1.5, 3, 300), null);
});

test('a span past the cap is not focused', () => {
  assert.deepStrictEqual(span(1, MAX_SPAN, 5000), [0, MAX_SPAN - 1]);
  assert.strictEqual(span(1, MAX_SPAN + 1, 5000), null);
});

test('clamped to a file that has shrunk', () => {
  assert.deepStrictEqual(span(8, 20, 10), [7, 9]);
  assert.strictEqual(span(12, 20, 10), null);
});

test('fade covers everything above and below, and nothing of the range', () => {
  assert.deepStrictEqual(fadeSpans(48, 50, 300), [[0, 47], [51, 299]]);
  assert.deepStrictEqual(fadeSpans(0, 5, 10), [[6, 9]]);
  assert.deepStrictEqual(fadeSpans(3, 9, 10), [[0, 2]]);
  assert.deepStrictEqual(fadeSpans(0, 9, 10), []);
});

test('a click or a key is always the reader', () => {
  assert.strictEqual(readerMoved({ kind: 2, sinceArmMs: 0, sameAsOurs: true }), true);
  assert.strictEqual(readerMoved({ kind: 1, sinceArmMs: 0, sameAsOurs: false }), true);
});

test('our own selection, or anything inside the guard, is not', () => {
  assert.strictEqual(readerMoved({ kind: undefined, sinceArmMs: 5000, sameAsOurs: true }), false);
  assert.strictEqual(readerMoved({ kind: 3, sinceArmMs: GUARD_MS - 1, sameAsOurs: false }), false);
});

test('a command moving the selection after the guard is the reader', () => {
  assert.strictEqual(readerMoved({ kind: 3, sinceArmMs: GUARD_MS, sameAsOurs: false }), true);
});

test('a hold keeps the bridge\'s own moves from clearing the fade, never a click or a key', () => {
  assert.strictEqual(readerMoved({ kind: 3, sinceArmMs: 5000, sameAsOurs: false, held: true }), false);
  assert.strictEqual(readerMoved({ kind: undefined, sinceArmMs: 5000, sameAsOurs: false, held: true }), false);
  assert.strictEqual(readerMoved({ kind: 2, sinceArmMs: 5000, sameAsOurs: false, held: true }), true);
  assert.strictEqual(readerMoved({ kind: 1, sinceArmMs: 5000, sameAsOurs: false, held: true }), true);
});
