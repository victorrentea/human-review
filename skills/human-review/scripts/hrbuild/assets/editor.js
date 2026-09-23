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
  window.HR.onready(function (caps) { SERVED = !!caps; });

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
    toast.classList.add('shown');
    clearTimeout(flash.timer);
    if (sticky) return;
    flash.timer = setTimeout(function () { toast.classList.remove('shown'); }, 2600);
  }

  // Published for the same reason `copy` is: the page has ONE toast, and a second script
  // that wanted one grew its own way of saying "copied" instead. TRACE_JS did exactly
  // that \u2014 it swapped the 📺's `data-tip` for two seconds, which changes an attribute
  // the tooltip had already rendered, so the only copy control on the page that said
  // nothing at all was the one that had written itself a message. Declared here rather
  // than in SERVER_JS because the toast and its stylesheet are this file's; SERVER_JS
  // runs first and builds `window.HR`, and every consumer of this runs after EDITOR_JS.
  window.HR.flash = flash;

  // `vscode://file//abs/path.java:487:1` → the two halves the server wants.
  function parse(href) {
    var m = /^vscode:\/\/file\/*(\/[^:]*?)(?::(\d+))?(?::\d+)?$/.exec(decodeURIComponent(href));
    return m ? { path: m[1], line: m[2] || '1' } : null;
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
      flash('This copy of the report is static, so nothing in it can run. Serve the page '
        + '\u2014 the "Serve" badge at the top copies the line that does \u2014 and this '
        + 'will re-render the diagram and reload.');
      return;
    }
    // The Serve badge copies a different kind of line: not one that changes this page
    // and wants a reload, but one that starts the server and opens the page from it.
    var serve = cmd.id === 'hr-serve';
    copy(cmd.getAttribute('data-copy') || '')
      .then(function () { flash(serve
        ? 'Copied \u2014 run it in a terminal: it starts the review server and opens this page served'
        : runhere
        ? 'Copied \u2014 this copy of the report cannot run it, so run it in a terminal'
        // The glyph, on a page that may or may not have a server. "…then reload this
        // page" used to ride along here and was only ever true of one of the commands
        // this now renders: `git revert` stages a diff and changes nothing the page
        // shows. Where a reload *is* part of the job, the play glyph does it.
        : 'Copied \u2014 paste it in a terminal'); });
  });

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
  function statusline(button) {
    var box = button.closest && button.closest('.rerun, .rband, .appenv');
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
    var box = button.closest && button.closest('.rerun, .rband, .appenv');
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

  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('a[href^="vscode:"]');
    if (!link || ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
    ev.preventDefault();
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
      var aim = parse(link.getAttribute('href'));
      fetch('/__open_diff__?path=' + encodeURIComponent(dpath) + '&base=' + encodeURIComponent(base)
            + (aim ? '&line=' + encodeURIComponent(aim.line) : ''))
        .then(function (r) {
          if (!r.ok) return r.text().then(function (t) { flash(t || 'Could not open the diff'); });
        })
        .catch(function () { flash('The review server is no longer running'); });
      return;
    }
    // Not served, and at top level: the extension's URI handler is the only channel left
    // that can materialise a before-image out of git. It is emitted only where the build
    // found that extension installed, so a portable guide never carries a URL that would
    // dead-end instead of degrading.
    var duri = link.getAttribute('data-diff-uri');
    if (!SERVED && !EMBEDDED && duri) { window.location.href = duri; return; }
    var ref2 = SERVED && parse(link.getAttribute('href'));
    if (ref2) {
      fetch('/__open__?path=' + encodeURIComponent(ref2.path) + '&line=' + ref2.line)
        .then(function (r) {
          // 404 means the server would not open it — a reference outside the repository,
          // or a file that has since moved. Say so rather than leave the click silent.
          if (!r.ok) flash('Could not open ' + ref2.path.split('/').pop());
        })
        .catch(function () { flash('The review server is no longer running'); });
      return;
    }
    if (!EMBEDDED) {
      // A diff link with no channel to open a diff through still opens the file, which is
      // the right thing — but silently, it reads as the feature being broken rather than
      // unavailable. That is exactly how this landed the first time, so it says so.
      if (base) flash('No diff channel here \u2014 opening the file at the change.');
      window.location.href = link.getAttribute('href');
      return;
    }
    // `path:line`, which is what Quick Open takes
    var ref = (link.textContent || '').trim().split('-')[0]
      || decodeURIComponent(link.getAttribute('href')).replace(/^vscode:\/\/file\/*/, '/').replace(/:\d+$/, '');
    copy(ref).then(function () { flash('Copied ' + ref + ' — paste into Quick Open (\u2318P)'); });
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
