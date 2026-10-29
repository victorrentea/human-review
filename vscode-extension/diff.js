// Open a file as a *diff* — a committed revision on the left, the working tree on
// the right — inside this window.
//
// The Human Review guide's Auto-fixed tab links every applied fix this way. A snippet
// shows the code that is there now, which answers "what does it say" and not "what did
// you change": judging whether a fix was a real finding or the agent overfitting means
// seeing the delta. `serve-review.py` already does this over its own loopback endpoint,
// but only for a guide it is *serving* — a guide opened straight off disk from Chrome
// has no channel to any server, and that is how the report is actually read. The
// extension's URI handler is that missing channel, and this is the work it does.

const vscode = require('vscode');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { execFile } = require('child_process');

// A ref, and nothing that could be read as a flag or a second argument. `git` is spawned
// without a shell, so this is not about quoting — it is about `--upload-pack=…` and
// friends arriving from a query string that anything on this machine can compose. Hence
// the first character: a ref may not open with `-`, which is the whole of the attack.
//
// It used to be `[0-9a-fA-F]{7,40}` — a raw sha and nothing else. But every source bar in
// a Human Review guide is written against `origin/main`, so every one of those handles
// arrived here and was turned away with "that is not a git ref": the page's most-clicked
// button, refused by the check meant to let it through, and the reader got the file
// instead of the diff. A name is not less safe than a sha — it is resolved through git
// before it reaches anything, and a name git cannot resolve is refused exactly as before.
// serve-review.py widened the same regex for the same reason; these two are a pair.
const REF_RE = /^[0-9A-Za-z][0-9A-Za-z._/+^~{}-]{0,100}$/;

function git(cwd, args, encoding) {
  return new Promise((resolve) => {
    execFile('git', ['-C', cwd, ...args], { encoding: encoding || 'utf8', maxBuffer: 64 * 1024 * 1024 },
      (err, stdout) => resolve(err ? null : stdout));
  });
}

/** Is `file` inside one of this window's workspace folders? The same containment test the
 *  router outside uses, asked from in here — a window must not be talked into opening a
 *  tree it has nothing to do with. */
function ownedHere(file) {
  for (const folder of vscode.workspace.workspaceFolders || []) {
    for (const root of new Set([folder.uri.fsPath, safeReal(folder.uri.fsPath)])) {
      const withSep = root.endsWith(path.sep) ? root : root + path.sep;
      if (file === root || file.startsWith(withSep)) return true;
    }
  }
  return false;
}

function safeReal(p) {
  try { return fs.realpathSync(p); } catch (_) { return p; }
}

/**
 * @returns {Promise<{ok: true} | {ok: false, error: string}>}
 *
 * Refuses rather than improvises. If the ref does not resolve, or the file did not exist
 * in it, or the two sides are identical, there is no diff to show — and a diff with an
 * invented left half is worse than no diff, because it looks exactly like evidence.
 */
async function openDiff({ file, base, line = 0, focus = true }) {
  if (!file || !path.isAbsolute(file)) return { ok: false, error: 'absolute path required' };
  if (!REF_RE.test(base || '')) return { ok: false, error: 'not a usable git ref' };
  if (!ownedHere(safeReal(file)) && !ownedHere(file)) {
    return { ok: false, error: 'that path is not in this window' };
  }
  let after;
  try { after = fs.readFileSync(file); } catch (_) {
    return { ok: false, error: `${path.basename(file)} is no longer in the working tree` };
  }

  const root = (await git(path.dirname(file), ['rev-parse', '--show-toplevel']) || '').trim();
  if (!root) return { ok: false, error: 'that file is not in a git repository' };
  const rel = path.relative(root, file);
  if (rel.startsWith('..')) return { ok: false, error: 'that file is outside the repository' };

  const shown = await git(root, ['show', `${base}:${rel}`], 'buffer');
  if (shown === null) return { ok: false, error: `${path.basename(file)} does not exist at ${base.slice(0, 8)}` };
  if (Buffer.from(shown).equals(after)) {
    return { ok: false, error: `${path.basename(file)} is unchanged since ${base.slice(0, 8)}` };
  }

  // Named `<stem>@<short><ext>` rather than `<name>@<short>`, so the extension survives
  // and the left pane keeps its syntax highlighting — and so the tab reads
  // `Owner@cb0988f5.java ↔ Owner.java`, which says what is being compared.
  //
  // Beside the report when there is one, matching where serve-review.py puts it so the two
  // routes do not each leave their own copy; otherwise in a temp dir. Unlike the served
  // route, *where* it lands no longer decides which window opens it — `vscode.diff` runs
  // in this extension host, so the window is already settled.
  const short = base.slice(0, 8);
  const parsed = path.parse(rel);
  const reportDir = path.join(root, '.human-review');
  const holder = fs.existsSync(reportDir) ? reportDir : path.join(os.tmpdir(), 'human-review-diffbase');
  const before = path.join(holder, '.diffbase', short, parsed.dir, `${parsed.name}@${short}${parsed.ext}`);
  try {
    fs.mkdirSync(path.dirname(before), { recursive: true });
    fs.writeFileSync(before, shown);
  } catch (e) {
    return { ok: false, error: `could not write the before-image: ${e.message}` };
  }

  // `selection` lands the diff on the line the click was about instead of at the top of
  // the file. Without it a fix five hundred lines down opens with the reader looking at
  // the package declaration, hunting for the change — which is the hunt the handle they
  // clicked exists to end. It is a `TextDocumentShowOptions`, so it applies to the
  // modified side, which is the one with a line number the caller could have known.
  const at = line > 0 ? new vscode.Range(line - 1, 0, line - 1, 0) : undefined;
  await vscode.commands.executeCommand(
    'vscode.diff',
    vscode.Uri.file(before), vscode.Uri.file(file),
    `${parsed.name}@${short}${parsed.ext} ↔ ${path.basename(file)}`,
    { preview: false, selection: at },
  );
  if (focus) {
    try { await vscode.commands.executeCommand('workbench.action.focusWindow'); } catch (_) { /* the diff is open regardless */ }
  }
  return { ok: true };
}

/**
 * A commit's own diff — `base` its parent, `head` the commit — as the commit's every file in
 * the changes editor, `file` first (VS Code ≥ 1.86's `vscode.changes`), or as `file` alone
 * when that command is missing or the commit changed nothing else. A Human Review page's
 * Fixed card opens its fix commit this way (Victor, 30 Oct 2026): the fix is a commit, so
 * the click shows the commit, not the file against whatever the tree holds since.
 *
 * Both sides come out of git, never the working tree, and are written beside the report
 * like `openDiff`'s before-image. Refuses rather than improvises, in the same words.
 * @returns {Promise<{ok: true, files: number} | {ok: false, error: string}>}
 */
async function openCommit({ file, base, head, line = 0, focus = true }) {
  if (!file || !path.isAbsolute(file)) return { ok: false, error: 'absolute path required' };
  if (!REF_RE.test(base || '') || !REF_RE.test(head || '')) return { ok: false, error: 'not a usable git ref' };
  if (!ownedHere(safeReal(file)) && !ownedHere(file)) {
    return { ok: false, error: 'that path is not in this window' };
  }
  let dir = path.dirname(file);
  while (!fs.existsSync(dir) && path.dirname(dir) !== dir) dir = path.dirname(dir);
  const root = (await git(dir, ['rev-parse', '--show-toplevel']) || '').trim();
  if (!root) return { ok: false, error: 'that file is not in a git repository' };
  const rel = path.relative(root, file).split(path.sep).join('/');
  if (rel.startsWith('..')) return { ok: false, error: 'that file is outside the repository' };
  const sha = async (ref) => (await git(root, ['rev-parse', '--verify', '--quiet', `${ref}^{commit}`]) || '').trim();
  const [from, to] = [await sha(base), await sha(head)];
  if (!from || !to) return { ok: false, error: `${(from ? head : base).slice(0, 8)} is not a commit here` };
  const listed = (await git(root, ['diff', '--name-status', '--no-renames', from, to]) || '')
    .split('\n').filter(Boolean).map((l) => { const [st, p] = l.split('\t'); return { st, p }; });
  if (!listed.length) return { ok: false, error: `${to.slice(0, 8)} changes nothing` };
  listed.sort((a, b) => (b.p === rel) - (a.p === rel));
  const [bs, hs] = [from.slice(0, 8), to.slice(0, 8)];
  const reportDir = path.join(root, '.human-review');
  const holder = fs.existsSync(reportDir) ? reportDir : path.join(os.tmpdir(), 'human-review-diffbase');
  const side = async (ref, short, p, absent) => {
    const parsed = path.parse(p);
    const out = path.join(holder, '.diffbase', hs, parsed.dir, `${parsed.name}@${short}${parsed.ext}`);
    const shown = absent ? Buffer.alloc(0) : await git(root, ['show', `${ref}:${p}`], 'buffer');
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, shown || Buffer.alloc(0));
    return vscode.Uri.file(out);
  };
  let pairs;
  try {
    pairs = [];
    for (const f of listed) {
      pairs.push([vscode.Uri.file(path.join(root, f.p)),
        await side(from, bs, f.p, f.st === 'A'), await side(to, hs, f.p, f.st === 'D')]);
    }
  } catch (e) {
    return { ok: false, error: `could not write the commit's files: ${e.message}` };
  }
  const subject = ((await git(root, ['log', '-1', '--format=%s', to])) || '').trim();
  const all = await vscode.commands.getCommands(true);
  if (pairs.length > 1 && all.includes('vscode.changes')) {
    await vscode.commands.executeCommand('vscode.changes', `${hs} ${subject}`.trim(), pairs);
  } else {
    const mine = pairs[0];
    const name = path.basename(rel);
    const at = line > 0 ? new vscode.Range(line - 1, 0, line - 1, 0) : undefined;
    await vscode.commands.executeCommand('vscode.diff', mine[1], mine[2],
      `${name} (${hs} ${subject})`.trim(), { preview: false, selection: at });
  }
  if (focus) {
    try { await vscode.commands.executeCommand('workbench.action.focusWindow'); } catch (_) { /* open regardless */ }
  }
  return { ok: true, files: pairs.length };
}

module.exports = { openDiff, openCommit, ownedHere, REF_RE };
