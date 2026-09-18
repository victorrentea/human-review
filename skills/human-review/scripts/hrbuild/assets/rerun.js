// The masthead's Rerun: re-derive the evidence a program can re-derive, and rebuild this
// page around it.
//
// It is the button for the loop this page is actually read in. A reviewer edits a test
// body, fixes a finding, adds a column — and until now catching the page up meant going
// back to the terminal the page was built from and remembering which of three commands
// refreshes what. Two of those three are wrong in a way nothing tells you: run the model's
// half again and the findings you are looking at are replaced by a different, equally
// fluent set at full price; run `--steps all` and you have just re-recorded the film.
//
// So this button asks for exactly one thing — `refresh-report.py --steps static` — and the
// server, not the page, decides what that means. The findings, the requirements matrix and
// the test catalogue are never touched: they are a judgement, produced once, when a human
// asks. Neither is the film: it is minutes long, it needs the application up, and it is
// the one artifact on this page whose re-recording is a decision.
//
// Hidden unless the probe says this server can honour it, like every other control here.
// A static copy has no process behind it to rebuild anything, and a button that copied a
// shell line instead would be offering the terminal round-trip this exists to remove.
//
// Since there are two of them, everything below is keyed off `data-rerun` rather than off
// an element id. The second button is the same machine with the model's half in front of
// it — `rerun-model.py`, then the same refresh with `--allow-model` — and the only thing
// that differs is the price, which is why the only thing the code below branches on is
// whether a button has a confirmation to show first. A copy of this block per button is
// how the free one and the paid one end up reporting failure differently.
(function () {
  var buttons = [].slice.call(
    document.querySelectorAll('button.chip-rerun[data-rerun]'));
  if (!buttons.length) return;
  var fail = document.getElementById('hr-rerun-fail');
  var done = document.getElementById('hr-rerun-done');

  // What `run-steps.py` prints last, which its own comment says is "phrased for the status
  // band on the served page rather than for this terminal": `N step(s) re-run, M unchanged
  // and skipped \u2014 about N s saved.` It had nowhere to go. The band beside this one
  // could only ever report a failure, so the six seconds a fast rerun takes read exactly
  // like a button that does nothing \u2014 which is the thing that sentence exists to
  // prevent.
  var SUMMARY = /^\[run-steps\] (.+)$/m;
  // Stashed across the reload this run ends in, and cleared as it is read. sessionStorage
  // rather than a query string or a global: the reload is `location.reload()` on the same
  // URL (the address bar is part of what a reader may have copied), and it is this tab's
  // news, not another tab's.
  var SAID = 'hr-rerun-said';

  function stash(snap) {
    var m = SUMMARY.exec((snap && snap.output) || '');
    if (!m) return;         // nothing was skipped: there is no fast to explain
    // Without the `--force` sentence the terminal ends on. A shell flag printed in prose
    // is the one thing this page does not do \u2014 every command it hands out lives in a
    // hover, on the control that copies it \u2014 and the reader of this band is holding a
    // mouse, not a terminal.
    var line = m[1].split('`--force`')[0].trim();
    try { sessionStorage.setItem(SAID, line); } catch (e) { /* private window */ }
  }

  // Read once, on the load the reload produced. Removed as it is read, so a reader who
  // refreshes the page five minutes later is not told about a rerun they have forgotten.
  (function sayWhatHappened() {
    if (!done) return;
    var line = null;
    try { line = sessionStorage.getItem(SAID); sessionStorage.removeItem(SAID); }
    catch (e) { return; }
    if (!line) return;
    done.querySelector('.rerundone-say').textContent = line;
    done.hidden = false;
    setTimeout(function () {
      done.classList.add('going');
      setTimeout(function () { done.hidden = true; done.classList.remove('going'); }, 500);
    }, 6000);
  })();
  // Where the reader was, restored after the reload this ends in — `HR.keepPlace`, which
  // the diagram offers and every other `reload` action call too. It used to live here, and
  // "the same scroll position, saved under the same key, restored on the same event" is
  // not a thing to keep two copies of: the second control to end in a reload would have
  // had to find this one and copy it.
  var remember = window.HR.keepPlace;

  function stop(btn, problem, snap) {
    btn.disabled = false;
    btn.classList.remove('running');
    btn.setAttribute('data-tip', btn.getAttribute('data-idle-tip') || '');
    // Both of them: the lock is shared, so while one was working the other was disabled
    // for a run it did not start, and leaving it that way would strand it.
    buttons.forEach(function (other) { other.disabled = false; });
    if (!fail) return;
    // The last lines, not the whole log: a build prints hundreds and the answer is at the
    // end of them. Shown at all because "it failed" is not actionable and this is — the
    // sentence a producer printed on its way out is usually the whole fix.
    var log = ((snap && snap.output) || '').split('\n');
    while (log.length && !log[log.length - 1].trim()) log.pop();
    fail.querySelector('.rerunfail-why').textContent =
        problem + (snap && snap.exit != null ? ' (exit ' + snap.exit + ')' : '');
    fail.querySelector('.rerunfail-log').textContent = log.slice(-14).join('\n');
    fail.hidden = false;
  }

  if (fail) {
    fail.querySelector('.rerunfail-x').addEventListener('click', function () {
      fail.hidden = true;
    });
  }

  function go(btn) {
    if (fail) fail.hidden = true;
    // And last run's summary, if it is still on screen: it is about the press before this
    // one, and leaving it up while a new run works reads as this one having finished.
    if (done) { done.hidden = true; done.classList.remove('going'); }
    // Every rerun button, not only this one: the server runs one at a time and a second
    // press on the other would join this run rather than start its own, which is correct
    // and unreadable — a reader who pressed the free button and watched the paid one's log
    // scroll past has been told the wrong thing by the page.
    buttons.forEach(function (other) { other.disabled = true; });
    // The glyph turns; nothing is written over it. These chips are two and three
    // characters wide and 'Running\u2026' in one reflowed the whole masthead the instant it
    // was pressed — and the mark that spins is the same mark the run glyphs down the page
    // already spin, so a reader who has seen one knows this one is working.
    btn.classList.add('running');
    remember();
    window.HR.run(btn.getAttribute('data-rerun'), {}, function (snap) {
      // One line, in the hover: the button has room for a word and the reader who wants
      // to know which producer it is on is the reader already pointing at it.
      var line = window.HR.tail(snap);
      btn.setAttribute('data-tip', line || 'Rebuilding this page\u2026');
    }).then(function (snap) {
      if (snap.state === 'done') {
        // What the run said about itself, kept for the other side of the reload.
        stash(snap);
        // The server holds its reload-watcher for the length of the rerun, so this is the
        // single reload of the whole run rather than one per producer.
        location.reload();
        return;
      }
      stop(btn, 'the rebuild did not finish', snap);
    }).catch(function (e) {
      // 409 is not a failure of the rebuild, it is the server saying this press started
      // nothing. It belongs back in the dialog the reader just came out of rather than in
      // the red band at the top of the page, which reads as "your run broke".
      if (e && e.status === 409 && panel) {
        stop(btn, '', null);
        if (fail) fail.hidden = true;
        panel.hidden = false;
        sayBusy({active: e.busy || {}, kind: (e.busy && e.busy.action === '__rerun_ai__')
                   ? 'rerun_ai' : 'rerun',
                 started: e.busy && e.busy.started, joined: 0});
        return;
      }
      stop(btn, e.message || 'the review server could not be reached', null);
    });
  }

  // The page's own confirmation, for the one button that spends money. Not
  // `window.confirm`: it cannot say the price in this page's voice, it cannot make the
  // safe answer the default one, and it is the dialog every reader on the internet has
  // been trained to dismiss unread — a reflex that on a native confirm costs five dollars
  // and here lands on Cancel.
  //
  // Resolved rather than returned as a boolean, because the answer arrives later than the
  // click and a caller that had to poll for it would be a second state machine.
  var panel = document.getElementById('hr-ai-confirm');
  var lastFocus = null;

  function shut() {
    if (!panel) return;
    panel.hidden = true;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  // What the panel says about a run already in flight, and what that does to the answer.
  // A paid press is never joined in silence any more — the server refuses it with 409 and
  // names the run — so the panel has to be able to show a reader the thing that is going
  // before they decide whether to want one of their own.
  function sayBusy(state) {
    var box = panel && panel.querySelector('.hrconfirm-busy');
    var yes = panel && panel.querySelector('.hrconfirm-yes');
    if (!box) return false;
    if (!state || !state.active) {
      box.hidden = true;
      box.textContent = '';
      if (yes) { yes.disabled = false; yes.textContent = 'Spend it, rerun with AI'; }
      return false;
    }
    var at = new Date((state.started || 0) * 1000);
    var clock = at.getHours() + ':' + ('0' + at.getMinutes()).slice(-2);
    box.textContent = (state.kind === 'rerun_ai' ? 'A paid run' : 'A rerun')
      + ' started at ' + clock + ' is still going'
      + (state.joined ? ' (' + state.joined + ' other press joined it)' : '')
      + '. Wait for it, then decide \u2014 it may already be doing what you want, and it may '
      + 'be building from a working tree that has moved since.';
    box.hidden = false;
    // Not merely discouraged. The press that cost eight dollars was a press this panel
    // would have allowed.
    if (yes) { yes.disabled = true; yes.textContent = 'Something is already running'; }
    return true;
  }

  function confirmSpend() {
    if (!panel) return Promise.resolve(true);
    lastFocus = document.activeElement;
    sayBusy(null);
    panel.hidden = false;
    // Asked at the moment of deciding, not at load: a run somebody started in another tab
    // three minutes ago is exactly the case this exists for.
    if (window.HR.status) {
      window.HR.status().then(sayBusy).catch(function () {});
    }
    // Cancel, not the spend: whatever a stray Return or a reflexive click lands on has to
    // be the answer that costs nothing.
    var no = panel.querySelector('.hrconfirm-no');
    if (no && no.focus) no.focus();
    return new Promise(function (resolve) {
      function done(answer) {
        panel.removeEventListener('click', onClick);
        document.removeEventListener('keydown', onKey);
        shut();
        resolve(answer);
      }
      function onClick(ev) {
        var t = ev.target;
        if (t.closest && t.closest('.hrconfirm-yes')) return done(true);
        if (t.closest && t.closest('.hrconfirm-no')) return done(false);
        // The backdrop is the panel itself; a click that never reached the box is a click
        // outside the dialog, which everywhere else on the web means "no".
        if (t === panel) return done(false);
      }
      function onKey(ev) {
        if (ev.key === 'Escape') { ev.preventDefault(); done(false); }
      }
      panel.addEventListener('click', onClick);
      document.addEventListener('keydown', onKey);
    });
  }

  buttons.forEach(function (btn) {
    // Stashed on the element so `stop` can put the button back exactly as it was without
    // a closure per button holding the strings. Only the tooltip: the face is a glyph the
    // run never replaces, so there is nothing else to put back.
    btn.setAttribute('data-idle-tip', btn.getAttribute('data-tip') || '');
    var paid = btn.getAttribute('data-rerun') === '__rerun_ai__';
    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      if (!paid) { go(btn); return; }
      confirmSpend().then(function (yes) { if (yes) go(btn); });
    });
  });

  window.HR.onready(function (caps) {
    if (!caps) return;
    // The price the button claims, out of what this page's own paid runs have cost. The
    // label used to read `~$5 on Sonnet` and it was a constant somebody typed once: three
    // real runs on this page came in at $4.00, $8.09 and $10.63, so a reader who budgeted
    // for the label was out by a factor of two. The markup keeps the range as its
    // fallback, which is what a static copy and a server with no ledger both show.
    var price = caps.price;
    if (price && price.text) {
      buttons.forEach(function (btn) {
        var fmt = btn.getAttribute('data-tip-fmt');
        if (!fmt) return;
        btn.setAttribute('data-tip', fmt.replace('{price}', price.text));
        btn.setAttribute('data-idle-tip', btn.getAttribute('data-tip'));
      });
      var face = panel && panel.querySelector('.hrconfirm-price');
      if (face) face.textContent = 'about ' + price.text + ' on Sonnet';
      var last = panel && panel.querySelector('.hrconfirm-last');
      if (last && price.last) {
        last.textContent = 'The last one really cost $' + price.last.toFixed(2)
          + (price.n > 1 ? ', and that average is over the last ' + price.n + '.' : '.');
        last.hidden = false;
      }
    }
    // Per button, from the probe's own answer for that verb. Inferring the paid one from
    // the free one would draw a $5 control over a server that has no model step beside it.
    buttons.forEach(function (btn) {
      if (!window.HR.can(btn.getAttribute('data-rerun'))) return;
      btn.hidden = false;
      btn.removeAttribute('aria-disabled');
      // The free one *is* the served badge, so the word steps aside for it. Only the free
      // one: `Rerun + AI` is a second thing this page can do, not a second way of saying
      // what this page is.
      if (btn.getAttribute('data-rerun') !== '__rerun__') return;
      var mode = document.getElementById('hr-mode');
      if (mode) mode.hidden = true;
    });
  });
})();
