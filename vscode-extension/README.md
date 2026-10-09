# Human Review for VS Code

The editor end of a [/human-review](https://github.com/victorrentea/human-review) page. A
review page is full of `File.java:49-51` references; with this extension installed, a click on
one lands you in the right place:

- **In the window that has the reviewed checkout open** — not whichever VS Code window was
  last active. With two checkouts of the same project open, the click goes to the one whose
  workspace folder contains the file (longest path wins).
- **Only on the reviewed version of the file.** A window qualifies when the file at its HEAD
  is the blob the review quoted. When no open window has it, you get a plain sentence and a
  prompt you can paste to a coding agent — never a silently wrong file.
- **As a range**: the quoted lines selected and highlighted, the rest of the file faded until
  you click or type.
- **As a diff** for an applied fix: the reviewed base on the left, the working tree on the right.
- **On the PR comment thread** for a card that was posted to the pull request (with the
  GitHub Pull Requests extension), expanded and focused, ready for Reply or Resolve.

A **`human-review`** item in the status bar says whether this checkout's review is being
served: green (served, on the reviewed commit), amber (served, checkout on another commit),
grey (a review on disk, no server — click to start one and open the page).

## How it connects

Each VS Code window listens on a random loopback port and publishes `{port, token}` in a
`0600` file under `~/.human-review/ide/`. The review server (`serve-review.py`, started by the
skill) reads those files, asks each window which folders it has open, and sends the click to
the owner. Only `127.0.0.1`, only callers holding the token, and only a short allowlist of
actions: open a file, open a diff, reveal a folder, raise the window, show the review page in
VS Code's Simple Browser.

A page opened straight off disk (no server) reaches the extension through
`vscode://victorrentea.human-review/…` links instead.

## Install

From the Marketplace: search **Human Review** (publisher `victorrentea`), or

```sh
code --install-extension victorrentea.human-review
```

From the repository, without the Marketplace — the packaged extension is committed next to
its source:

```sh
code --install-extension vscode-extension/dist/human-review.vsix
```

## Settings

- `humanReview.serveScript` — absolute path to `serve-review.py`, for the status-bar item to
  start a server with. Empty: the copy installed with the `human-review` Claude Code plugin,
  else `~/workspace/human-review`.

## Develop

```sh
cd vscode-extension
npm test                                   # plain node, no VS Code host
npx --yes @vscode/vsce package --no-dependencies -o dist/human-review.vsix
```
