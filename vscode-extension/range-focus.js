// Land on a *range* of a file, not on a line of it: the range highlighted, everything else
// faded, until the reader takes over the editor.
//
// A Human Review reference is `OwnerListTest.java:49-51` — the reviewer means those three
// lines, and a caret on 49 says nothing about where the quote stops. So the bridge selects
// the range, paints it with a soft background that survives the selection moving, and
// fades every other line to 35% so the eye finds the three lines in a five-hundred-line
// class without hunting.
//
// The fade is a statement about the moment of arriving, not a mode the file is left in.
// It goes on the reader's first own move in that editor — a click, a keystroke, an edit —
// when another text editor becomes the active one, and when the next reference is opened
// (one focus at a time, replaced, never stacked). Focus moving to the review page itself
// (a webview, so no text editor is active) keeps it: the reader is reading the page
// against the code, which is exactly when the highlight is wanted.
//
// The pure part (which lines to fade, whether a selection event is the reader's) is
// exported on its own so plain node can test it without a VS Code host.

/** How long after the bridge sets its own selection an event from it is still ours.
 *  `showTextDocument` and the explicit `editor.selection =` each fire a change, sometimes
 *  a beat late; nobody clicks inside a file a quarter of a second after it appeared. */
const GUARD_MS = 400;

/** The longest span that gets the treatment. A "range" of thousands of lines is a whole
 *  file, where fading the rest would fade nothing and highlighting it all says nothing. */
const MAX_SPAN = 2000;

/** 1-based `line`..`endLine` → 0-based inclusive `[start, end]`, clamped to the document,
 *  or null when there is no range to focus (no end, a single line, a reversed or absurd
 *  span). */
function span(line, endLine, lineCount) {
  const a = Number(line), b = Number(endLine);
  if (!Number.isInteger(a) || !Number.isInteger(b) || a < 1 || b <= a) return null;
  if (b - a + 1 > MAX_SPAN) return null;
  const last = Math.max(0, (Number(lineCount) || 0) - 1);
  const start = Math.min(a - 1, last), end = Math.min(b - 1, last);
  return end > start ? [start, end] : null;
}

/** The 0-based inclusive line spans outside `[start, end]` in a document of `lineCount`
 *  lines: at most one above and one below. */
function fadeSpans(start, end, lineCount) {
  const out = [];
  if (start > 0) out.push([0, start - 1]);
  if (end < lineCount - 1) out.push([end + 1, lineCount - 1]);
  return out;
}

// vscode.TextEditorSelectionChangeKind: Keyboard = 1, Mouse = 2, Command = 3.
const KEYBOARD = 1, MOUSE = 2;

/** Is this selection change the reader's own move? A click or a key always is. Anything
 *  else (a command, or no kind at all, which is what a programmatic set reports) is ours
 *  while it lands on the selection we set, falls inside the guard or inside a `hold()` —
 *  the bridge walking the caret to a PR comment thread (comment-focus.js) — and the
 *  reader's after that — Go to Line, a Find jump. */
function readerMoved({ kind, sinceArmMs, sameAsOurs, held }) {
  if (kind === MOUSE || kind === KEYBOARD) return true;
  if (sameAsOurs || held) return false;
  return sinceArmMs >= GUARD_MS;
}

let current = null;
let highlightDeco = null;
let fadeDeco = null;

function decorations(vscode) {
  if (!highlightDeco) {
    highlightDeco = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: new vscode.ThemeColor('editor.rangeHighlightBackground'),
      overviewRulerColor: new vscode.ThemeColor('editor.findMatchHighlightBackground'),
      overviewRulerLane: vscode.OverviewRulerLane.Full,
    });
    // Inline, not whole-line: a whole-line decoration is drawn on an overlay layer behind
    // the text, so its opacity fades nothing. On the text spans it does — the same
    // mechanism VS Code uses to grey out unused code.
    fadeDeco = vscode.window.createTextEditorDecorationType({ opacity: '0.35' });
  }
  return { highlightDeco, fadeDeco };
}

/** Drop the current focus, if any: its decorations off its editor, its listeners gone. */
function clear() {
  if (!current) return;
  const { editor, subs } = current;
  current = null;
  for (const s of subs) { try { s.dispose(); } catch (_) { /* already gone */ } }
  try {
    editor.setDecorations(highlightDeco, []);
    editor.setDecorations(fadeDeco, []);
  } catch (_) { /* the editor was closed */ }
}

/** Select, reveal, highlight and fade `line..endLine` (1-based) in `editor`. With no range
 *  (only a line, or a span `span()` rejects) it only clears an earlier focus, so a
 *  single-line open behaves exactly as before. Returns whether a range was focused. */
function focus(editor, line, endLine) {
  clear();
  const vscode = require('vscode');
  const doc = editor.document;
  const s = span(line, endLine, doc.lineCount);
  if (!s) return false;
  const [start, end] = s;
  const { highlightDeco: hl, fadeDeco: fade } = decorations(vscode);
  const sel = new vscode.Selection(start, 0, end, doc.lineAt(end).text.length);
  const armedAt = Date.now();
  editor.selection = sel;
  editor.revealRange(sel, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  editor.setDecorations(hl, [new vscode.Range(start, 0, end, 0)]);
  editor.setDecorations(fade, fadeSpans(start, end, doc.lineCount).map(([a, b]) =>
    new vscode.Range(a, 0, b, doc.lineAt(b).text.length)));

  const subs = [
    vscode.window.onDidChangeTextEditorSelection((e) => {
      if (e.textEditor !== editor) return;
      const sameAsOurs = e.selections.length === 1 && e.selections[0].isEqual(sel);
      const held = !!current && Date.now() < current.heldUntil;
      if (readerMoved({ kind: e.kind, sinceArmMs: Date.now() - armedAt, sameAsOurs, held })) clear();
    }),
    vscode.workspace.onDidChangeTextDocument((e) => {
      if (e.document === doc && e.contentChanges.length) clear();
    }),
    vscode.window.onDidChangeActiveTextEditor((ed) => {
      if (ed && ed !== editor) clear();
    }),
    vscode.window.onDidChangeVisibleTextEditors((eds) => {
      if (!eds.includes(editor)) clear();
    }),
  ];
  current = { editor, subs, heldUntil: 0, range: [start, end] };
  return true;
}

/** Which range is focused right now (1-based, inclusive), or null — for the bridge's
 *  read-only `/editor-state`, so a caller can check the fade survived what came after. */
function active() {
  if (!current) return null;
  const r = current.range;
  return { path: current.editor.document.uri.fsPath, line: r[0] + 1, endLine: r[1] + 1 };
}

/** For the next `ms`, a selection change that is not a click or a key is the bridge's own
 *  (it is about to move the caret to a comment thread and back), not the reader's. Extends,
 *  never shortens; a no-op when nothing is focused. */
function hold(ms) {
  if (current) current.heldUntil = Math.max(current.heldUntil, Date.now() + ms);
}

module.exports = { focus, clear, hold, active, span, fadeSpans, readerMoved, GUARD_MS, MAX_SPAN };
