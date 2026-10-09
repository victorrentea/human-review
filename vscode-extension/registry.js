// Where every VS Code window running this extension publishes `{port, token, pid}`, one file
// per extension host: `vscode-<pid>.json`. serve-review.py reads it to find the window that
// owns a file; the windows read it to hand a click to each other.
//
// Its own directory, not the `~/.walkie-talkie/ide/` the bridge was born in (inside
// victor-vsc): both extensions can run in the same extension host, and one pid means one
// file name — sharing a directory, each would overwrite the other's token.
const os = require('os');
const path = require('path');

const REGISTRY = path.join(os.homedir(), '.human-review', 'ide');

module.exports = { REGISTRY };
