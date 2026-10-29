# Human Review for IntelliJ

The IntelliJ end of a [/human-review](https://github.com/victorrentea/human-review) page — the
twin of [`vscode-extension/`](../vscode-extension), speaking the same wire protocol, so
`serve-review.py` can hand a click to either editor.

- **A `File.java:49-51` reference opens in the project window that owns the file** (longest
  path wins across every open project), as a range: the lines selected and highlighted, the
  rest of the file muted until you click, move the caret or type. The window comes to the front.
- **A fix opens as a diff**: the file at the review's base commit on the left (read-only), the
  working tree on the right (editable), scrolled to the line the fix is about, in the project
  that owns the file. A ref git cannot resolve, a file unchanged since, a path no project owns:
  refused in a sentence the page shows, never a diff with an invented left half.
- **The reviewed version, or a plain no**: a reference on a page that carries its commit opens
  only where the file is the very blob that was reviewed — a project here, or another IntelliJ
  or VS Code window running the bridge (it is handed over). Edited since: an amber note on the
  line. Nowhere: a refusal with a prompt to copy for a coding agent.
- **Pages read off disk** reach the plugin through `jetbrains://idea/human-review/…` links
  (`open`, `diff`, `review-open`, the query of the VS Code extension's URI handler, and
  `open-pr?url=…&root=…`), which the
  page sends when the reader picked IntelliJ in the masthead earlier. `jetbrains://` is owned by
  JetBrains Toolbox, which hands it to the IntelliJ it manages: without Toolbox those clicks go
  nowhere.
- **A `human-review` item in the status bar** says whether this checkout's review is being
  served: green (served, on the reviewed commit), amber (served, checkout on another commit),
  grey (a review on disk, no server — click to start one and open the page). Hidden when the
  project has no `.human-review/review.html`.
- **The page beside the code**, in a *Human Review* tool window on the right — an embedded
  Chromium (JCEF). `serve-review.py` started from IntelliJ's terminal shows a new review there
  (`/open-url`), and a click on the status-bar item opens it there too. No stripe button until a
  page has been shown. An IDE without JCEF uses the OS browser (`/open-url` answers `501`).
- **PR comments**: a card the review posted to the pull request opens on its lines, then a
  notification says *This card is a PR comment* with an **Open on GitHub** action for its thread —
  opened only when you click it. (VS Code expands the thread in the editor; IntelliJ has no public
  API for that.) The bridge answers `comment: "notified"`, or `"none"` when no GitHub URL came with
  the click; `/editor-state` reports the last one as `lastComment`.
- **GitHub pull requests**, in the bundled GitHub plugin's *Pull Requests* tool window (nothing
  of that plugin is linked against: its view models are internal, so `PullRequests.kt` finds the
  tool window by id, the tree and the status block by shape and name, and degrades to a no-op):
  - **Follow the editor**: switching the editor to a file of the open PR selects it in the PR's
    changes tree and scrolls there. On by default; right-click the tree → *Follow the Editor*.
  - **A foldable status block**: double-click the checks/reviewers block under the tree to fold
    it to its first line ("✓ All required checks have passed") and give the tree the room; again
    to unfold. Remembered; also right-click the tree → *Fold the Status Block*.
  - **Open a PR there**: the review page's *in IntelliJ* beside a review posted to the PR opens it
    in that tool window, its tab selected and its timeline open (`/open-pr`, or the `open-pr`
    link off disk) — in the window that has the checkout, after checking the tool window is on
    the PR's repository.
- **Installs and updates without a restart** — a dynamic plugin (below).

## How it connects

The IDE listens on one random loopback port **per process** (IntelliJ hosts every project
window in one JVM, unlike VS Code's one extension host per window) and publishes
`{"app":"idea", port, token, pid}` in a `0600` file `~/.human-review/ide/idea-<pid>.json`,
deleted on exit and on unload. Only `127.0.0.1`, only callers sending the token in
`x-relay-token`, and only a short allowlist: `/ping` (with the plugin's `version`),
`/editor-state`, `/open-file`, `/open-diff`, `/review-open`, `/open-url` (`{url, root?,
preserveFocus?}`; http(s) only), `/open-pr` (`{url, root?}`: a `https://github.com/…/pull/N`;
`409` with a sentence when the GitHub plugin is off, not connected, or on another repository),
and `/command` with `workbench.action.focusWindow`, `humanReview.status`, `revealInExplorer` or
`humanReview.openDatabase`.

## Install

Build, then **Settings → Plugins → ⚙ → Install Plugin from Disk…** →
`intellij-plugin/build/distributions/human-review-0.3.0.zip`. No restart: the plugin loads into
the running IDE.

```sh
cd intellij-plugin
./gradlew buildPlugin          # → build/distributions/human-review-0.3.0.zip
```

**`jetbrains://` links need JetBrains Toolbox.** A page read off disk (no review server) reaches
the plugin through `jetbrains://idea/human-review/…` links. macOS hands the `jetbrains://` scheme
to Toolbox, which passes it to the IntelliJ it manages; an IntelliJ installed without Toolbox
registers only `idea://`, and those clicks go nowhere. Served pages do not need it.

## Updates without a restart

The plugin is dynamic — every extension point it uses can be loaded and unloaded live, it has
no components, and `verifyPlugin` fails the build on anything that is not (`NOT_DYNAMIC`). On
unload it closes its port, deletes `idea-<pid>.json`, and takes its highlights, the edited mark
and its notification back out of the editors (`Lifecycle.kt`); a plugin loaded into a running IDE
starts its bridge for the projects already open.

**Nothing that builds UI is called from the plugin's own stack** (`OffStack.kt`). Every toolbar
keeps a `Throwable` of where it was created, and a Throwable keeps the class of every frame: an
editor opened from a lambda of ours held the old build's classloader for as long as it stayed
open, and the 30 Oct 2026 reload failed with "not unload-safe". Editors, diffs, tool-window
activations and the Pull Requests tree's selection are handed to the EDT as JDK-owned Runnables
(a method handle wrapped by `MethodHandleProxies`), so no frame of ours is under them.

- **Colleagues** get new versions from the Marketplace like any plugin: *Settings → Plugins →
  Updates → Update*, applied live without a restart prompt; or tick *Update plugins
  automatically* (Settings → Appearance & Behavior → System Settings → Updates) and they arrive
  by themselves.
- **Developing it**: `./deploy-local.sh` rebuilds and swaps the jar into the running IntelliJ,
  which reloads it in a second or two. It needs `-Didea.auto.reload.plugins=true` in
  *Help → Edit Custom VM Options…* (the IDE then reloads a plugin whose jar *content* changes),
  and the plugin installed from disk once. It keeps the installed jar's file name (a new name is
  ignored), stages the copy outside `lib/` and renames it into place (never an overwrite: the
  loaded classloader still reads the old file), then watches `idea.log`: ✅ on
  `- load plugin 'Human Review'`, then 30 s more for IntelliJ's verdict on the build it replaced
  — since 2026.2 that comes *after* the load, silent when clean, and exit 3 on "expected to
  unload but are still referenced" (restart needed). The reload lands only once IntelliJ's window
  gets focus; when it has not within `RELOAD_WAIT` seconds (60), `./deploy-local.sh --verdict`
  waits for it again without building. `IDE_CONFIG=…` picks the IDE when several have it
  installed. **A hot reload swaps classes, not `plugin.xml`**: IntelliJ reloads the plugin with
  the descriptor it parsed at startup (`DynamicPlugins.loadPlugin(descriptor)`), so a new
  extension, listener or action declared there appears only after a restart (or an install from
  disk or the Marketplace). What must work right after a live update is wired in code — light
  services started from the startup activity and `pluginLoaded`.

## Settings

**Settings → Tools → Human Review**

- **serve-review.py**: absolute path to the script the status item starts a server with. Empty:
  the copy installed with the `human-review` Claude Code plugin, else `~/workspace/human-review`.
- **Open the review page in the Human Review tool window** (default on): where a click on the
  status-bar item opens the page. Off, or an IDE without JCEF: the OS browser.
- **Pull Requests tool window: follow the editor** (default on) and **fold the status block**
  (default off) — the same two toggles as in the changes tree's right-click menu.

## Develop

```sh
./gradlew test                 # the pure parts, plus a headless IDE (HeadlessIdeTest): the bridge's
                               # routes against a project with a git repo, the diff viewer and the
                               # page view replaced by recorders, jetbrains:// links through the
                               # platform's own entry, the unload taking everything back out
./gradlew verifyPlugin         # against IC 2024.3 (sinceBuild) and the 2025.1 the build uses
./deploy-local.sh              # into the running IntelliJ, no restart (above)
./gradlew runIde --args=/path/to/a/checkout
```

Tests never touch `~/.human-review/ide/`: `-Dhuman-review.registry.dir` points the registry at
`build/test-registry`, a test bridge is started only by the test that wants one, and no window
is raised in a test run. JCEF is not started in a test either: a headless JVM has none, so the
`501` path is the real one, and the tool window is a recorder (`PageView`) — the browser itself is
the one part only a real IDE shows.

`runIde` passes `-Didea.trust.all.projects=true`: the "Trust this project?" modal would
otherwise hold the EDT, and every bridge route that needs it would answer 503.
