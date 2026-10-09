// Land on a reference *and* on the PR comment thread that sits on it: expanded, focused,
// so the reader can Reply or Resolve Conversation without hunting for the glyph.
//
// A Human Review card that has been posted to the pull request knows its comment's line.
// The GitHub Pull Requests extension draws that comment as a thread in the file — but
// collapsed or not, it is a glyph in the margin, and the reader who clicked the card came
// for the conversation as much as for the code.
//
// **Not `workbench.action.focusCommentOnCurrentLine`.** It is the obvious command (1.140
// has it, `editor.action.focusCommentOnCurrentLine` does not exist), but with no thread on
// the cursor's line it raises an error notification — and "no thread yet" is the normal
// state for the first second after the file opens, while the PR extension is still
// fetching, and the permanent state on a branch with no active PR. An extension cannot
// read the `activeCursorHasComment` context key to ask first.
//
// `editor.action.nextCommentThreadAction` does the same reveal — `nextCommentThread(true)`
// → `widget.reveal(undefined, CommentWidgetFocus.Widget)`, which expands the zone widget,
// centres it and focuses it — and is silent when there is nothing to go to. It also moves
// the selection onto the thread's range, which is the one thing about it this side can
// observe: the selection ending on the comment's line *is* the proof the thread was found
// and focused. So the cursor goes to the line above the range, the command runs, and the
// selection says whether it landed; nothing moved means the threads are not loaded yet,
// and it tries again for a couple of seconds before settling for the range alone.
//
// The pure part (where to probe from, what a landing means, when to stop) is exported on
// its own so plain node can test it without a VS Code host.

/** How long to keep asking. The PR extension adds threads to an editor a beat after it
 *  becomes visible; past this the PR is not active here, or the comment is not on it. */
const RETRY_MS = 2000;
/** Between two asks. */
const STEP_MS = 200;
/** Threads stepped over before giving up on one landing on the line (another thread
 *  sitting between the probe and the comment). */
const MAX_HOPS = 4;

const NEXT = 'editor.action.nextCommentThreadAction';
const PREVIOUS = 'editor.action.previousCommentThreadAction';

/** Where to put the caret before asking, and which way to ask. 1-based `line` (the
 *  reference's first line) and `commentLine` (the line GitHub anchors the thread on, the
 *  last of its range — where VS Code draws the glyph). Returns a 0-based line, whether the
 *  caret sits at its end, and the command.
 *
 *  Forward from the end of the line above the reference: the nearest thread *starting*
 *  after the caret is the one on the reference. On line 1 there is no line above, so it
 *  goes backward from the end of the comment's line instead. */
function probe(line, commentLine, lineCount) {
  const last = Math.max(1, Number(lineCount) || 1);
  const target = Math.min(Math.max(1, Number(commentLine) || Number(line) || 1), last);
  const start = Math.min(Math.max(1, Number(line) || target), target);
  if (start > 1) return { line0: start - 2, atEnd: true, command: NEXT };
  return { line0: target - 1, atEnd: true, command: PREVIOUS };
}

/** What one ask did, from the selection before and after it (0-based `{startLine,
 *  endLine}`): `'landed'` on the comment's thread, `'none'` when nothing moved (no thread
 *  loaded yet), `'again'` on another thread short of the comment (forward only: step on),
 *  `'missed'` on a thread past it. */
function outcome({ before, after, commentLine, command }) {
  if (!after || (before && before.startLine === after.startLine && before.endLine === after.endLine
      && before.startChar === after.startChar && before.endChar === after.endChar)) return 'none';
  const end1 = after.endLine + 1;
  if (end1 === commentLine) return 'landed';
  if (command === NEXT && end1 < commentLine) return 'again';
  return 'missed';
}

/** Keep trying? Only while nothing was found and there is time left. */
function shouldRetry(result, elapsedMs) {
  return result === 'none' && elapsedMs + STEP_MS <= RETRY_MS;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function snap(sel) {
  return sel && { startLine: sel.start.line, startChar: sel.start.character,
    endLine: sel.end.line, endChar: sel.end.character };
}

/** The selection after a command, once the extension host has heard about it: the change
 *  event can trail the command's own reply. */
function selectionAfter(vscode, editor, ms) {
  return new Promise((resolve) => {
    const done = () => { sub.dispose(); clearTimeout(t); resolve(editor.selection); };
    const sub = vscode.window.onDidChangeTextEditorSelection((e) => { if (e.textEditor === editor) done(); });
    const t = setTimeout(done, ms);
  });
}

let last = null;

/** Reveal, expand and focus the PR comment thread on `commentLine` (1-based) of `editor`.
 *  `line` is where the reference starts. Never throws; resolves to `{ comment: 'focused' |
 *  'none' | 'missed' | 'reader', ms, tries }`. The reader's selection (the range, or the caret) is put
 *  back afterwards, and `hold(ms)` — range-focus's — keeps the bridge's own moves from
 *  counting as the reader taking over and clearing the fade. */
async function focusThread(editor, line, commentLine, { hold } = {}) {
  const vscode = require('vscode');
  const t0 = Date.now();
  const target = Number(commentLine) || Number(line) || 1;
  const keep = editor.selection;
  const quiet = (ms) => { if (hold) hold(ms); };
  let result = 'none', tries = 0, readerTookOver = false;
  // A click or a key in that editor while this runs is the reader deciding for themselves:
  // stop asking, and leave the caret where they put it.
  const watch = vscode.window.onDidChangeTextEditorSelection((e) => {
    if (e.textEditor === editor && (e.kind === 1 || e.kind === 2)) readerTookOver = true;
  });
  quiet(RETRY_MS + 1000);
  try {
    for (;;) {
      if (readerTookOver) break;
      tries++;
      const p = probe(line, target, editor.document.lineCount);
      const at = Math.min(p.line0, editor.document.lineCount - 1);
      const col = p.atEnd ? editor.document.lineAt(at).text.length : 0;
      editor.selection = new vscode.Selection(at, col, at, col);
      for (let hop = 0; hop <= MAX_HOPS; hop++) {
        const before = snap(editor.selection);
        await vscode.commands.executeCommand(p.command);
        const after = snap(await selectionAfter(vscode, editor, 150));
        result = outcome({ before, after, commentLine: target, command: p.command });
        if (result !== 'again') break;
      }
      if (result === 'again') result = 'missed';
      if (readerTookOver || !shouldRetry(result, Date.now() - t0)) break;
      await sleep(STEP_MS);
    }
  } catch (_) {
    result = 'none';
  } finally {
    // Give the reader back what they were given: the range selected (or the caret on the
    // line). The thread widget keeps the focus — setting a selection does not take it.
    watch.dispose();
    quiet(600);
    if (!readerTookOver) {
      try { editor.selection = keep; } catch (_) { /* editor closed meanwhile */ }
    }
  }
  const comment = result === 'landed' ? 'focused' : readerTookOver ? 'reader' : result;
  last = { comment, ms: Date.now() - t0, tries, path: editor.document.uri.fsPath, line: target, at: new Date().toISOString() };
  return { comment, ms: last.ms, tries };
}

/** What the last attempt did, for the bridge's read-only state echo. */
function lastResult() { return last; }

module.exports = { focusThread, lastResult, probe, outcome, shouldRetry, RETRY_MS, STEP_MS, MAX_HOPS, NEXT, PREVIOUS };
