// Human Review for VS Code: the editor end of a /human-review page.
//
//   bridge.js        the loopback listener serve-review.py calls: open a reference in the
//                    window that owns it, on the reviewed commit, as a range or a diff
//   uri-handler.js   `vscode://victorrentea.human-review/…` — the same, for a page read
//                    off disk with no server behind it
//   review-status.js the "human-review" status-bar item: is this checkout's review served?
const bridge = require('./bridge');
const uriHandler = require('./uri-handler');
const reviewStatus = require('./review-status');

function activate(context) {
  bridge.activate(context);
  uriHandler.register(context);
  reviewStatus.register(context);
}

function deactivate() {
  bridge.deactivate();
}

module.exports = { activate, deactivate };
