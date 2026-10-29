// Click-to-source depends on the OS handing `vscode://` to the editor, and only a real
// browser tab can ask it to. VS Code's own Simple Browser is a webview: its iframe is
// sandboxed without `allow-top-navigation` under a `frame-src *` CSP, so it cannot launch
// an external scheme at all and the click does nothing whatever the anchor says — a
// target="_blank" does not help either, because there is no tab to open it in.
//
// **But a sandboxed iframe can still fetch its own origin.** When the guide is served by
// serve-review.py rather than opened off disk, the click becomes a request back to that
// server, which opens the file in the VS Code window that has this repository — and the
// reader lands in the class, embedded or not. That is the whole reason the guide is
// served instead of opened as a file.
//
// So there are three cases, and only the last one is a consolation prize:
//   served    → ask the server; it puts the caret in the file.
//   top level → navigate in place, handing off to the editor with no tab stranded behind.
//   embedded, unserved → copy the reference and say what to do with it, once, in a banner.
(function () {
  var EMBEDDED = window.self !== window.top;
  // False until the probe in SERVER_JS says otherwise, and never back. It used to be
  // `location.protocol === 'http:'`, which is not the same question: the demo on GitHub
  // Pages is https, so every handle on it fetched `/__open__` against github.io and
  // toasted a failure at the reader. Starting false costs the first few tens of
  // milliseconds after paint, during which a click falls through to the href — which is
  // the same thing it does on a machine with no server at all, and therefore already
  // tested.
  var SERVED = false;

  // One implementation, on HR, because a `file://` page needs the `execCommand` fallback
  // and two copies of that had already drifted apart once.
  var copy = window.HR.copy;

  var toast = null;
  // `sticky` is for a command that is still running: a docker build outlasts 2.6 seconds
  // many times over, and a progress line that fades while the thing is still going reads
  // as the thing having stopped. The next flash replaces it; a plain one after it ends
  // clears it.
  function flash(message, sticky) {
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'copy-toast';
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.remove('asks');
    toast.classList.add('shown');
    clearTimeout(flash.timer);
    if (sticky) return;
    flash.timer = setTimeout(function () { toast.classList.remove('shown'); }, 2600);
  }

  // Published for the same reason `copy` is: the page has ONE toast, and a second script
  // that wanted one grew its own way of saying "copied" instead. TRACE_JS did exactly
  // that \u2014 it swapped the 🎭's `data-tip` for two seconds, which changes an attribute
  // the tooltip had already rendered, so the only copy control on the page that said
  // nothing at all was the one that had written itself a message. Declared here rather
  // than in SERVER_JS because the toast and its stylesheet are this file's; SERVER_JS
  // runs first and builds `window.HR`, and every consumer of this runs after EDITOR_JS.
  window.HR.flash = flash;

  // A refusal that the reader can act on: the sentence, and one button that copies the
  // prompt an agent needs to make the click work. It stays until it is used or replaced —
  // a button that fades in 2.6 seconds is a button nobody manages to press.
  function offerPrompt(message, prompt) {
    flash(message, true);
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = 'Copy prompt';
    b.addEventListener('click', function () {
      copy(prompt).then(function () { flash('Copied \u2014 paste it to an agent'); });
    });
    toast.appendChild(b);
    // Sticky, so it needs its own way out: a × after the button, Esc, or a click anywhere
    // else. Only an asking toast gets them — a plain flash still fades on its own.
    var x = document.createElement('button');
    x.type = 'button';
    x.className = 'ct-close';
    x.setAttribute('aria-label', 'Close');
    x.setAttribute('data-tip', 'Close');
    x.textContent = '\u00d7';
    x.addEventListener('click', dismissAsk);
    toast.appendChild(x);
    toast.classList.add('asks');
  }
  function dismissAsk() {
    if (toast && toast.classList.contains('asks')) toast.classList.remove('shown', 'asks');
  }
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') dismissAsk(); });
  document.addEventListener('click', function (ev) {
    if (toast && toast.classList.contains('asks') && !toast.contains(ev.target)) dismissAsk();
  });

  // What the build stamped on <html>: the commit this guide quotes, the checkout it was built
  // in, and — when the editor extension was installed — the id that owns its URI handler.
  // Without a commit there is nothing to check a window against, and every path below
  // behaves as it did before the stamp existed.
  var STAMP = document.documentElement.dataset || {};
  function commitQuery() {
    return 'sha=' + encodeURIComponent(STAMP.hrHead) + '&root=' + encodeURIComponent(STAMP.hrRoot || '')
      + '&branch=' + encodeURIComponent(STAMP.hrBranch || '');
  }

  // Which IDE a click on code opens in: VS Code or IntelliJ (16 Oct 2026, Victor), or none.
  // Both run the same bridge — VS Code's extension, IntelliJ's plugin — and the server routes
  // every editor request to the one picked here (`ide=`), and to the other only when the
  // picked one has no window on the file (the answer says so, and so does the toast).
  // Picked with the two logos in the masthead (`paintChips`, 20 Oct 2026): the green one is
  // connected, at most one is, a press on red connects it and a press on green leaves none —
  // then a click on code copies its `path:line` instead. Remembered per browser; never
  // picked, none is connected and both logos are red. Off disk there
  // is no server to route, but the pick still holds: IntelliJ gets
  // `jetbrains://idea/human-review/…` links (`ideaLink`), which its plugin routes itself;
  // VS Code keeps `vscode://`.
  var IDE_KEY = 'hr-ide', IDE_NAME = {vscode: 'VS Code', idea: 'IntelliJ'};
  var connected = [];
  // Read once and kept here too, so a browser that refuses storage still keeps the pick
  // for as long as the tab is open.
  var chosen = null;
  try { chosen = localStorage.getItem(IDE_KEY); } catch (e) {}
  // Nothing connected until the reader connects one (Victor, 20 Oct 2026: "red by default").
  function ide() { return IDE_NAME[chosen] ? chosen : null; }
  function choose(app) {
    chosen = app || 'none';
    try { localStorage.setItem(IDE_KEY, chosen); } catch (e) {}
    paintIde();
  }
  function ideQuery() { return SERVED && ide() ? '&ide=' + ide() : ''; }
  // Where a click on code can reach an IDE at all: served, or off disk at top level.
  // Embedded off disk it is copied whatever the pick, and online it goes to GitHub.
  function reachable() { return SERVED || (!EMBEDDED && location.protocol === 'file:'); }
  // Off disk, at top level, with IntelliJ the pick: clicks go to IntelliJ's plugin.
  function offDiskIdea() { return !SERVED && reachable() && ide() === 'idea'; }
  // The IDE the page's words are about: the pick where a click honours it, else VS Code,
  // whose `vscode://` the links carry. Null: none connected.
  function shownIde() { return reachable() ? ide() : 'vscode'; }
  // The faces follow the pick: `.ide-word` in every "in VS Code" link, and the two marks
  // beside it, one of which `html.ide-idea` hides and both of which `html.ide-none` does
  // (review.css, genseq.css). Tooltips are reworded as tip.js shows them (`ideWords`), so a
  // tip another script sets late follows too. `hr-ide` on the document after each paint,
  // for what words itself elsewhere (folders.js).
  function paintIde() {
    var shown = shownIde();
    document.documentElement.classList.toggle('ide-idea', shown === 'idea');
    document.documentElement.classList.toggle('ide-none', !shown);
    [].forEach.call(document.querySelectorAll('.ide-word'),
                    function (w) { w.textContent = shown ? IDE_NAME[shown] : 'your IDE'; });
    paintChips();
    titleTips();
    aimAll(document);
    document.dispatchEvent(new CustomEvent('hr-ide'));
  }
  window.HR.ide = function () { return SERVED ? ide() : null; };
  // True when the reader disconnected both IDEs: nothing on the page may open one.
  window.HR.ideOff = function () { return reachable() && !ide(); };
  window.HR.ideName = function (app) { return IDE_NAME[app || shownIde()] || 'your IDE'; };
  // Not the chips' own tips: they name the IDE they are about.
  window.HR.ideWords = function (text, el) {
    var shown = shownIde(), to = shown === 'idea' ? 'IntelliJ' : shown ? null : 'your IDE';
    if (!text || !to || (el && el.closest && el.closest('.chip-ide'))) return text;
    return String(text).replace(/VS Code/g, to);
  };

  // ---- The two logos in the masthead ---------------------------------------------------
  // Connected: that IDE's logo alone, green — amber instead when the server says its window
  // is on another commit of this checkout, so only files unchanged since open. Nothing
  // connected: both logos, red, to pick from. Online they carry no colour: core.css greys
  // them like every `data-local` control and server.js answers a press.
  var chips = [].slice.call(document.querySelectorAll('.chip-ide[data-ide]'));
  var said = null;                         // the last badge poll's answer, about `ide()`
  // Picked, but the server says no window of it has this checkout: red, not green, and a
  // press opens it there rather than disconnecting (Victor, 29 Oct 2026: it was green with
  // IntelliJ on another project, and every button failed until a disconnect and reconnect).
  function away(app) { return SERVED && app === ide() && !!said && said.state === 'off'; }
  function paintChips() {
    var on = ide(), local = reachable();
    chips.forEach(function (chip) {
      var app = chip.getAttribute('data-ide'), name = IDE_NAME[app], mine = local && app === on;
      var near = mine && SERVED && said && said.state === 'near', gone = mine && away(app);
      chip.classList.toggle('ide-on', mine && !near && !gone);
      chip.classList.toggle('ide-near', !!near);
      chip.classList.toggle('ide-off', local && (!mine || gone));
      chip.hidden = local && !!on && !mine;
      chip.setAttribute('aria-pressed', mine ? 'true' : 'false');
      // One line, what a press does (Victor, 20 Oct 2026: "say click to disconnect IntelliJ,
      // the rest should not be there"). The colour already tells the state.
      var head = gone ? (said.tip || '').split('\n')[0] || 'Click to open this checkout in ' + name
        : (mine ? 'Click to disconnect ' : 'Click to connect ') + name;
      chip.setAttribute('data-tip', head);
      chip.removeAttribute('data-tip-html');
    });
  }
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      if (!reachable()) {
        flash(EMBEDDED ? 'Embedded off disk, code clicks are copied whatever the IDE'
                       : 'Works on a local copy — see Serve');
        return;
      }
      var app = chip.getAttribute('data-ide');
      if (away(app) && window.HR.connectIde) { window.HR.connectIde(app); return; }
      if (app === ide()) {
        choose(null);
        flash(IDE_NAME[app] + ' disconnected — code clicks now copy the path');
        return;
      }
      choose(app);
      if (SERVED && window.HR.connectIde) window.HR.connectIde(app);
      else flash('Code now opens in ' + IDE_NAME[app]);
    });
  });

  // ---- The href itself follows the pick ------------------------------------------------
  // Every code link is built as `vscode://file/…`, and that stays its address: kept in
  // `data-hr-code` the first time this sees it, and read through `codeHref` by everything
  // that needs the file and line (here, genseq.js, xref.js). What the `href` SAYS is the
  // picked IDE's own URL — IntelliJ's `jetbrains://` when it is the pick and a click can
  // reach it — so the status bar on hover, a middle-click and Copy Link Address all name
  // the IDE the reader chose (Victor, 29 Oct 2026: a `.dgm-src` hovered with IntelliJ
  // picked still read `vscode://`). Re-aimed on every pick (`paintIde`), and for every link
  // a script draws or re-points later (the observer below): a statement panel's handler,
  // a tooltip's file list, a test row opened for the first time.
  var XLINK_NS = 'http://www.w3.org/1999/xlink';
  var CODE_LINK = 'a[href^="vscode://file/"], a[data-hr-code]';
  function codeHref(a) {
    var h = a.getAttribute('href') || '';
    return /^vscode:\/\/file\//.test(h) ? h : (a.getAttribute('data-hr-code') || h);
  }
  window.HR.codeHref = codeHref;
  function aim(a) {
    var h = a.getAttribute('href') || '';
    // A link re-pointed by a script carries its new address; one turned into GitHub's
    // online (server.js) or into anything else is no longer this file's to aim.
    if (/^vscode:\/\/file\//.test(h)) a.setAttribute('data-hr-code', h);
    else if (h.indexOf('jetbrains://idea/human-review/') !== 0) return;
    var want = reachable() && ide() === 'idea' ? (ideaLink(a) || codeHref(a)) : codeHref(a);
    if (h === want) return;
    a.setAttribute('href', want);
    if (a.hasAttributeNS && a.hasAttributeNS(XLINK_NS, 'href')) a.setAttributeNS(XLINK_NS, 'xlink:href', want);
  }
  function aimAll(root) {
    if (root.matches && root.matches(CODE_LINK)) aim(root);
    if (root.querySelectorAll) [].forEach.call(root.querySelectorAll(CODE_LINK), aim);
  }
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        if (r.type === 'attributes') { if (r.target.matches && r.target.matches(CODE_LINK)) aim(r.target); return; }
        [].forEach.call(r.addedNodes, function (n) { if (n.nodeType === 1) aimAll(n); });
      });
    }).observe(document.documentElement, {childList: true, subtree: true, attributes: true, attributeFilter: ['href']});
  }

  // Registered only now, below every `var` that `paintIde` reads. Each asset is its own
  // script element, so off disk (and online, where nothing is probed) the answer has settled
  // before this file runs and `onready` calls back *synchronously*: registered at the top,
  // it read `connected` while still undefined, threw, and took every click handler below
  // with it — no copy button on a file:// page copied anything (19 Oct 2026).
  window.HR.onready(function (caps) { SERVED = !!caps; paintIde(); });
  // A rebuild swapped part of the page in place (`hr:swapped`, panels.js): its "in VS Code"
  // words and tips arrive as the build wrote them, and are painted for the pick again.
  document.addEventListener('hr:swapped', function () { paintIde(); });

  // Said when the server sent a click to the other IDE: the picked one has no window on it.
  function fellBack(said) {
    return said && said.fallback ? 'Opened in ' + IDE_NAME[said.ide] + ' \u2014 no '
      + IDE_NAME[ide()] + ' window has this file' : '';
  }

  // `vscode://file//abs/path.java:487:1` → the two halves the server wants.
  function parse(href) {
    var m = /^vscode:\/\/file\/*(\/[^:]*?)(?::(\d+))?(?::\d+)?$/.exec(decodeURIComponent(href));
    return m ? { path: m[1], line: m[2] || '1' } : null;
  }

  // The href can only carry a line, but the face often names a range: `VisitMapper.java:51-53`,
  // or a multi-span quote's `89,93-95`. Its last number is where the reference ends, sent as
  // `endLine` so the editor selects the whole range and fades the rest. Only when the face
  // starts on the very line the href opens at — a face that says something else is not
  // describing this link, and a single-line reference sends just its line.
  function endOf(link, line) {
    var said = [link.textContent || '', link.getAttribute('data-tip') || ''];
    for (var i = 0; i < said.length; i++) {
      var m = /:(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)\s*$/.exec(said[i].trim());
      if (!m) continue;
      var n = m[1].split(/[-,]/);
      if (n[0] === String(line) && Number(n[n.length - 1]) > Number(line)) return n[n.length - 1];
    }
    return null;
  }
  function endQuery(link, line) {
    var end = endOf(link, line);
    return end ? '&endLine=' + end : '';
  }

  // The IntelliJ plugin's twin of the VS Code extension's URI handler, for a page with no
  // server: `jetbrains://idea/human-review/<diff|review-open|open>?…`, the same queries
  // uri-handler.js reads. The plugin finds the project window that owns the file itself, so
  // the link names a path, never a window. A diff handle asks for the diff (the plugin says so
  // and opens the file when there is none); a page that knows its commit asks for the reviewed
  // version; anything else opens the file, as a range when the face names one. `jetbrains://`
  // belongs to JetBrains Toolbox, which hands it to its IntelliJ — the reason this is only
  // ever sent to a reader who picked IntelliJ.
  function ideaLink(link) {
    // The href first, then the address editor.js keeps once the href names IntelliJ
    // (`codeHref`, spelled out: this runs in tests without the rest of the file).
    var aim = parse(link.getAttribute('href') || '') || parse(link.getAttribute('data-hr-code') || '');
    if (!aim) return null;
    function url(kind, q) {
      var out = [];
      for (var k in q) {
        if (q[k] !== null && q[k] !== undefined && q[k] !== '') out.push(k + '=' + encodeURIComponent(q[k]));
      }
      return 'jetbrains://idea/human-review/' + kind + '?' + out.join('&');
    }
    var base = link.getAttribute('data-diff-base');
    if (base && link.getAttribute('data-diff-path')) {
      // `head`: a commit's own diff (a Fixed card's fix commit), not against the tree.
      return url('diff', {file: aim.path, base: base, head: link.getAttribute('data-diff-head'),
                          line: aim.line});
    }
    var end = endOf(link, aim.line);
    // A PR comment's card, as the served page sends it (`commentQuery`).
    var c = commentAim(link, aim.path, aim.line);
    var said = c ? {comment: 1, commentLine: c.at !== c.line ? c.at : '', commentUrl: c.url} : {};
    if (STAMP.hrHead) {
      return url('review-open', Object.assign({file: aim.path, line: aim.line, endLine: end, sha: STAMP.hrHead,
                                 root: STAMP.hrRoot, branch: STAMP.hrBranch}, said));
    }
    return url('open', Object.assign({file: aim.path, line: aim.line, endLine: end}, said));
  }

  // A card posted to the PR names its thread on its "on GitHub \u2197" link (`data-pr-*`).
  // A click on one of that card's references to the same file, on lines that overlap the
  // thread's, asks the editor to bring the thread up too \u2014 expanded and focused, ready
  // for Reply or Resolve \u2014 with `comment=1`, plus `commentLine` when GitHub anchored it
  // on another line than the reference starts on (a thread sits on its range's last line).
  // Any other link sends nothing, and opens exactly as it did.
  //
  // The comment's own GitHub URL rides along as `commentUrl` (the ↗'s href): IntelliJ cannot
  // expand a PR thread in the file, so its plugin lands on the lines and offers "Open on
  // GitHub" instead — and needs to know where that is.
  function commentAim(link, path, line) {
    for (var li = link.closest && link.closest('li'); li;
         li = li.parentElement && li.parentElement.closest('li')) {
      var gh = null;
      for (var i = 0; i < li.children.length; i++) {
        if (li.children[i].matches('a.f-gh[data-pr-path]')) { gh = li.children[i]; break; }
      }
      if (!gh) continue;
      var rel = gh.getAttribute('data-pr-path');
      if (path !== rel && path.slice(-(rel.length + 1)) !== '/' + rel) return null;
      var at = Number(gh.getAttribute('data-pr-line'));
      var from = Number(gh.getAttribute('data-pr-start')) || at;
      var a = Number(line), b = Number(endOf(link, line) || line);
      if (!at || b < from || a > at) return null;
      return {at: at, line: a, url: gh.getAttribute('href') || ''};
    }
    return null;
  }

  function commentQuery(link, path, line) {
    var c = commentAim(link, path, line);
    if (!c) return '';
    return '&comment=1' + (c.at !== c.line ? '&commentLine=' + c.at : '')
      + (c.url ? '&commentUrl=' + encodeURIComponent(c.url) : '');
  }

  // The one button on the page that copies something that is not a source reference:
  // the command that re-renders the hand-drawn diagram. It lives in this handler because
  // `copy` and `flash` do, and a second clipboard-and-toast implementation for one button
  // is how two of them end up behaving differently.
  //
  // Served, it runs the command instead \u2014 the same command, looked up by the id the build
  // stamped on the button. Its last stage rewrites *this file*, which is why the reload
  // is part of the action and not left to the reader: the alternative is a green tick
  // beside a picture that is still the old one, which is precisely the confusion the
  // "then reload this page" in the copy message exists to prevent.
  // server.js reads `endOf` too, to give a range its `#L<a>-L<b>` on GitHub. Published
  // here, past the helpers test_open_comment.py slices out and runs in node (no `window`).
  window.HR.endOf = endOf;
  document.addEventListener('click', function (ev) {
    var cmd = ev.target.closest &&
      ev.target.closest('button.copycmd, button.runhere');
    if (!cmd) return;
    // There used to be a third class here, `cmdpeek`, which opened a fold with the command
    // in it, and a fourth arrangement after that: a word button beside a separate glyph,
    // two elements for one action. Both are gone. What is left is one control per command
    // \u2014 its label and its mark, in the same button \u2014 which runs served and copies off
    // disk. The mark is which of the two this copy of the report can honour.
    var action = cmd.getAttribute('data-action');
    if (action && window.HR.can(action)) { rerun(cmd, action); return; }
    // Static, and the offer knows its own command: the click *copies* it. That is the one
    // thing this copy of the report can do with it, so it is what the click does — a
    // control whose whole answer is a sentence explaining why it did nothing is a control
    // the reader learns to stop pressing. The clipboard mark *on* the button is what keeps
    // that from being a magic trick — it is the visible statement that a click here copies
    // something — and its hover carries the line that goes on the clipboard.
    var runhere = cmd.classList.contains('runhere');
    if (runhere && !cmd.getAttribute('data-copy')) {
      // Nothing to copy — the only offers left in this state are the ones whose command
      // lives in a fold beside them. The line that gets the reader to served mode is
      // already on the badge in the title row, so this points at it.
      flash('Static copy. Click \uD83D\uDCCB Serve at the top to enable.');
      return;
    }
    // The Serve badge copies a different kind of line: not one that changes this page
    // and wants a reload, but one that starts the server and opens the page from it.
    var serve = cmd.id === 'hr-serve';
    if (serve && location.protocol === 'file:') { serveHere(cmd); return; }
    if (serve) { flash(cmd.getAttribute('data-tip-online') || SERVE_OFF); return; }
    copyCommand(cmd, serve, runhere);
  });

  function copyCommand(cmd, serve, runhere, note) {
    copy(cmd.getAttribute('data-copy') || '')
      .then(function () { flash(note || cmd.getAttribute('data-say') || (serve
        ? 'Copied \u2014 run it in a terminal: it starts the review server and opens this page served'
        : runhere
        ? 'Copied \u2014 this copy of the report cannot run it, so run it in a terminal'
        // The glyph, on a page that may or may not have a server. "…then reload this
        // page" used to ride along here and was only ever true of some of the commands
        // this renders. Where a reload *is* part of the job, the play glyph does it.
        : 'Copied — paste it in a terminal')); });
  }

  // Serve, pressed on the page read off disk: ask the launcher on this Mac
  // (`serve-launcher.py`, installed by `install-serve-launcher.sh`) to start the review
  // server on this page's directory, then become the served page — this tab, not a new
  // one. Copying the command was the whole feature before, and it is still what happens
  // where no launcher answers: a refused connection is how "not installed" is spelt.
  // Asked on the click and never on load, so a reader of the zip on another machine does
  // not get a red "connection refused" line in the console for merely opening the page.
  // The command goes on the clipboard first, on every press and before the launcher is
  // asked: a launcher that hangs or fails leaves the reader holding the line already, not
  // waiting 30 s to be handed it (16 Oct 2026, Victor). Copied here, inside the click,
  // because the clipboard is only writable while the gesture is still live.
  var LAUNCHER = 'http://127.0.0.1:7653';
  function serveHere(cmd) {
    if (cmd.classList.contains('starting')) return;
    cmd.classList.add('starting');
    copy(cmd.getAttribute('data-copy') || '');
    flash('Starting the review server… (command copied, in case it does not)', true);
    fetch(LAUNCHER + '/serve', {
      method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({page: decodeURIComponent(location.pathname)})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || !j.url) {
          var e = new Error(j.error || 'the launcher refused');
          e.said = true;
          throw e;
        }
        flash('Served — reloading this tab from ' + j.url.split('/')[2], true);
        location.replace(j.url + location.hash);
      });
    }).catch(function (e) {
      cmd.classList.remove('starting');
      // The launcher answered and the server did not start: say why. The line is already
      // on the clipboard, and in a terminal it prints the same error where it reads whole.
      flash(e.said
        ? 'Could not start it (' + e.message + ') — the command is copied, run it in a terminal'
        : 'Copied — run it in a terminal. To make this button start it itself, run '
          + 'scripts/install-serve-launcher.sh once');
    });
  }

  // The chip's hover says what a click does on *this* copy: off disk, start the server and
  // reload from it. Anywhere else (GitHub Pages, the zip under another server) it is greyed
  // out and says why: the command it would copy names folders on the Mac that built the
  // page, and nothing on the reader's machine can be asked from there (12 Oct 2026, Victor).
  var SERVE_OFF = 'Serve works only on a report opened from disk (file://): it starts the '
    + 'review server on the folder this page sits in. This copy is served from '
    + (location.host || 'elsewhere') + ', so there is nothing here for it to start.';
  (function () {
    var chip = document.getElementById('hr-serve');
    if (!chip) return;
    if (location.protocol !== 'file:') {
      chip.classList.add('off');
      chip.setAttribute('aria-disabled', 'true');
      // What a reader of the published copy does to see the buttons work, built from the
      // repositories this page is about (`data-tip-online`).
      chip.setAttribute('data-tip', chip.getAttribute('data-tip-online') || SERVE_OFF);
      return;
    }
    chip.classList.add('launches');
    chip.setAttribute('data-tip', 'Static copy: buttons copy commands. Click to start the '
      + 'review server and reload this tab from it, so buttons run their action instead of '
      + 'copying a command. Also copies the terminal command, to run yourself if the '
      + 'launcher (scripts/install-serve-launcher.sh) fails.');
  })();

  // Where a running command says what it is doing: a line under the control that started
  // it, if the block offering that control put one there.
  //
  // This exists because of one complaint, and it is the right complaint. `Update the
  // report` re-renders a diagram and then rebuilds the whole page — seconds, during which
  // the page said nothing whatever. A reader who presses a control and gets no sign does
  // not wait patiently: they press it again, and then they stop believing the page. The
  // toast said it, but a toast is at the foot of the window, away from the thing pressed,
  // and it fades.
  //
  // The line is the command's own last line, polled from `/__run_status__` — what is
  // actually happening, not a script's guess at what stage it has reached.
  // `.diagram` last: the draw.io card's buttons sit in its header, outside `.rerun`, and
  // the card is the box that holds both them and the status line under the header.
  function statusline(button) {
    var box = button.closest && button.closest('.rerun, .rband, .appenv, .diagram');
    return box ? box.querySelector('.runstatus') : null;
  }

  // `[review] ` is what `build-review-html.py` prefixes every line it prints with, and it
  // is the last stage of every command declared under a diagram — so the moment a line
  // wearing it appears, the diagram is done and the page is being rebuilt. A marker rather
  // than a guess at elapsed time, and the only one needed: everything before it is the
  // producer the offer named in `data-run-say`.
  var REBUILDING = /^\[review\]/;

  // Two parts, and they answer two different questions. The **phase** is what a reader
  // wants at a glance — is this still going, and roughly where is it — and it is three
  // words that do not move. The **tail** is the command's own last line, which is what
  // they want when it takes longer than they expected or stops; it is clipped to one line,
  // because a build prints two-hundred-character sentences about dropped diff links and a
  // status line that reflows the page under the reader is its own small chaos.
  function say(status, phase, tail, done) {
    if (!status) return;
    status.classList.toggle('done', !!done);
    var p = status.querySelector('.rs-phase'), s = status.querySelector('.rs-tail');
    if (p) { p.textContent = phase || ''; s.textContent = tail || ''; }
    else status.textContent = phase || tail || '';
    status.hidden = false;
  }

  function rerun(button, action) {
    var last = '';
    // Every runnable control on this page is now one button carrying its own mark — the
    // words and the glyph are the same element, because two elements for one action is a
    // question the reader has to answer before pressing either. So there is no longer a
    // wordless variant to special-case, and no variant whose face can be rewritten: the
    // mark spins in place and the sentence goes to the status line and the toast.
    // 'Running\u2026' over `Update the report` would reflow the row it sits in and take the
    // label away from the one control that says what is running.
    var status = statusline(button);
    // Two offers under the same picture run through here, and "Re-rendering the diagram"
    // over a click that has just thrown the layout away would be the page describing the
    // wrong half of what it is doing.
    var opening = button.getAttribute('data-run-say') || 'Re-rendering the diagram\u2026';
    button.disabled = true;
    button.classList.add('running');
    // Every offer in the same block goes down with it: they run one command over one
    // working tree, and a second press while the first is going is a reader who could not
    // tell it had started. Both faces of each offer, because the probe decides which one is
    // up and a disabled run glyph beside a live clipboard for the same command is the pair
    // this page spent two commits getting rid of.
    var box = button.closest && button.closest('.rerun, .rband, .appenv, .diagram');
    var kin = box ? [].slice.call(box.querySelectorAll('.cmd-run, .cmd-copy')) : [];
    kin.forEach(function (b) { b.disabled = true; });
    say(status, opening, '');
    flash(opening, true);
    window.HR.run(action, {}, function (snap) {
      var line = window.HR.tail(snap);
      // Only on change: the poll is every 700ms and a quiet command would otherwise
      // repaint the same sentence eighty times while nothing happened.
      if (line && line !== last) {
        last = line;
        say(status, REBUILDING.test(line) ? 'Rebuilding the page\u2026' : opening, line);
        flash(line, true);
      }
    }).then(function (done) {
      if (done.state === 'done') {
        button.classList.remove('running');
        say(status, 'Done \u2014 reloading this page', '', true);
        flash('Rebuilt \u2014 reloading this page', true);
        // Which tab and how far down, kept across the reload — the same place-keeper the
        // masthead's Rerun uses, because landing at the top of the first tab after
        // pressing a button three screens into the fifth one is its own small betrayal.
        window.HR.keepPlace();
        // A beat, so the sentence is readable before the page goes. `reload()` and not a
        // cache-busting navigation: the server sends no-store for exactly this.
        setTimeout(function () { location.reload(); }, 800);
        return;
      }
      restore();
      var why = window.HR.tail(done) || ('The command exited ' + done.exit);
      say(status, 'It stopped \u2014 exit ' + done.exit, why, true);
      flash(why);
    }).catch(function (e) {
      restore();
      var why = e.message || 'The review server is no longer running';
      say(status, 'It could not be run', why, true);
      flash(why);
    });

    function restore() {
      button.disabled = false;
      button.classList.remove('running');
      kin.forEach(function (b) { b.disabled = false; });
    }
  }

  // THE one way a click reaches source code (Victor, 29 Oct 2026: "it should never happen
  // again"). Every code link on the page is an `<a href="vscode://file/…">` — HTML or SVG,
  // drawn by the build or by a script — and this listener on `document` turns its click into
  // whatever the IDE picked in the masthead takes: a request to the server with `ide=`,
  // IntelliJ's `jetbrains://` off disk, VS Code's own handler, or a copied `path:line`. The
  // href is only the address. A script that opens code without a real click on such a link
  // — a panel that keeps its clicks to itself, a handle whose target it computed — hands it
  // to `HR.openCode` (below) instead of `location.href`, which is how the Sequence tab's
  // statement panel opened VS Code with IntelliJ picked. test_editor_links.py fails a page,
  // or a script, that navigates to an editor any other way.
  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('a[href^="vscode:"], a[data-hr-code]');
    if (!link || ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
    ev.preventDefault();
    openCode(link);
  });
  // `link`: the anchor, or a bare `vscode://file/…` href for a target no anchor carries.
  function openCode(link) {
    if (typeof link === 'string') {
      var made = document.createElement('a');
      made.setAttribute('href', link);
      link = made;
    }
    // Online the references are GitHub's or greyed (server.js); one handed over by a script
    // gets the same answer the greyed ones give.
    if (document.documentElement.classList.contains('hr-online')) {
      flash('Works on a local copy \u2014 see Serve');
      return;
    }
    // Both logos red: the reader disconnected the IDEs, so nothing opens one. The reference
    // is copied instead, and the toast says where to connect one.
    if (window.HR.ideOff()) {
      var r0 = refOf(link);
      copy(r0).then(function () { flash('Copied ' + r0 + ' — no IDE connected (logos at the top)'); });
      return;
    }
    // A fix is a change, so the reference to it opens as a change. Only the served path
    // can do that — the diff needs the *before* file materialised out of git, which is
    // work no page can do for itself. Everywhere else this link keeps the href it was
    // given, which is the same `vscode://file/…` every other reference carries, so the
    // worst case is today's behaviour (the file, at the first differing line) and never
    // a dead custom URL on a machine without the helper.
    var base = link.getAttribute('data-diff-base');
    var dpath = link.getAttribute('data-diff-path');
    if (SERVED && base && dpath) {
      // The line travels too. The href already carries it — it is the same line the face
      // beside this handle names — and a diff that opens scrolled to the top of a
      // five-hundred-line class leaves the reader hunting for the four lines the box is
      // about, which is the hunt this button exists to end.
      var at = parse(codeHref(link));
      // `data-diff-head`: the diff is a commit's own — `base` its parent, `head` the commit
      // (a Fixed card's fix commit, Victor 30 Oct 2026) — not `base` against the tree.
      var head = link.getAttribute('data-diff-head');
      fetch('/__open_diff__?path=' + encodeURIComponent(dpath) + '&base=' + encodeURIComponent(base)
            + (head ? '&head=' + encodeURIComponent(head) : '')
            + (at ? '&line=' + encodeURIComponent(at.line) : '') + ideQuery())
        .then(function (r) {
          if (!r.ok) return r.text().then(function (t) { flash(t || 'Could not open the diff'); });
          if (r.status === 200) return r.json().then(function (j) { if (fellBack(j)) flash(fellBack(j)); });
        })
        .catch(function () { flash('The review server is no longer running'); });
      return;
    }
    // Off disk, IntelliJ picked: every reference goes to its plugin (`ideaLink`), a diff
    // handle as a diff, a stamped page through the reviewed-version check.
    var jb = offDiskIdea() && ideaLink(link);
    if (jb) { window.location.href = jb; return; }
    // Not served, and at top level: the extension's URI handler is the only channel left
    // that can materialise a before-image out of git. It is emitted only where the build
    // found that extension installed, so a portable guide never carries a URL that would
    // dead-end instead of degrading.
    var duri = link.getAttribute('data-diff-uri');
    if (!SERVED && !EMBEDDED && duri) { window.location.href = duri; return; }
    // Off disk, at top level, with the extension: its handler finds the window holding the
    // reviewed version of the file, and says so in VS Code when none does.
    var aimed = !SERVED && !EMBEDDED && !base && STAMP.hrHead && STAMP.hrOpenUri
      && parse(codeHref(link));
    if (aimed) {
      window.location.href = 'vscode://' + STAMP.hrOpenUri + '/review-open?file='
        + encodeURIComponent(aimed.path) + '&line=' + aimed.line + endQuery(link, aimed.line)
        + '&' + commitQuery();
      return;
    }
    var ref2 = SERVED && parse(codeHref(link));
    if (ref2) {
      fetch('/__open__?path=' + encodeURIComponent(ref2.path) + '&line=' + ref2.line
            + endQuery(link, ref2.line) + commentQuery(link, ref2.path, ref2.line)
            + (STAMP.hrHead ? '&' + commitQuery() : '') + ideQuery())
        .then(function (r) {
          if (r.status === 204) return;
          return r.text().then(function (t) {
            var said = null;
            try { said = JSON.parse(t); } catch (e) { /* a bare 404 has no body */ }
            if (r.ok) {
              if (fellBack(said)) flash(fellBack(said));
              else if (said && said.edited) flash('Opened \u2014 edited since the reviewed commit');
              return;
            }
            if (said && said.prompt) { offerPrompt(said.message, said.prompt); return; }
            // 404 means the server would not open it — a reference outside the repository,
            // or a file that has since moved. Say so rather than leave the click silent.
            flash((said && said.message) || ('Could not open ' + ref2.path.split('/').pop()));
          });
        })
        .catch(function () { flash('The review server is no longer running'); });
      return;
    }
    if (!EMBEDDED) {
      // A diff link with no channel to open a diff through still opens the file, which is
      // the right thing — but silently, it reads as the feature being broken rather than
      // unavailable. That is exactly how this landed the first time, so it says so.
      if (base) flash('No diff channel here \u2014 opening the file at the change.');
      window.location.href = codeHref(link);
      return;
    }
    var ref = refOf(link);
    copy(ref).then(function () { flash('Copied ' + ref + ' — paste into Quick Open (\u2318P)'); });
  }
  window.HR.openCode = openCode;
  // A finding's title opens its first reference in the picked IDE (Victor, 20 Oct 2026):
  // the card's IDE link when it has one, else the first source reference in its body.
  // The click is handed to that link, so it goes wherever a click on the link would —
  // online, to GitHub, where server.js pointed the references. A drag that selected
  // text is a selection, not a click.
  function titleAim(title, online) {
    var li = title.closest && title.closest('ol.findings > li');
    if (!li) return null;
    // Online the IDE link is hidden and inert: the body's reference, now GitHub's, instead.
    // Asked at the click: a served page is `hr-online` until its probe answers.
    return (!online && li.querySelector(':scope > a.f-vsc'))
      || li.querySelector('a.srcref[href^="vscode:"], a.srcref[data-hr-code], a.srcref[href^="http"]');
  }
  function markTitles(root) {
    [].forEach.call(root.querySelectorAll('ol.findings > li > .f-title'), function (t) {
      if (!titleAim(t)) return;
      t.classList.add('opens');
    });
  }
  markTitles(document);
  // Its tip, worded for what the click will do now (`paintIde` calls it on every change).
  function titleTips() {
    var on = ide();
    var say = !reachable() ? (EMBEDDED ? 'Copy the path' : 'Open on GitHub')
      : on ? 'Open in ' + IDE_NAME[on] : 'Copy the path (no IDE connected)';
    [].forEach.call(document.querySelectorAll('.f-title.opens'),
                    function (t) { t.setAttribute('data-tip', say); });
  }
  titleTips();
  // And the Review tab swapped in by a rebuild (`hr:swapped`, panels.js): its titles arrive
  // unmarked, so they are marked and worded again.
  document.addEventListener('hr:swapped', function (ev) {
    if (!ev.detail || !ev.detail.querySelectorAll) return;
    markTitles(ev.detail);
    titleTips();
  });
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest && ev.target.closest('.f-title.opens');
    if (!t || ev.target.closest('a') || ev.button !== 0) return;
    var sel = window.getSelection && String(window.getSelection());
    if (sel) return;
    var to = titleAim(t, document.documentElement.classList.contains('hr-online'));
    if (to) to.click();
  });
  // `path:line`, which is what Quick Open takes
  function refOf(link) {
    return (link.textContent || '').trim().split('-')[0]
      || decodeURIComponent(codeHref(link)).replace(/^vscode:\/\/file\/*/, '/').replace(/:\d+$/, '');
  }

  // The connected logo, served: is that IDE's window on this checkout, and is the checkout
  // on the reviewed commit? Asked again every few seconds while the tab is visible, because
  // the answer changes under the page — a window opened, a branch switched, a commit made.
  // The answer also lists every IDE with a window here (`ides`), which words the red logo's
  // tip and picks the IDE for a reader who never picked one.
  window.HR.onready(function (caps) {
    if (!caps || !chips.length || !STAMP.hrHead) return;
    function ask() {
      if (document.hidden) return Promise.resolve(said);
      var asked = ide();
      return fetch('/__editor__?' + commitQuery() + ideQuery(), {cache: 'no-store'})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (!j) return said;
          connected = j.ides || [];
          // An IDE came or went and the default moved with it: this answer is about the
          // other one, so ask again rather than colour a logo for an IDE no longer picked.
          if (ide() !== asked) { said = null; paintIde(); setTimeout(ask, 0); return j; }
          said = asked ? j : null;
          paintIde();
          return j;
        })
        .catch(function () { return null; });
    }
    // A press on a red logo, once the pick has moved: bring forward the window that has this
    // checkout, or open the IDE on it — the server's own folder, not one this page names.
    // Nothing is checked out; when the checkout is on another branch or commit, the press
    // ends in the prompt below instead.
    window.HR.connectIde = function (app) {
      var name = IDE_NAME[app];
      var opened = fetch('/__editor_open__', {
        method: 'POST', cache: 'no-store',
        headers: {'Content-Type': 'application/json',
                  'X-Human-Review-Token': caps.token || ''},
        body: JSON.stringify({ide: app})
      }).then(function (r) {
        return r.ok ? r.json().then(function (j) {
          flash(name + ' connected — ' + (j.how === 'focused'
            ? 'bringing window ' + (j.window || '') + ' to the front' : 'opening it on ' + j.opened));
          return true;
        }) : r.text().then(function (t) { flash(t || 'the review server refused'); return false; });
      }).catch(function () { flash('the review server is no longer running'); return false; });
      // Asked afresh, not read off the last poll: a branch switched two seconds ago is
      // exactly the case this prompt is for.
      said = null;
      Promise.all([opened, ask()]).then(function (both) {
        if (both[0] && both[1] && both[1].prompt && ide() === app) offerCheckout(both[1], app);
      });
      setTimeout(ask, 3000);
    };
    // The checkout is not on the reviewed commit: say so under the logo, with the prompt
    // that would fix it and one 📋 that copies it. Words only — neither the server nor the
    // editor checks anything out — and it stays until it is copied or dismissed, because a
    // button that fades in 2.6 seconds is a button nobody manages to press.
    var pop = null;
    function closePop() { if (pop) { pop.remove(); pop = null; } }
    function offerCheckout(j, app) {
      var chip = document.getElementById('hr-ide-' + app);
      closePop();
      pop = document.createElement('div');
      pop.id = 'hr-vsc-pop';
      pop.setAttribute('role', 'dialog');
      var say = document.createElement('p');
      say.textContent = (j.note || 'This checkout is not on the reviewed commit.')
        + ' Paste this to a coding agent:';
      var code = document.createElement('code');
      code.textContent = j.prompt;
      var row = document.createElement('div');
      row.className = 'vp-row';
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'vp-copy';
      b.textContent = '\ud83d\udccb Copy prompt';
      b.addEventListener('click', function () {
        copy(j.prompt).then(function () { flash('Copied \u2014 paste it to an agent'); closePop(); });
      });
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'vp-close';
      x.setAttribute('aria-label', 'Close');
      x.setAttribute('data-tip', 'Close');
      x.textContent = '\u00d7';
      x.addEventListener('click', closePop);
      row.appendChild(b);
      row.appendChild(x);
      pop.appendChild(say);
      pop.appendChild(code);
      pop.appendChild(row);
      document.body.appendChild(pop);
      // The logo's own tip is still up under the pointer that pressed it, and it opens
      // below a chip this close to the top, i.e. over this. tip.js hides on focusout.
      chip.dispatchEvent(new FocusEvent('focusout', {bubbles: true}));
      // Under the logo, kept on screen: the masthead row can sit anywhere across the width.
      var r = chip.getBoundingClientRect();
      pop.style.top = Math.round(r.bottom + 6) + 'px';
      pop.style.left = Math.max(8, Math.min(Math.round(r.left),
        window.innerWidth - pop.offsetWidth - 8)) + 'px';
    }
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') closePop(); });
    document.addEventListener('click', function (ev) {
      if (pop && !pop.contains(ev.target) && !(ev.target.closest && ev.target.closest('.chip-ide'))) {
        closePop();
      }
    });
    ask();
    setInterval(ask, 5000);
    document.addEventListener('visibilitychange', ask);
  });

  // The path in the footer: plain text off disk, a Finder link where the server says it
  // can `open -R`. The page names which of the served files it is — its own URL path — and
  // the server refuses anything outside the directory it serves.
  window.HR.onready(function (caps) {
    var path = document.getElementById('hr-diskpath');
    if (!caps || !caps.reveal || !path) return;
    path.classList.add('reveals');
    path.setAttribute('role', 'button');
    path.tabIndex = 0;
    path.setAttribute('data-tip', 'Show this file in Finder');
    function reveal() {
      fetch('/__reveal__', {
        method: 'POST', cache: 'no-store',
        headers: {'Content-Type': 'application/json',
                  'X-Human-Review-Token': caps.token || ''},
        body: JSON.stringify({page: decodeURIComponent(location.pathname)})
      }).then(function (r) {
        return r.ok ? flash('Showing it in Finder')
                    : r.text().then(function (t) { flash(t || 'the review server refused'); });
      }).catch(function () { flash('the review server is no longer running'); });
    }
    path.addEventListener('click', reveal);
    path.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); reveal(); }
    });
  });

  // The banner is the consolation prize, so it must not be printed until we know there
  // is nothing better on offer — which is now something we learn after the page has
  // painted rather than from the URL. Waiting for the probe also means it is never shown
  // and then withdrawn, which would be a paragraph of apology flashing past for no reason.
  if (!EMBEDDED) return;
  window.HR.onready(function (caps) {
    if (caps) return;
    var note = document.createElement('p');
    note.className = 'embedded-note';
    note.innerHTML = 'You are reading this inside an embedded browser, opened straight off '
      + 'disk, so the links cannot reach the editor. Clicking a <code>path:line</code> '
      + 'copies it instead. Serve the guide with <code>serve-review.py</code> and they open '
      + 'the file for real.';
    var body = document.querySelector('.wrap') || document.body;
    body.insertBefore(note, body.firstChild);
  });
})();
