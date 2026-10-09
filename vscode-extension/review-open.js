// Open a Human Review reference in the window that has *the reviewed version* of the file —
// or say plainly that no window does.
//
// A reference on the guide quotes a file as it was at the commit the review was built from.
// Handing its absolute path to whichever window VS Code picks is how the reader ended up in
// another checkout's copy, or in a copy three commits later, with nothing saying so. So a
// window qualifies only if the file at its HEAD is the same blob as the file at the reviewed
// commit. Per file, not per commit: commits that do not touch it change nothing the reader
// sees; one that does moves the lines, and then the link would lie.
//
// No qualifying window is an error carrying a prompt for an agent, never a fallback. And
// nothing here checks anything out: a branch switched under a session working in that
// checkout is how someone else's work gets swept away.

const vscode = require('vscode');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const rangeFocus = require('./range-focus');
const commentFocus = require('./comment-focus');

const { REGISTRY } = require('./registry');
const SHA_RE = /^[0-9a-f]{7,40}$/i;

function git(cwd, args) {
  return new Promise((resolve) => {
    execFile('git', ['-C', cwd, ...args], { encoding: 'utf8' },
      (err, stdout) => resolve(err ? null : stdout.trim()));
  });
}

function call(entry, method, route, body) {
  return new Promise((resolve) => {
    const data = body ? Buffer.from(JSON.stringify(body), 'utf8') : null;
    const req = http.request({
      hostname: '127.0.0.1', port: entry.port, path: route, method, timeout: 4000,
      headers: { 'x-relay-token': entry.token, ...(data ? { 'Content-Type': 'application/json', 'Content-Length': data.length } : {}) },
    }, (res) => {
      let out = '';
      res.on('data', (c) => { out += c; });
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(out) }); } catch (_) { resolve({ status: res.statusCode, body: null }); }
      });
    });
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.on('error', () => resolve(null));
    req.end(data || undefined);
  });
}

function hereFolders() {
  return (vscode.workspace.workspaceFolders || []).map((f) => {
    let real = f.uri.fsPath;
    try { real = fs.realpathSync(real); } catch (_) { /* folder went away */ }
    return { name: f.name, path: f.uri.fsPath, realPath: real };
  });
}

/** Every VS Code window that answers on its port, this one included (asked directly, not
 *  over loopback to itself). */
async function liveWindows() {
  let names = [];
  try { names = fs.readdirSync(REGISTRY).filter((f) => /^vscode-\d+\.json$/.test(f)).sort(); } catch (_) { return []; }
  const out = [];
  for (const name of names) {
    let entry;
    try { entry = JSON.parse(fs.readFileSync(path.join(REGISTRY, name), 'utf8')); } catch (_) { continue; }
    if (entry.pid === process.pid) { out.push({ entry, folders: hereFolders(), self: true }); continue; }
    const res = await call(entry, 'GET', '/ping');
    if (!res || !res.body || !res.body.ok || res.body.app !== 'vscode') continue;
    out.push({ entry, folders: res.body.folders || [], self: false });
  }
  return out;
}

const inside = (p, dir) => !!dir && (p === dir || p.startsWith(dir.endsWith(path.sep) ? dir : dir + path.sep));

/**
 * @param {{file: string, sha: string, root: string, branch?: string}} ref
 *   `file` is the absolute path the guide was built with, `root` the checkout it was built
 *   in, `sha` that checkout's HEAD at build time.
 */
async function locate({ file, sha, root, branch }) {
  if (!path.isAbsolute(file || '') || !path.isAbsolute(root || '') || !SHA_RE.test(sha || '')) {
    return { ok: false, error: 'bad-request' };
  }
  const rel = path.relative(root, file);
  if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) return { ok: false, error: 'bad-request' };
  const relGit = rel.split(path.sep).join('/');

  // Every repository a window shows that could hold this file: the one each folder sits in,
  // and — for a window opened on a directory above several checkouts — the one the file's
  // own path sits in.
  const windows = await liveWindows();
  const candidates = [];
  for (const w of windows) {
    for (const f of w.folders) {
      const tops = new Set();
      const top = await git(f.realPath || f.path, ['rev-parse', '--show-toplevel']);
      if (top) tops.add(top);
      if (inside(file, f.path) || inside(file, f.realPath)) {
        const own = await git(path.dirname(file), ['rev-parse', '--show-toplevel']);
        if (own) tops.add(own);
      }
      for (const t of tops) {
        const target = path.join(t, rel);
        if (!inside(target, f.realPath) && !inside(target, f.path)) continue;
        // The window owning the very path the guide names is the best claim; the deeper its
        // folder, the more specific.
        const score = inside(file, f.path) || inside(file, f.realPath) ? (f.path || '').length : 0;
        candidates.push({ w, folder: f.name, top: t, target, score });
      }
    }
  }

  // The blob the guide quoted. The checkout it was built in knows the commit while it is
  // still on this machine; a window's own clone may know it too, once fetched.
  let want = await git(root, ['rev-parse', '--verify', '--quiet', `${sha}:${relGit}`]);
  for (const c of candidates) {
    if (want) break;
    want = await git(c.top, ['rev-parse', '--verify', '--quiet', `${sha}:${relGit}`]);
  }

  const seen = [];
  const hits = [];
  for (const c of candidates) {
    const head = await git(c.top, ['rev-parse', '--short=8', 'HEAD']);
    const on = await git(c.top, ['branch', '--show-current']);
    seen.push(`${c.folder} (${on || 'detached'} @ ${head || '?'})`);
    if (!want) continue;
    const have = await git(c.top, ['rev-parse', '--verify', '--quiet', `HEAD:${relGit}`]);
    if (have !== want) continue;
    const status = await git(c.top, ['status', '--porcelain', '--', relGit]);
    hits.push({ ...c, edited: status !== null && status !== '' });
  }
  if (hits.length) {
    hits.sort((a, b) => b.score - a.score);
    return { ok: true, hit: hits[0], short: sha.slice(0, 8) };
  }

  const short = sha.slice(0, 8);
  const on = branch || await git(root, ['branch', '--show-current']) || 'detached';
  const remote = await git(root, ['remote', 'get-url', 'origin']);
  const open = [...new Set(seen)];
  return {
    ok: false,
    error: 'no-window',
    message: `No open VS Code window is on this commit (${on} @ ${short})`,
    prompt: [
      `I'm reviewing ${relGit} at commit ${sha} (branch ${on}${remote ? `, repo ${remote}` : ''}), `
        + 'but no open VS Code window has that version of the file.',
      `Open VS Code windows: ${open.length ? open.join(', ') : 'none'}.`,
      `Find a local checkout of this repository whose HEAD is ${sha} — the review was built in ${root} — `
        + 'and open it in VS Code (`code <dir>`).',
      'Do not check out, switch, reset or stash in any existing checkout: other sessions may be working in them. '
        + 'If no checkout is at that commit, stop and tell me which one you would use.',
    ].join('\n'),
  };
}

let editedDeco = null;

/** The "this is not the reviewed text any more" poster, on the line the reader landed on.
 *  It lives as long as that editor does: switching tabs drops it, which is right — it is
 *  about the moment of arriving, not a permanent stain on the file. */
function markEdited(editor, line0, text) {
  if (!editor || !text) return;
  if (!editedDeco) {
    editedDeco = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: 'rgba(255, 170, 0, 0.12)',
      overviewRulerColor: new vscode.ThemeColor('editorWarning.foreground'),
      after: { margin: '0 0 0 2em', color: new vscode.ThemeColor('editorWarning.foreground'), fontStyle: 'italic' },
    });
  }
  const at = new vscode.Range(line0, 0, line0, 0);
  editor.setDecorations(editedDeco, [{ range: at, hoverMessage: text, renderOptions: { after: { contentText: text } } }]);
}

const editedText = (short) => `⚠ Edited since the reviewed commit ${short}: lines may have moved`;

async function revealHere(file, line, warn, endLine, commentLine) {
  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  const at = new vscode.Range(line - 1, 0, line - 1, 0);
  const editor = await vscode.window.showTextDocument(doc, { selection: at, preserveFocus: false, viewColumn: vscode.ViewColumn.One });
  editor.revealRange(at, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
  markEdited(editor, line - 1, warn);
  rangeFocus.focus(editor, line, endLine);
  try { await vscode.commands.executeCommand('workbench.action.focusWindow'); } catch (_) { /* opened regardless */ }
  // A card posted to the PR: its thread too, expanded and focused (comment-focus.js).
  if (commentLine) return (await commentFocus.focusThread(editor, line, commentLine, { hold: rangeFocus.hold })).comment;
  return undefined;
}

/** Locate, then open in the window that qualified. Never throws; the result says what
 *  happened in words the caller can show as they are. */
async function openReviewed(ref) {
  const line = Math.max(1, Number(ref.line) || 1);
  // Passed on as given: range-focus.js decides whether it is a range worth focusing.
  const endLine = ref.endLine == null || ref.endLine === '' ? undefined : Number(ref.endLine);
  // `comment`: the card has a PR thread on this file; `commentLine` is the line GitHub
  // anchored it on, the reference's own first line when not given.
  const at = Number(ref.commentLine);
  const commentLine = ref.comment === true ? (Number.isInteger(at) && at >= 1 ? at : line) : undefined;
  const found = await locate(ref);
  if (!found.ok) return found;
  const { w, target, edited, folder } = found.hit;
  const warn = edited ? editedText(found.short) : '';
  if (w.self) {
    let comment;
    try { comment = await revealHere(target, line, warn, endLine, commentLine); } catch (e) { return { ok: false, error: 'open-failed', message: e.message }; }
    return { ok: true, folder, path: target, edited, ...(comment ? { comment } : {}) };
  }
  const res = await call(w.entry, 'POST', '/open-file', { path: target, line, endLine, focus: true, warn,
    ...(commentLine ? { comment: true, commentLine } : {}) });
  if (res && res.body && res.body.ok) return { ok: true, folder, path: target, edited,
    ...(res.body.comment ? { comment: res.body.comment } : {}) };
  return { ok: false, error: 'open-failed', message: `${folder} did not open ${path.basename(target)}` };
}

module.exports = { openReviewed, locate, markEdited };
