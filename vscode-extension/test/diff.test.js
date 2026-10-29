// Plain node, no VS Code host: `node --test test/diff.test.js`. Holds `openCommit` — a Human
// Review page's Fixed card opening its fix commit — to what it promises: both sides of the
// commit out of git, never the working tree; the commit's every file in the changes editor
// with the clicked one first; one file as a plain diff on the clicked line; a refusal in
// words for anything it cannot show.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const Module = require('module');

const repo = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'hr-commit-')));
const git = (...args) => execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8' }).trim();
git('init', '-q');
git('config', 'user.email', 't@t');
git('config', 'user.name', 't');
fs.mkdirSync(path.join(repo, 'src'));
fs.writeFileSync(path.join(repo, 'src', 'A.java'), 'class A {\n  int x;\n}\n');
fs.writeFileSync(path.join(repo, 'src', 'B.java'), 'class B {}\n');
git('add', '.');
git('commit', '-qm', 'impl');
fs.writeFileSync(path.join(repo, 'src', 'A.java'), 'class A {\n  int x;\n  int y;\n}\n');
fs.writeFileSync(path.join(repo, 'src', 'C.java'), 'class C {}\n');
git('add', '.');
git('commit', '-qm', '[auto-fix] two files');
const fix = git('rev-parse', 'HEAD');
const parent = git('rev-parse', 'HEAD^');
fs.writeFileSync(path.join(repo, 'src', 'B.java'), 'class B { int z; }\n');
git('commit', '-qam', '[auto-fix] one file');
const one = git('rev-parse', 'HEAD');
// The tree moves on after the fix: the commit's own sides must not be read from it.
fs.writeFileSync(path.join(repo, 'src', 'A.java'), 'class A { /* later */ }\n');

const ran = [];
let commands = ['vscode.changes', 'vscode.diff'];
const uri = (p) => ({ fsPath: p, toString: () => `file://${p}` });
const stub = {
  workspace: { workspaceFolders: [{ uri: { fsPath: repo } }] },
  commands: {
    executeCommand: async (id, ...args) => { ran.push([id, ...args]); },
    getCommands: async () => commands,
  },
  Uri: { file: uri },
  Range: class { constructor(a, b, c, d) { Object.assign(this, { a, b, c, d }); } },
};
const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  return request === 'vscode' ? 'vscode' : resolve.call(this, request, ...rest);
};
require.cache.vscode = { id: 'vscode', filename: 'vscode', loaded: true, exports: stub };

const { openCommit } = require('../diff');
const file = path.join(repo, 'src', 'A.java');

test('a commit of several files opens in the changes editor, the clicked file first', async () => {
  ran.length = 0;
  const done = await openCommit({ file, base: parent, head: fix, line: 3, focus: false });
  assert.deepStrictEqual(done, { ok: true, files: 2 });
  const [id, title, pairs] = ran[0];
  assert.strictEqual(id, 'vscode.changes');
  assert.strictEqual(title, `${fix.slice(0, 8)} [auto-fix] two files`);
  assert.strictEqual(pairs[0][0].fsPath, file);
  assert.match(fs.readFileSync(pairs[0][1].fsPath, 'utf8'), /^class A \{\n {2}int x;\n\}\n$/);
  // The commit's side, not the tree's later edit.
  assert.match(fs.readFileSync(pairs[0][2].fsPath, 'utf8'), /int y;/);
  // An added file: an empty left side.
  assert.strictEqual(fs.readFileSync(pairs[1][1].fsPath, 'utf8'), '');
});

test('a one-file commit, or no changes editor, is a plain diff on the line', async () => {
  ran.length = 0;
  const done = await openCommit({ file: path.join(repo, 'src', 'B.java'), base: `${one}^`, head: one, line: 1, focus: false });
  assert.strictEqual(done.ok, true);
  assert.strictEqual(ran[0][0], 'vscode.diff');
  assert.strictEqual(ran[0][4].selection.a, 0);
  commands = ['vscode.diff'];
  ran.length = 0;
  await openCommit({ file, base: parent, head: fix, line: 3, focus: false });
  assert.strictEqual(ran[0][0], 'vscode.diff');
  assert.match(ran[0][3], /^A\.java \(/);
  commands = ['vscode.changes', 'vscode.diff'];
});

test('refuses a flag for a ref, a path outside the window, and a commit it does not know', async () => {
  assert.strictEqual((await openCommit({ file, base: '--upload-pack=x', head: fix })).ok, false);
  assert.strictEqual((await openCommit({ file: '/etc/hosts', base: parent, head: fix })).error,
    'that path is not in this window');
  assert.match((await openCommit({ file, base: parent, head: 'deadbeef' })).error, /not a commit here/);
});
