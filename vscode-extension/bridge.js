// The loopback listener a Human Review page reaches VS Code through.
//
// A review page is full of `path:line` references, and a reviewer reading one wants to land
// in the class — in the window that has *that* checkout open, on the reviewed commit. The
// page cannot do it itself: its references are `vscode://file/…` links, which macOS routes to
// the last-active window, and inside VS Code's embedded browser a sandboxed iframe cannot hand
// a custom scheme to the OS at all. What the page *can* do is fetch its own origin, so the
// server it was loaded from (serve-review.py) calls this.
//
// Every window runs its own extension host, so every window listens on its own port (port 0:
// the OS picks a free one) and publishes `{port, token, pid}` under ~/.human-review/ide/
// (registry.js). The caller pings them all and picks the window whose workspace folders
// contain the file, longest prefix first.
//
// Carved out of victor-vsc's relay-terminal.js, without the Walkie Talkie routes (/bind,
// /send, /state, /unbind, /reload): those type into a terminal, and nothing here needs to.

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const vscode = require('vscode');

const { REGISTRY } = require('./registry');
const { openDiff } = require('./diff');
const { openReviewed, markEdited } = require('./review-open');
const rangeFocus = require('./range-focus');
const commentFocus = require('./comment-focus');

let server = null;
let registryFile = null;
let token = null;

function activate(context) {
  server = http.createServer(handle);
  server.on('error', (e) => console.error('[human-review] listener failed:', e.message));

  server.listen(0, '127.0.0.1', () => {
    const port = server.address().port;
    // A shared secret in a 0600 file in the user's home: any local process can reach a
    // loopback port, and this one opens files and raises windows.
    token = crypto.randomBytes(16).toString('hex');
    try {
      fs.mkdirSync(REGISTRY, { recursive: true });
      registryFile = path.join(REGISTRY, `vscode-${process.pid}.json`);
      fs.writeFileSync(registryFile, JSON.stringify({
        app: 'vscode',
        port,
        token,
        pid: process.pid,
        ppid: process.ppid,
      }), { mode: 0o600 });
    } catch (e) {
      console.error('[human-review] could not publish registry entry:', e.message);
    }
  });

  context.subscriptions.push({ dispose: deactivate });
}

function deactivate() {
  if (server) { try { server.close(); } catch (_) {} server = null; }
  if (registryFile) { try { fs.unlinkSync(registryFile); } catch (_) {} registryFile = null; }
}

function send(res, code, body) {
  const data = JSON.stringify(body);
  res.writeHead(code, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) });
  res.end(data);
}

const firstFolder = () => (vscode.workspace.workspaceFolders || [])[0]?.name || null;

/** What `/command` will run. An allowlist, not any command id: the token gates the caller,
 *  the list gates what a caller can do. */
const COMMANDS = new Set([
  // Raise this window. The page's VS Code badge asks the window that has the reviewed
  // checkout to come forward: `open -a "Visual Studio Code" <folder>` reuses a window only
  // when its folder is exactly that path, so a window opened on the parent directory or on a
  // multi-root workspace holding the checkout got a duplicate beside it instead.
  'workbench.action.focusWindow',
  // The status-bar "human-review" click: serves this window's review when nothing does, then
  // opens the page.
  'humanReview.status',
]);

/** What `/command` will run on a folder or file, named by `&uri=` — never without one. Kept
 *  apart from COMMANDS so a URI-taking command cannot be called bare (it would act on whatever
 *  happens to be selected), and so the URI is checked before anything runs: a `file:` URI
 *  inside one of this window's workspace folders, nothing else. */
const URI_COMMANDS = new Set([
  // The Structure tab: a click on a package or a Maven module box shows that folder selected
  // in this window's Explorer.
  'revealInExplorer',
]);

/** Read a JSON body, then call `then(parsed)`; a malformed one is a 400 here. */
function withJson(req, res, then) {
  let body = '';
  req.on('data', (c) => { body += c; if (body.length > 100_000) req.destroy(); });
  req.on('end', () => {
    let parsed;
    try { parsed = JSON.parse(body || '{}'); } catch (_) {
      return send(res, 400, { ok: false, error: 'expected JSON' });
    }
    Promise.resolve(then(parsed)).catch((e) => send(res, 500, { ok: false, error: e.message }));
  });
}

function handle(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (!token || req.headers['x-relay-token'] !== token) {
    return send(res, 403, { ok: false, error: 'bad token' });
  }

  // Who this window is, and which paths it owns. Callers route on `folders` — every folder,
  // not only the first (a multi-root window owns all of them), each as its literal path and
  // its realpath (a checkout reached through a symlink is one tree under two names). `folder`
  // is a *name*, kept for older callers; a name is not an address.
  if (req.method === 'GET' && url.pathname === '/ping') {
    return send(res, 200, {
      ok: true,
      app: 'vscode',
      bridge: 'human-review',
      focused: vscode.window.state.focused,
      folder: firstFolder(),
      folders: (vscode.workspace.workspaceFolders || []).map((f) => {
        const p = f.uri.fsPath;
        let real = p;
        try { real = fs.realpathSync(p); } catch (_) { /* folder went away under us */ }
        return { name: f.name, path: p, realPath: real };
      }),
      activeFile: vscode.window.activeTextEditor?.document.uri.fsPath || null,
    });
  }

  // What this window's editor is showing, for a caller checking that an open landed —
  // read-only. The file, the selection, the range still highlighted with the rest faded
  // (range-focus.js), and what the last comment-thread focus did (comment-focus.js).
  if (req.method === 'GET' && url.pathname === '/editor-state') {
    const ed = vscode.window.activeTextEditor;
    const sel = ed && ed.selection;
    return send(res, 200, {
      ok: true,
      folder: firstFolder(),
      focused: vscode.window.state.focused,
      activeFile: ed ? ed.document.uri.fsPath : null,
      selection: sel ? { start: { line: sel.start.line + 1, character: sel.start.character },
        end: { line: sel.end.line + 1, character: sel.end.character } } : null,
      lastComment: commentFocus.lastResult(),
      rangeFocus: rangeFocus.active(),
    });
  }

  if (req.method === 'POST' && url.pathname === '/command') {
    const id = url.searchParams.get('id');
    const args = [];
    if (URI_COMMANDS.has(id)) {
      let uri;
      try { uri = vscode.Uri.parse(url.searchParams.get('uri') || '', true); } catch (_) { uri = null; }
      if (!uri || uri.scheme !== 'file' || !vscode.workspace.getWorkspaceFolder(uri)) {
        return send(res, 400, { ok: false, error: `${id} needs a file: uri inside this window's folders` });
      }
      args.push(uri);
    } else if (!COMMANDS.has(id)) {
      return send(res, 400, { ok: false, error: `not allowed: ${id}` });
    }
    vscode.commands.executeCommand(id, ...args).then(
      () => send(res, 200, { ok: true, folder: firstFolder() }),
      (err) => send(res, 500, { ok: false, error: err.message }));
    return;
  }

  // Show a URL in this window's embedded browser, beside the code: the review page, when the
  // run that built it is in this window's terminal. The Simple Browser, because an
  // extension's `openExternal` goes to the OS browser. http(s) only: the Simple Browser's
  // iframe renders a `file://` URL as a blank panel with no error anywhere.
  if (req.method === 'POST' && url.pathname === '/open-url') {
    return withJson(req, res, async (parsed) => {
      const target = String(parsed.url || '');
      if (!/^https?:\/\//i.test(target)) {
        return send(res, 400, { ok: false, error: 'http(s) only — an embedded browser cannot load file:// URLs; serve the folder' });
      }
      await vscode.commands.executeCommand('simpleBrowser.api.open', vscode.Uri.parse(target), {
        viewColumn: parsed.beside === false ? vscode.ViewColumn.Active : vscode.ViewColumn.Beside,
        // The caller is a script in a terminal of this window; stealing the caret would land
        // the next thing typed in a browser's URL bar.
        preserveFocus: parsed.preserveFocus !== false,
      });
      send(res, 200, { ok: true, url: target, folder: firstFolder(), view: 'simple-browser' });
    });
  }

  // Open a file at a line (or a range, `endLine`) in this window, and raise the window.
  // `showTextDocument` focuses the editor *within* the window but does not raise it, so a
  // click from a browser in front "did nothing" as far as the reader could see;
  // `workbench.action.focusWindow` is the native raise VS Code's own URL handler runs.
  if (req.method === 'POST' && url.pathname === '/open-file') {
    return withJson(req, res, async (parsed) => {
      const file = String(parsed.path || '');
      if (!path.isAbsolute(file)) return send(res, 400, { ok: false, error: 'absolute path required' });
      const line = Math.max(1, Number(parsed.line) || 1) - 1;
      try {
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
        const editor = await vscode.window.showTextDocument(doc, {
          selection: new vscode.Range(line, 0, line, 0),
          preserveFocus: false,
          viewColumn: vscode.ViewColumn.One,
        });
        // A caller that found the file edited since the commit it is quoting says so, on the
        // line the reader lands on rather than in a toast elsewhere.
        if (parsed.warn) markEdited(editor, line, String(parsed.warn));
        const ranged = rangeFocus.focus(editor, line + 1, parsed.endLine);
        if (parsed.focus !== false) {
          try { await vscode.commands.executeCommand('workbench.action.focusWindow'); } catch (_) {
            // The file is open, which is the part that matters.
          }
        }
        // `comment`: the reference is a card posted to the PR, so the thread GitHub put on it
        // is brought up too — expanded and focused, ready for Reply or Resolve.
        let comment;
        if (parsed.comment === true) {
          const at = Number(parsed.commentLine);
          const commentLine = Number.isInteger(at) && at >= 1 ? at : line + 1;
          comment = (await commentFocus.focusThread(editor, line + 1, commentLine,
            { hold: rangeFocus.hold })).comment;
        }
        send(res, 200, { ok: true, path: doc.uri.fsPath, line: line + 1,
          ...(ranged ? { endLine: Number(parsed.endLine) } : {}),
          ...(comment ? { comment } : {}) });
      } catch (e) {
        send(res, 404, { ok: false, error: e.message, path: file });
      }
    });
  }

  // Open a file as a diff in this window: a committed revision on the left, the working tree
  // on the right (diff.js). A refusal is a 409 with a sentence the caller relays verbatim.
  if (req.method === 'POST' && url.pathname === '/open-diff') {
    return withJson(req, res, async (parsed) => {
      const result = await openDiff({
        file: String(parsed.file || parsed.path || ''),
        base: String(parsed.base || ''),
        line: Number(parsed.line) || 0,
        focus: parsed.focus !== false,
      });
      send(res, result.ok ? 200 : 409, result);
    });
  }

  // Open a reference in the window holding the *reviewed* version of the file
  // (review-open.js). Any window can be asked: it finds the right one itself.
  if (req.method === 'POST' && url.pathname === '/review-open') {
    return withJson(req, res, async (parsed) => {
      const result = await openReviewed({
        file: String(parsed.file || ''), line: parsed.line, endLine: parsed.endLine,
        comment: parsed.comment === true, commentLine: parsed.commentLine,
        sha: String(parsed.sha || ''),
        root: String(parsed.root || ''), branch: String(parsed.branch || ''),
      });
      send(res, result.ok ? 200 : result.error === 'bad-request' ? 400 : 409, result);
    });
  }

  send(res, 404, { ok: false });
}

module.exports = { activate, deactivate };
