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
        // The server holds its reload-watcher for the length of the rerun, so this is the
        // single reload of the whole run rather than one per producer.
        location.reload();
        return;
      }
      stop(btn, 'the rebuild did not finish', snap);
    }).catch(function (e) {
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

  function confirmSpend() {
    if (!panel) return Promise.resolve(true);
    lastFocus = document.activeElement;
    panel.hidden = false;
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
