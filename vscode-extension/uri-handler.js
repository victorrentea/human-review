// `vscode://victorrentea.human-review/…` — the channel a page has when it has no server.
//
// The Human Review guide read over http can ask its own origin to do things (that is
// `/__open__` and `/__open_diff__`). Read straight off disk from Chrome — which is how it
// is actually read — a `file://` page has no origin worth asking and no way to reach
// loopback, so every reference degrades to the one thing a browser can still do: hand a
// `vscode://file/…` URL to the OS. That opens the file. It cannot open a *diff*, because
// a diff needs the before-side materialised out of git first, and no page can do that for
// itself. So the click that promised a before/after quietly gave a plain file, and the
// feature read as broken rather than unavailable.
//
// A URI handler closes that gap without touching the scheme registration: `vscode://` stays
// registered to VS Code, its own OAuth callbacks
// (`vscode://vscode.github-authentication/…`) keep working, and LaunchServices is not
// involved. We are just another extension VS Code routes an authority to.
//
// **The window the OS picks is not the window that owns the file.** VS Code hands the URI
// to one extension host — in practice the last-active window's — and that is exactly the
// bug this whole area started with, so the handler does not assume it is the right one. It
// asks the registry under ~/.human-review/ide/ (registry.js) which window's workspace folders contain
// the path (longest prefix wins) and, when that is not us, hands the work over to that
// window's listener instead of doing it here.

const vscode = require('vscode');
const http = require('http');
const fs = require('fs');
const path = require('path');

const { openDiff, ownedHere, REF_RE } = require('./diff');
const { openReviewed } = require('./review-open');
const rangeFocus = require('./range-focus');

const { REGISTRY } = require('./registry');

function get(url, token) {
  return request({ ...url, method: 'GET', headers: { 'x-relay-token': token } });
}

function request(options, body) {
  return new Promise((resolve) => {
    const req = http.request({ timeout: 4000, ...options }, (res) => {
      let out = '';
      res.on('data', (c) => { out += c; });
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(out) }); }
        catch (_) { resolve({ status: res.statusCode, body: null }); }
      });
    });
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.on('error', () => resolve(null));
    if (body) req.end(body); else req.end();
  });
}

/** The window whose workspace folders claim `file` most specifically, or undefined.
 *
 *  Same staleness rule as every other reader of this registry: an entry is trusted only
 *  once the process it names answers on its port with its own token. A refused port means
 *  the window is gone and the file is deleted; a timeout proves nothing and deletes
 *  nothing, because unplugging a live window costs it until its next activation. */
async function ownerOf(file) {
  let best = null;
  let entries = [];
  try { entries = fs.readdirSync(REGISTRY).filter((f) => /^vscode-\d+\.json$/.test(f)).sort(); } catch (_) { return undefined; }
  for (const name of entries) {
    const full = path.join(REGISTRY, name);
    let entry;
    try { entry = JSON.parse(fs.readFileSync(full, 'utf8')); } catch (_) { continue; }
    try { process.kill(entry.pid, 0); } catch (e) {
      if (e.code === 'ESRCH') { try { fs.unlinkSync(full); } catch (_) {} continue; }
    }
    const res = await get({ hostname: '127.0.0.1', port: entry.port, path: '/ping' }, entry.token);
    if (!res) { continue; }
    if (res.status === 0 || !res.body || !res.body.ok || res.body.app !== 'vscode') continue;
    for (const folder of res.body.folders || []) {
      for (const spelling of [folder.path, folder.realPath]) {
        if (!spelling) continue;
        const withSep = spelling.endsWith(path.sep) ? spelling : spelling + path.sep;
        if ((file === spelling || file.startsWith(withSep)) && (!best || spelling.length > best.score)) {
          best = { score: spelling.length, entry, folder: res.body.folder };
        }
      }
    }
  }
  return best || undefined;
}

async function handleDiff(query) {
  const file = query.get('file') || '';
  const base = query.get('base') || '';
  const line = Math.max(1, Number(query.get('line')) || 1);

  // Validated here as well as in diff.js, so a bad ref never reaches the network hop
  // either. Cheap, and the two callers of openDiff are not the only future ones.
  if (!REF_RE.test(base)) {
    vscode.window.showWarningMessage(`Human Review: ${base ? 'that is not a git ref' : 'no git ref given'} — opening the file instead.`);
    return openPlainRouted(file, line);
  }
  if (!path.isAbsolute(file)) return;

  const owner = await ownerOf(file);
  const mine = owner && owner.entry.pid === process.pid;

  if (owner && !mine) {
    const payload = Buffer.from(JSON.stringify({ file, base, line }), 'utf8');
    const res = await request({
      hostname: '127.0.0.1', port: owner.entry.port, path: '/open-diff', method: 'POST',
      headers: {
        'x-relay-token': owner.entry.token,
        'Content-Type': 'application/json',
        'Content-Length': payload.length,
      },
    }, payload);
    if (res && res.body && res.body.ok) return;
    // The owning window refused (no such ref there, file unchanged, or it went away
    // between the ping and now). Say why, once, and still put the reader in the file —
    // never a silent no-op, which is the failure that made this look broken.
    const why = (res && res.body && res.body.error) || 'the window that owns it did not answer';
    vscode.window.showWarningMessage(`Human Review: no diff (${why}) — opening the file instead.`);
    return openPlainRouted(file, line);
  }

  // Either this window owns it, or nobody does and we are the window the OS chose. Doing
  // it here is then the best available answer; `openDiff` still refuses if the path is
  // outside this window's folders, and the fallback below catches that.
  const done = await openDiff({ file, base, line });
  if (!done.ok) {
    vscode.window.showWarningMessage(`Human Review: no diff (${done.error}) — opening the file instead.`);
    return openPlainRouted(file, line);
  }
}

/** The plain-file fallback, routed the same way the diff is.
 *
 *  Falling back must not quietly undo the routing: opening the file in whichever window
 *  the OS happened to hand the URI to is the original bug wearing a different hat — the
 *  right file, in a window belonging to another checkout, where the reference beside it
 *  resolves against the wrong tree. So the owner is resolved first here too, and only a
 *  path nobody owns is opened locally. */
async function openPlainRouted(file, line, endLine) {
  if (!file || !path.isAbsolute(file)) return;
  const owner = await ownerOf(file);
  if (owner && owner.entry.pid !== process.pid) {
    const payload = Buffer.from(JSON.stringify({ path: file, line, endLine, focus: true }), 'utf8');
    const res = await request({
      hostname: '127.0.0.1', port: owner.entry.port, path: '/open-file', method: 'POST',
      headers: {
        'x-relay-token': owner.entry.token,
        'Content-Type': 'application/json',
        'Content-Length': payload.length,
      },
    }, payload);
    if (res && res.body && res.body.ok) return;
  }
  return openPlain(file, line, endLine);
}

async function openPlain(file, line, endLine) {
  if (!file || !path.isAbsolute(file)) return;
  try {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    const at = new vscode.Range(line - 1, 0, line - 1, 0);
    const editor = await vscode.window.showTextDocument(doc, { selection: at, preserveFocus: false });
    rangeFocus.focus(editor, line, endLine);
    try { await vscode.commands.executeCommand('workbench.action.focusWindow'); } catch (_) {}
  } catch (_) { /* the file moved; the warning above already said something */ }
}

/** A guide read off disk has nowhere to show "no window has this", so it is said here, in
 *  the window the OS handed the URI to — with the prompt one click away. */
async function handleReviewOpen(query) {
  const ref = {
    file: query.get('file') || '', line: query.get('line'), endLine: query.get('endLine') || undefined,
    sha: query.get('sha') || '',
    root: query.get('root') || '', branch: query.get('branch') || '',
  };
  const done = await openReviewed(ref);
  if (done.ok) return;
  // A guide built before it carried its commit cannot be checked; it opens as it always did.
  if (done.error === 'bad-request') return openPlainRouted(ref.file, Math.max(1, Number(ref.line) || 1), ref.endLine);
  if (!done.prompt) return vscode.window.showWarningMessage(`Human Review: ${done.message}`);
  const pick = await vscode.window.showErrorMessage(done.message, 'Copy prompt');
  if (pick === 'Copy prompt') await vscode.env.clipboard.writeText(done.prompt);
}

function register(context) {
  context.subscriptions.push(vscode.window.registerUriHandler({
    handleUri(uri) {
      // `uri.query` is a raw query string; URLSearchParams does the percent-decoding that
      // a path with spaces in it needs.
      const query = new URLSearchParams(uri.query || '');
      if (uri.path === '/diff') return handleDiff(query);
      if (uri.path === '/open') return openPlainRouted(query.get('file') || '', Math.max(1, Number(query.get('line')) || 1), query.get('endLine') || undefined);
      if (uri.path === '/review-open') return handleReviewOpen(query);
      vscode.window.showWarningMessage(`Human Review: nothing handles ${uri.path || '/'}`);
    },
  }));
}

module.exports = { register, ownerOf };
