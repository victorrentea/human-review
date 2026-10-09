// Plain node, no VS Code host: `node --test test/bridge.test.js`. Starts the real listener
// against a stub `vscode` module and holds it to the contract serve-review.py relies on: it
// publishes {port, token} under ~/.human-review/ide/, refuses a caller without the token,
// answers /ping with every folder it owns, and runs only allowlisted commands.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hr-bridge-'));
process.env.HOME = home;
const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'hr-folder-'));

const ran = [];
const stub = {
  window: { state: { focused: true }, activeTextEditor: undefined },
  workspace: {
    workspaceFolders: [{ name: 'shop', uri: { fsPath: folder } }],
    getWorkspaceFolder: (uri) => (uri.fsPath.startsWith(folder) ? {} : undefined),
  },
  commands: { executeCommand: async (id, ...args) => { ran.push([id, ...args]); } },
  Uri: { parse: (s) => { const u = new URL(s); return { scheme: u.protocol.slice(0, -1), fsPath: decodeURIComponent(u.pathname) }; } },
};
const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  return request === 'vscode' ? 'vscode' : resolve.call(this, request, ...rest);
};
require.cache.vscode = { id: 'vscode', filename: 'vscode', loaded: true, exports: stub };

const bridge = require('../bridge');

async function entry() {
  const dir = path.join(home, '.human-review', 'ide');
  for (let i = 0; i < 50; i++) {
    const f = path.join(dir, `vscode-${process.pid}.json`);
    if (fs.existsSync(f)) return { file: f, ...JSON.parse(fs.readFileSync(f, 'utf8')) };
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error('no registry entry');
}

const call = (e, route, { method = 'GET', token = e.token } = {}) =>
  fetch(`http://127.0.0.1:${e.port}${route}`, { method, headers: { 'x-relay-token': token } })
    .then(async (r) => ({ status: r.status, body: await r.json() }));

test('the bridge publishes itself, owner-only, and answers only its token', async (t) => {
  const subs = [];
  bridge.activate({ subscriptions: subs });
  t.after(() => subs.forEach((s) => s.dispose()));
  const e = await entry();

  assert.strictEqual(e.app, 'vscode');
  assert.strictEqual(fs.statSync(e.file).mode & 0o777, 0o600);
  assert.strictEqual((await call(e, '/ping', { token: 'nope' })).status, 403);

  const ping = await call(e, '/ping');
  assert.strictEqual(ping.status, 200);
  assert.strictEqual(ping.body.app, 'vscode');
  assert.strictEqual(ping.body.folder, 'shop');
  assert.deepStrictEqual(ping.body.folders.map((f) => f.path), [folder]);
  assert.strictEqual(ping.body.folders[0].realPath, fs.realpathSync(folder));

  assert.strictEqual((await call(e, '/command?id=workbench.action.reloadWindow', { method: 'POST' })).status, 400);
  assert.strictEqual((await call(e, '/command?id=workbench.action.focusWindow', { method: 'POST' })).status, 200);
  assert.deepStrictEqual(ran.at(-1), ['workbench.action.focusWindow']);

  const outside = encodeURIComponent('file:///etc');
  assert.strictEqual((await call(e, `/command?id=revealInExplorer&uri=${outside}`, { method: 'POST' })).status, 400);
  const inside = encodeURIComponent(`file://${folder}/src`);
  assert.strictEqual((await call(e, `/command?id=revealInExplorer&uri=${inside}`, { method: 'POST' })).status, 200);

  assert.strictEqual((await call(e, '/bind', { method: 'POST' })).status, 404, 'no terminal routes here');
});

test('deactivating removes the registry entry', async () => {
  const f = path.join(home, '.human-review', 'ide', `vscode-${process.pid}.json`);
  assert.ok(!fs.existsSync(f));
});
