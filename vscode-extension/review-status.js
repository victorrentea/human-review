// "human-review" in the status bar: is a Human Review of this window's checkout being served?
//
//   grey   — `.human-review/review.html` exists, but no serve-review.py is serving it
//   green  — a live server serves `<folder>/.human-review`, and the checkout's HEAD is the
//            reviewed commit (`data-hr-head` on the <html> of review.html)
//   amber  — served, but the checkout is on another commit: links in the page open only the
//            files unchanged since
//   hidden — the window has no review on disk (a grey item in every window is noise)
//
// How it is detected, and why exactly so:
// - `.server.json` (written by serve-review.py beside review.html) gives the port and pid.
//   Only a hint — it survives a `kill -9`, and the port may belong to someone else by now.
// - The proof is the marker (`GET /__human_review__`): key `humanReview`, `served` = exactly
//   our directory, `pid` = the one in the file. Asked ONCE per (port, pid): any GET other than
//   /__watch__ and /__editor__ resets the server's `--idle-minutes` clock, so polling the
//   marker every 10 s would keep it alive forever.
// - After that, each tick is only `kill(pid, 0)` + reading the file: no network. A server
//   that stops normally deletes its `.server.json`; a killed one leaves the file but its pid
//   dies — grey either way.
// One timer per window: 10 s while served, then 5 → 10 → 20 → 30 s while nothing is; a
// FileSystemWatcher on `.server.json`/`review.html`, window focus and any HEAD change from
// the git extension refresh at once.
//
// A click on grey starts the server (`serve-review.py … --no-open`, which reuses a live one on
// the same folder), then opens the page exactly like a click on green.
const fs = require('fs');
const path = require('path');
const http = require('http');
const os = require('os');
const { execFile } = require('child_process');
const { git } = require('./git');

const DIR = '.human-review';
const PAGE = 'review.html';
const IDENTITY = '.server.json';
const MARKER = '/__human_review__';
const MARKER_KEY = 'humanReview';

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

// What the build stamped on <html> and in <title>; it sits in the first ~1 KB of a file of a
// few MB, so only the head is read, and only when its mtime changed.
const stampCache = new Map();
function readStamp(page) {
  let st;
  try { st = fs.statSync(page); } catch { return null; }
  const hit = stampCache.get(page);
  if (hit && hit.mtime === st.mtimeMs) return hit.stamp;
  let head = '';
  try {
    const fd = fs.openSync(page, 'r');
    try {
      const buf = Buffer.alloc(8192);
      head = buf.toString('utf8', 0, fs.readSync(fd, buf, 0, buf.length, 0));
    } finally { fs.closeSync(fd); }
  } catch { return null; }
  const attr = (name) => (new RegExp(`\\b${name}="([^"]*)"`).exec(head) || [])[1] || '';
  const title = ((/<title>([^<]*)<\/title>/i.exec(head) || [])[1] || '')
    .replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" })[e]);
  const stamp = { sha: attr('data-hr-head'), branch: attr('data-hr-branch'), title: title.trim() };
  stampCache.set(page, { mtime: st.mtimeMs, stamp });
  return stamp;
}

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

function getMarker(port, timeout = 1500) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: MARKER, timeout }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { body += c; if (body.length > 1 << 20) req.destroy(); });
      res.on('end', () => {
        if (res.statusCode !== 200) return resolve({ ok: false, answered: true });
        try { resolve({ ok: true, info: JSON.parse(body) }); }
        catch { resolve({ ok: false, answered: true }); }
      });
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve({ ok: false, answered: false }));
  });
}

async function checkoutHead(folder) {
  try {
    const [head, ref] = (await git(folder, ['rev-parse', 'HEAD', '--abbrev-ref', 'HEAD'])).split('\n');
    return { head, branch: ref === 'HEAD' ? 'detached' : ref };
  } catch { return null; }
}

/**
 * The review state of one workspace folder. No `vscode` here, so it can run from plain node
 * against live servers. `memo` keeps marker checks between ticks: `verified` = confirmed
 * (dir|port|pid) keys, `rejected` = those someone else answered (not asked again — no point
 * resetting their idle clock).
 */
async function probeFolder(folder, memo = { verified: new Set(), rejected: new Set() }) {
  const dir = path.join(folder, DIR);
  const page = path.join(dir, PAGE);
  const stamp = readStamp(page);
  if (!stamp) return { state: 'none', folder };
  const base = { folder, dir, title: stamp.title, reviewed: { sha: stamp.sha, branch: stamp.branch } };

  const rec = readJson(path.join(dir, IDENTITY));
  const port = rec && rec.port, pid = rec && rec.pid;
  if (!Number.isInteger(port) || !pidAlive(pid)) return { state: 'off', ...base };
  let real;
  try { real = fs.realpathSync(dir); } catch { real = dir; }
  const key = `${real}|${port}|${pid}`;
  if (memo.rejected.has(key)) return { state: 'off', ...base };
  if (!memo.verified.has(key)) {
    const m = await getMarker(port);
    const info = m.ok && m.info;
    if (info && info[MARKER_KEY] && info.served === real && info.pid === pid) {
      memo.verified.add(key);
    } else {
      if (m.answered || info) memo.rejected.add(key);  // someone else on the port: stop asking
      return { state: 'off', ...base };
    }
  }

  const live = { ...base, port, pid, url: `http://127.0.0.1:${port}/${PAGE}` };
  const co = await checkoutHead(folder);
  live.checkout = co;
  const at = !stamp.sha || (co && co.head && co.head.startsWith(stamp.sha));
  return { state: at ? 'on' : 'near', ...live };
}

const RANK = { none: 0, off: 1, near: 2, on: 3 };

/** Cel mai „aprins" dintre folderele ferestrei (multi-root: unul servit bate unul gri). */
async function probeFolders(folders, memo) {
  let best = { state: 'none' };
  for (const f of folders) {
    const s = await probeFolder(f, memo);
    if (RANK[s.state] > RANK[best.state]) best = s;
  }
  return best;
}

// --------------------------------------------------------------------------- UI

const short = (sha) => (sha || '').slice(0, 8);
const where = (b, sha) => `${b ? b + ' @ ' : ''}${short(sha) || '?'}`;

// serve-review.py: the `humanReview.serveScript` setting if set, then the installed Claude
// Code plugin's copy (the one the skill runs), then the newest in the plugin cache, then a
// checkout in ~/workspace. No hand-written hash — every plugin update changes it.
function serveScript(configured) {
  if (configured && fs.existsSync(configured)) return configured;
  const home = os.homedir();
  const rel = path.join('skills', 'human-review', 'scripts', 'serve-review.py');
  const cands = [];
  const installed = readJson(path.join(home, '.claude/plugins/installed_plugins.json'));
  const rec = installed && (installed.plugins || installed)['human-review@human-review'];
  if (Array.isArray(rec)) rec.forEach((r) => r && r.installPath && cands.push(path.join(r.installPath, rel)));
  const cache = path.join(home, '.claude/plugins/cache/human-review/human-review');
  let cached = [];
  try {
    cached = fs.readdirSync(cache).map((h) => path.join(cache, h, rel))
      .map((f) => { try { return { f, t: fs.statSync(f).mtimeMs }; } catch { return null; } })
      .filter(Boolean).sort((a, b) => b.t - a.t).map((x) => x.f);
  } catch { /* no plugin installed */ }
  cands.push(...cached, path.join(home, 'workspace/human-review', rel));
  return cands.find((f) => fs.existsSync(f)) || null;
}

// python3 from the extension host's PATH, with fallbacks for a thin PATH (started from the Dock).
function python() {
  const dirs = (process.env.PATH || '').split(path.delimiter).filter(Boolean)
    .concat(['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin']);
  for (const d of dirs) {
    const f = path.join(d, 'python3');
    try { fs.accessSync(f, fs.constants.X_OK); return f; } catch { /* next */ }
  }
  return 'python3';
}

/**
 * Start (or reuse) the server for `<folder>/.human-review` and resolve to the page's URL. The
 * script daemonizes itself (`--_child`, new session) and exits once the port answers, so
 * `execFile` does not hold the extension host — it only waits for the parent, ~1 s.
 * `--no-open`: the caller opens the page, the same as a click on the served state.
 */
function startServer(folder, script = serveScript()) {
  if (!script) return Promise.reject(new Error('serve-review.py not found — is the human-review plugin installed?'));
  const args = [script, path.join(folder, DIR), '--idle-minutes', '240', '--no-open'];
  return new Promise((resolve, reject) => {
    execFile(python(), args, { timeout: 30000 }, (err, stdout, stderr) => {
      const url = String(stdout || '').trim().split('\n').pop().trim();
      if (!err && /^http:\/\/127\.0\.0\.1:\d+\//.test(url)) return resolve(url);
      const tail = String(stderr || '').trim().split('\n').slice(-5).join('\n')
        || (err && err.message) || `unexpected output: ${url}`;
      reject(new Error(tail));
    });
  });
}

function register(context) {
  const vscode = require('vscode');
  // Leftmost, so it is always in the same place however long the rest of the bar gets.
  const item = vscode.window.createStatusBarItem('humanreview', vscode.StatusBarAlignment.Left, 1000001);
  item.name = 'Human Review';
  context.subscriptions.push(item);

  const memo = { verified: new Set(), rejected: new Set() };
  let current = { state: 'none' };
  let timer, idle = 0, running = false, again = false, disposed = false, starting = null;

  function paint(s) {
    current = s;
    if (s.state === 'none') { item.hide(); return; }
    const name = path.basename(s.folder);
    const md = new vscode.MarkdownString(undefined, true);
    md.supportThemeIcons = true;
    const title = s.title ? `**${s.title.replace(/[\\`*_[\]<>]/g, '\\$&')}**\n\n` : '';
    const reviewed = where(s.reviewed && s.reviewed.branch, s.reviewed && s.reviewed.sha);
    if (s.state === 'off') {
      item.text = starting ? 'human-review $(sync~spin)' : 'human-review';
      item.color = new vscode.ThemeColor('disabledForeground');
      md.appendMarkdown(`$(circle-outline) Human Review — not served\n\n${title}`
        + `${name}/${DIR}/${PAGE} reviews ${reviewed}, but no review server is serving it.\n\n`
        + (starting ? '$(sync~spin) Starting the review server…' : 'Click to serve it.'));
    } else {
      item.text = 'human-review $(circle-filled)';
      const co = s.checkout ? where(s.checkout.branch, s.checkout.head) : 'unknown';
      if (s.state === 'on') {
        item.color = new vscode.ThemeColor('testing.iconPassed');
        md.appendMarkdown(`$(pass-filled) Human Review — served on :${s.port}\n\n${title}`
          + `Reviewing ${reviewed}, which is what this checkout has.\n\n`);
      } else {
        item.color = new vscode.ThemeColor('editorWarning.foreground');
        md.appendMarkdown(`$(warning) Human Review — served on :${s.port}, other commit\n\n${title}`
          + `Reviewing ${reviewed}, but this checkout is on ${co}: `
          + 'links in the page open only files unchanged since.\n\n');
      }
      md.appendMarkdown(`Click: open ${s.url} in the browser.`);
    }
    item.tooltip = md;
    item.command = 'humanReview.status';
    item.show();
  }

  async function tick() {
    if (disposed) return;
    clearTimeout(timer);
    if (running) { again = true; return; }
    running = true;
    try {
      const folders = (vscode.workspace.workspaceFolders || [])
        .filter((f) => f.uri.scheme === 'file').map((f) => f.uri.fsPath);
      const s = await probeFolders(folders, memo);
      paint(s);
      idle = s.state === 'on' || s.state === 'near' ? 0 : idle + 1;
    } catch { /* the next tick tries again */ }
    running = false;
    if (again) { again = false; return tick(); }
    // Served: 10 s, enough to notice a stopped server. Nothing: 5 → 10 → 20 → 30 s; a newly
    // started server is caught at once anyway by the watcher on `.server.json`.
    const delay = idle === 0 ? 10000 : Math.min(30000, 5000 * 2 ** Math.min(idle - 1, 3));
    if (!disposed) timer = setTimeout(tick, delay);
  }
  const soon = () => { idle = Math.min(idle, 1); setTimeout(tick, 150); };

  const watchers = [];
  function rewatch() {
    watchers.splice(0).forEach((w) => w.dispose());
    for (const f of vscode.workspace.workspaceFolders || []) {
      const w = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(f, `${DIR}/{${IDENTITY},${PAGE}}`));
      w.onDidCreate(soon); w.onDidChange(soon); w.onDidDelete(soon);
      watchers.push(w);
    }
  }
  rewatch();

  context.subscriptions.push(
    vscode.commands.registerCommand('humanReview.status', async () => {
      const s = current;
      if (s.state === 'on' || s.state === 'near') {
        await vscode.env.openExternal(vscode.Uri.parse(s.url));
        return;
      }
      if (s.state !== 'off' || starting) return;
      const configured = vscode.workspace.getConfiguration('humanReview').get('serveScript');
      starting = startServer(s.folder, serveScript(configured));
      paint(current);
      try {
        const url = await starting;
        starting = null;
        idle = 0;
        tick();  // marker verified now → green/amber without waiting for the timer
        await vscode.env.openExternal(vscode.Uri.parse(url));
      } catch (e) {
        starting = null;
        paint(current);
        vscode.window.showErrorMessage(
          `Could not serve ${path.basename(s.folder)}/${DIR}: ${e.message}`);
      }
    }),
    vscode.window.onDidChangeWindowState((st) => { if (st.focused) soon(); }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => { rewatch(); soon(); }),
    { dispose: () => { disposed = true; clearTimeout(timer); watchers.forEach((w) => w.dispose()); } },
  );

  // HEAD moved (checkout, commit) → green/amber repaints at once, not on the next tick.
  (async () => {
    const ext = vscode.extensions.getExtension('vscode.git');
    if (!ext) return;
    try {
      const api = (ext.isActive ? ext.exports : await ext.activate()).getAPI(1);
      let lastHeads = '';
      const onRepo = () => {
        const heads = api.repositories.map((r) => r.state.HEAD && r.state.HEAD.commit).join(',');
        if (heads !== lastHeads) { lastHeads = heads; if (current.state !== 'none') soon(); }
      };
      const watch = (r) => context.subscriptions.push(r.state.onDidChange(onRepo));
      api.repositories.forEach(watch);
      context.subscriptions.push(api.onDidOpenRepository((r) => { watch(r); onRepo(); }));
    } catch { /* no git extension: the tick remains */ }
  })();

  tick();
}

module.exports = { register, probeFolder, probeFolders, readStamp, serveScript, startServer };
