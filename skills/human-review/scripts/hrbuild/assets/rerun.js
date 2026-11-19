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
  // The presses that spend money, and so open the confirmation first: the masthead's and a
  // tab's 🤖, and the Tests tab's 🧬 Mutagent (AI writes mutants of the PR's code).
  var PAID = {'__rerun_ai__': true, '__mutagent__': true};

  // The run while it runs: a bar, the step it is on, how many are left, roughly how long.
  //
  // What the page can see is the run's tail (`snap.output`), and `run-steps.py` prints one
  // line as it starts each step -- `  * diagrams -> data,packages` -- and `refresh-report.py`
  // prints the build command when the steps are over. What the page cannot see is the
  // future: how long `diagrams` will take is not in the log until it is over. So the band
  // estimates from the last run, whose timings the build carried onto the element as
  // `data-expect` (`.steps-cache.json`), and credits the step in flight with the time it
  // has been running, capped just under its expectation so the bar never reaches the end
  // before the run does.
  //
  // It survives a refresh, which the turning glyph never did. The server keeps the run and
  // says which one is going (`HR.status()`), so a page that loads mid-run adopts it -- see
  // the block at the bottom -- and the reader who reloaded to "check" finds the same bar
  // further along rather than a page that looks like nothing was ever pressed.
  var progress = (function () {
    var box = document.getElementById('hr-rerun-progress');
    var noop = function () {};
    if (!box) return {start: noop, update: noop, finish: noop, hide: noop};
    var expect = {};
    try { expect = JSON.parse(box.getAttribute('data-expect') || '{}'); } catch (e) {}
    var steps = expect.steps || {};
    var order = Object.keys(steps);
    var fill = box.querySelector('.rerunprog-fill');
    var say = box.querySelector('.rerunprog-say');
    var eta = box.querySelector('.rerunprog-eta');
    // `run-steps.py`: `  * name -> tabs` (running), `  = name: unchanged…`, `  - name: skipped`.
    var STEP = /^  ([*=-]) ([\w-]+)(?: ->|:)/gm;
    // `refresh-report.py`: `[refresh] $ … build-review-html.py …` once the steps are done.
    var BUILD = /\[refresh\] \$ [^\n]*build-review-html\.py/g;
    var runStarted = 0, began = {}, primed = false, kind = 'rerun';

    // The tab pills, each its own progress bar. A tab is what its free ↺ re-derives
    // (`data-steps`), so how far along a tab is comes from the same step lines as the band:
    // a step done or skipped counts whole, the one in flight by the time it has run against
    // what it took last time. A tab joins the run when one of its steps is in it — the one
    // whose ↺ was pressed from the start, so the press shows at once. Never backwards: the
    // tail the server hands back is the run's last lines, and a step that scrolled out of it
    // has not come undone. The tab's own steps take it to 80%, the build that follows them
    // the rest of the way: until the reload nothing on the tab has changed, but a bar that
    // stood still for the fifteen seconds of the build read as a run that had hung.
    // The presses sit in each tab's own title now, not beside its pill, so the pill is found
    // by the tab they name — once per tab: the Data tab carries a set per card.
    var tabs = [], seen = {};
    document.querySelectorAll('.tabre > .tabrerun[data-rerun="__rerun__"][data-tab][data-steps]')
      .forEach(function (b) {
        var id = b.getAttribute('data-tab');
        var pill = document.getElementById('tabbtn-' + id);
        if (seen[id] || !pill || !pill.classList.contains('tab')) return;
        seen[id] = true;
        tabs.push({pill: pill, id: id, on: false, at: 0,
                   steps: b.getAttribute('data-steps').split(',').filter(Boolean)});
      });
    // A lit pill is the run's bar, so the band under the masthead steps aside for it: the
    // same bar twice, one over the other, was the tab pill's news told a second time. What
    // the band said in words -- the step it is on, the seconds left -- moves into the hover
    // of every lit pill (`sayTabs`). A run no pill has joined yet (the model thinking, a
    // masthead press still on a step no tab owns) keeps the band: it is the only bar there.
    function paintTab(t, frac) {
      t.at = Math.max(t.at, frac);
      if (!t.on) { t.on = true; t.pill.classList.add('tab-regen'); box.hidden = true; }
      t.pill.style.setProperty('--tab-fill', (t.at * 100).toFixed(1) + '%');
    }
    function sayTabs() {
      var line = say.textContent + (eta.textContent ? ' \u00b7 ' + eta.textContent : '');
      tabs.forEach(function (t) { if (t.on) t.pill.setAttribute('data-tip', line); });
    }
    function paintTabs(frac, built) {
      tabs.forEach(function (t) {
        var mine = t.steps.filter(function (n) { return n in frac || order.indexOf(n) >= 0; });
        if (!mine.some(function (n) { return n in frac; })) return;
        var got = 0, all = 0;
        mine.forEach(function (n) { all += expected(n); got += (frac[n] || 0) * expected(n); });
        paintTab(t, Math.min(0.98, 0.8 * (all ? got / all : 0) + 0.2 * built));
      });
    }
    function clearTabs() {
      tabs.forEach(function (t) {
        t.on = false; t.at = 0;
        t.pill.classList.remove('tab-regen');
        t.pill.removeAttribute('data-tip');
        t.pill.style.removeProperty('--tab-fill');
      });
    }

    function expected(name) {
      return typeof steps[name] === 'number' ? steps[name] : (expect['default'] || 5);
    }
    function total() {
      var t = expect.build || 15;
      if (!order.length) return t;
      order.forEach(function (n) { t += expected(n); });
      return t;
    }
    // How long the phase in flight has been running. The page that pressed the button
    // saw the phase's first line arrive and clocked it (`began`). A page that loaded
    // mid-run did not, and reckons instead from the run's own clock -- the server says
    // when it started, and says it again to whoever asks -- minus what the steps before
    // this one are expected to have taken, a skipped or unchanged step counting as no
    // time at all. Either way the bar stands where it stood before the reload, rather
    // than restarting the step at zero.
    function credit(name, spentBefore, cap) {
      var since = began[name] > 0 ? began[name]
        : (runStarted ? runStarted + spentBefore * 1000 : Date.now());
      var elapsed = (Date.now() - since) / 1000;
      return Math.max(0, Math.min(elapsed, cap * 0.95));
    }
    // `only`: the steps a tab's ↻ re-runs, so the bar measures that run and not the
    // masthead's whole list.
    function start(k, startedAt, only, tab) {
      kind = k || 'rerun';
      clearTabs();
      tabs.forEach(function (t) { if (t.id === tab) paintTab(t, 0.02); });
      order = only && only.length
        ? only.slice() : Object.keys(steps);
      runStarted = startedAt ? startedAt * 1000 : Date.now();
      began = {}; primed = false;
      box.classList.remove('failed');
      fill.style.width = '0%';
      say.textContent = kind === 'rerun_ai' ? 'Asking the model…' : 'Rebuilding this page…';
      eta.textContent = startedAt ? 'started ' + clock(startedAt) : '';
      box.hidden = tabs.some(function (t) { return t.on; });
      sayTabs();
    }
    function clock(epochSeconds) {
      var at = new Date(epochSeconds * 1000);
      return at.getHours() + ':' + ('0' + at.getMinutes()).slice(-2);
    }
    function update(snap) {
      var out = (snap && snap.output) || '';
      if (snap && snap.started && !runStarted) runStarted = snap.started * 1000;
      var seen = [], m, lastStepAt = -1, buildAt = -1;
      STEP.lastIndex = 0;
      while ((m = STEP.exec(out))) { seen.push({mark: m[1], name: m[2]}); lastStepAt = m.index; }
      BUILD.lastIndex = 0;
      while ((m = BUILD.exec(out))) buildAt = m.index;
      var building = buildAt > lastStepAt;
      var now = Date.now();
      // The first tail this page sees may be a run already well along: nothing in it
      // "began now". A phase already in that tail is marked as such (-1) and reckoned
      // from the run's clock for as long as it lasts; only a line that arrives on a
      // later poll is clocked at the moment it arrives.
      var clock = primed; primed = true;
      var done = 0, spent = 0, current = null, stepFrac = {};
      seen.forEach(function (st, i) {
        var last = i === seen.length - 1;
        if (!began[st.name]) began[st.name] = clock ? now : -1;
        if (st.mark !== '*' || !last || building) {
          done += expected(st.name);
          stepFrac[st.name] = 1;
          if (st.mark === '*') spent += expected(st.name);
          return;
        }
        current = st.name;
        var ran = credit(st.name, spent, expected(st.name));
        done += ran;
        stepFrac[st.name] = ran / expected(st.name);
      });
      var all = total();
      if (building) {
        if (!began['\0build']) began['\0build'] = clock ? now : -1;
        done = all - (expect.build || 15) + credit('\0build', spent, expect.build || 15);
      }
      paintTabs(stepFrac, building
        ? credit('\0build', spent, expect.build || 15) / (expect.build || 15) : 0);
      var frac = Math.max(0, Math.min(1, all ? done / all : 0));
      fill.style.width = (frac * 100).toFixed(1) + '%';
      var n = order.length || seen.length;
      if (building) {
        say.textContent = 'Building the page…';
      } else if (current) {
        say.textContent = 'Rebuilding: ' + current + ' (' + seen.length + '/' + n + ')';
      } else if (seen.length) {
        say.textContent = 'Rebuilding: ' + seen[seen.length - 1].name + ' (' + seen.length + '/' + n + ')';
      } else {
        say.textContent = kind === 'rerun_ai' ? 'Asking the model…' : 'Starting…';
      }
      // No estimate while the model is thinking: nothing on the page has measured that.
      if (seen.length || kind !== 'rerun_ai') {
        eta.textContent = '~' + Math.max(1, Math.round(all - done)) + ' s left';
      }
      sayTabs();
    }
    function finish() {
      tabs.forEach(function (t) { if (t.on) paintTab(t, 1); });
      fill.style.width = '100%';
      say.textContent = 'Done — reloading…';
      eta.textContent = '';
      sayTabs();
    }
    function hide() { box.hidden = true; clearTabs(); }
    return {start: start, update: update, finish: finish, hide: hide};
  })();

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
  // Also called without a reload, when the run's build was swapped in place (`landed`).
  function sayWhatHappened() {
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
  }
  sayWhatHappened();
  // Where the reader was, restored after the reload this ends in — `HR.keepPlace`, which
  // the diagram offers and every other `reload` action call too. It used to live here, and
  // "the same scroll position, saved under the same key, restored on the same event" is
  // not a thing to keep two copies of: the second control to end in a reload would have
  // had to find this one and copy it.
  var remember = window.HR.keepPlace;

  function stop(btn, problem, snap) {
    progress.hide();
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

  // The run is done and its build is on disk. The watcher brings it in (`HR.afterRebuild`):
  // swapped in place, after which the press is over here and now — the bar goes, the
  // buttons come back, the summary shows — or by a reload, which keeps the place and the
  // summary for the other side as it always did.
  function landed(btn, since) {
    if (!window.HR.afterRebuild) { remember(); location.reload(); return; }
    window.HR.afterRebuild(since).then(function () {
      progress.hide();
      buttons.forEach(function (other) { other.disabled = false; });
      if (btn) {
        btn.classList.remove('running');
        btn.setAttribute('data-tip', btn.getAttribute('data-idle-tip') || '');
      }
      sayWhatHappened();
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
    var since = window.HR.stampNow ? window.HR.stampNow() : null;
    var tab = btn.getAttribute('data-tab');
    // The steps this press runs, when the button says: a tab's own, or the masthead ↺⏳'s
    // whole list (the slow ones included, which the last run's timings may not know).
    var only = btn.getAttribute('data-steps');
    progress.start(PAID[btn.getAttribute('data-rerun')] ? 'rerun_ai' : 'rerun',
                   0, only ? only.split(',') : null, tab);
    window.HR.run(btn.getAttribute('data-rerun'), tab ? {tab: tab} : {}, function (snap) {
      // One line, in the hover: the button has room for a word and the reader who wants
      // to know which producer it is on is the reader already pointing at it. The lit tab's
      // hover, or the band under the masthead while no tab is lit, says the rest.
      var line = window.HR.tail(snap);
      btn.setAttribute('data-tip', line || 'Rebuilding this page\u2026');
      progress.update(snap);
    }).then(function (snap) {
      if (snap.state === 'done') {
        progress.finish();
        // What the run said about itself, kept for the other side of the reload.
        stash(snap);
        // The server holds its reload-watcher for the length of the rerun, so this is the
        // single reload of the whole run rather than one per producer — or no reload at all,
        // when the tabs the run changed can be swapped in place (`landed`).
        landed(btn, since);
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
        sayBusy({active: e.busy || {}, kind: (e.busy && PAID[e.busy.action])
                   ? 'rerun_ai' : 'rerun',
                 started: e.busy && e.busy.started, joined: 0});
        return;
      }
      stop(btn, e.message || 'the review server could not be reached', null);
    });
  }

  // A draw.io card's own ring: redraw that one picture and put it back where it is.
  //
  // The tab's ring re-derives the whole tab and rebuilds the whole page, then reloads it —
  // forty seconds on the Data tab, for a box the reader moved in draw.io, and the reload
  // drops them at the top of the card. This one asks the server for the card alone
  // (`refresh-card.py`); the answer comes back about a second later *with the new card in
  // it*, and the old one is replaced where it stands. No reload, so the scroll, the open
  // tab and every other card stay exactly as they were. The markup is what a full build
  // writes (the server's side holds that), so the page after the swap is the page a
  // reload would have shown.
  //
  // The ring itself is kept, not swapped: it is the element the probe raised and the
  // listeners are on. Anything else in the card that needs waking up — the (i), the
  // Revert's run mark — hears `hr:swapped`.
  function swapCard(live, markup, btn) {
    var tpl = document.createElement('template');
    tpl.innerHTML = markup;
    var fresh = tpl.content.firstElementChild;
    if (!fresh || fresh.getAttribute('data-drawio') !== live.getAttribute('data-drawio')) {
      return false;
    }
    var ring = btn.closest('.tabre'), slot = fresh.querySelector('.tabre');
    if (ring && slot) slot.parentNode.replaceChild(ring, slot);
    live.parentNode.replaceChild(fresh, live);
    document.dispatchEvent(new CustomEvent('hr:swapped', {detail: fresh}));
    return true;
  }

  function redrawCard(btn) {
    if (fail) fail.hidden = true;
    if (done) { done.hidden = true; done.classList.remove('going'); }
    buttons.forEach(function (other) { other.disabled = true; });
    btn.classList.add('running');
    var name = btn.getAttribute('data-card');
    // Held from the press, not from the answer: the server publishes the new stamp the
    // moment the redraw is done, and a watch poll landing between that and the swap would
    // reload the page out from under it.
    window.HR.quiet(true);
    function free() {
      btn.classList.remove('running');
      btn.setAttribute('data-tip', btn.getAttribute('data-idle-tip') || '');
      buttons.forEach(function (other) { other.disabled = false; });
    }
    window.HR.run('__rerun__', {card: name}, function (snap) {
      btn.setAttribute('data-tip', window.HR.tail(snap) || 'Redrawing this diagram\u2026');
    }).then(function (snap) {
      if (snap.state !== 'done') {
        window.HR.quiet(false);
        stop(btn, 'the redraw did not finish', snap);
        return;
      }
      var live = btn.closest('[data-drawio]');
      if (!snap.card || !live || !swapCard(live, snap.card, btn)) {
        // No card came back: the redraw rebuilt the whole page instead — the drawing
        // became the base's again (or stopped being), which strikes the tab — so load it.
        remember();
        location.reload();
        return;
      }
      window.HR.adoptStamp(snap.stamp);
      window.HR.quiet(false);
      free();
    }).catch(function (e) {
      window.HR.quiet(false);
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

  // The panel's first paragraph describes the masthead's paid run. A tab's paid ↻ says
  // what *it* buys instead (`data-confirm`), and the masthead's sentence comes back after.
  var bodyP = panel && panel.querySelector('.hrconfirm-b');
  var bodyDefault = bodyP ? bodyP.innerHTML : '';

  // Each priced paid press is quoted out of its own program's ledger: the probe answers
  // `prices[kind]`, and the button names its kind in `data-price` — absent means the
  // matrix's, which is also what the older probe's single `price` was. The Demo tab's 🤖
  // rewrites the film's script, a different run at a different price, and quoting the
  // matrix's figure over it would be the label typed once and never measured all over again.
  var prices = {};
  function priceOf(btn) {
    var kind = (btn && btn.getAttribute('data-price')) || 'model';
    return prices[kind] || null;
  }
  // The model the quoted runs were on, out of the same ledger rows as the price — never a
  // name typed into the markup. Eval run 8's chip said "on Sonnet" over a Haiku run. A
  // probe that names none (an older server, an empty ledger) keeps the markup's own word.
  function onModel(price, fallback) {
    return ' on ' + ((price && price.model) || fallback || 'Sonnet');
  }
  function fillPrice(fmt, price) {
    return fmt.replace('{price} on Sonnet', price.text + onModel(price))
      .replace('{price}', price.text);
  }
  function lastLine(price) {
    if (!price || !price.last) return '';
    return 'The last one really cost $' + price.last.toFixed(2)
      + (price.n > 1 ? ', and that average is over the last ' + price.n + '.' : '.');
  }

  function confirmSpend(btn) {
    if (!panel) return Promise.resolve(true);
    var priced = !!(btn && btn.getAttribute('data-tip-fmt'));
    var price = priced ? priceOf(btn) : null;
    if (bodyP) {
      var own = btn && btn.getAttribute('data-confirm');
      if (own) {
        bodyP.textContent = (price && price.text ? 'About ' + price.text + onModel(price) + '. ' : '')
          + own;
      } else bodyP.innerHTML = bodyDefault;
    }
    // "The last one really cost $X" is that program's own invoice. A paid press with no
    // ledger of its own (the Review tab's re-review wears no `data-tip-fmt`) must not
    // borrow the matrix's.
    var lastP = panel.querySelector('.hrconfirm-last');
    if (lastP) {
      var borrowed = !!(btn && btn.getAttribute('data-confirm')
                        && !btn.getAttribute('data-tip-fmt'));
      lastP.textContent = lastLine(price);
      lastP.hidden = borrowed || !lastP.textContent;
    }
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

  // Per button, at load and for every one a rebuild swaps in later (`hr:swapped`, below):
  // a tab's panel carries its own ↻ in its title, and a swapped panel's arrive unwired.
  function bind(btn) {
    if (btn.__hrRerun) return;
    btn.__hrRerun = true;
    // Stashed on the element so `stop` can put the button back exactly as it was without
    // a closure per button holding the strings. Only the tooltip: the face is a glyph the
    // run never replaces, so there is nothing else to put back.
    btn.setAttribute('data-idle-tip', btn.getAttribute('data-tip') || '');
    var paid = PAID[btn.getAttribute('data-rerun')];
    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      if (btn.hasAttribute('data-card')) { redrawCard(btn); return; }
      if (!paid) { go(btn); return; }
      confirmSpend(btn).then(function (yes) { if (yes) go(btn); });
    });
  }
  buttons.forEach(bind);

  window.HR.onready(function (caps) {
    if (!caps) return;
    // The price the button claims, out of what this page's own paid runs have cost. The
    // label used to read `~$5 on Sonnet` and it was a constant somebody typed once: three
    // real runs on this page came in at $4.00, $8.09 and $10.63, so a reader who budgeted
    // for the label was out by a factor of two. The markup keeps the range as its
    // fallback, which is what a static copy and a server with no ledger both show.
    prices = caps.prices || {};
    if (!prices.model && caps.price) prices.model = caps.price;
    var price = prices.model;
    buttons.forEach(raise);
    if (price && price.text) {
      var face = panel && panel.querySelector('.hrconfirm-price');
      if (face) face.textContent = 'about ' + price.text + onModel(price);
    }
    adopt();
  });

  // What the probe's answer does to one button: its price, and whether it shows at all.
  function raise(btn) {
    var caps = window.HR.caps && window.HR.caps();
    if (!caps) return;
    var fmt = btn.getAttribute('data-tip-fmt');
    var own = priceOf(btn);
    if (fmt && own && own.text) {
      btn.setAttribute('data-tip', fillPrice(fmt, own));
      btn.setAttribute('data-idle-tip', btn.getAttribute('data-tip'));
      if (/costs about/.test(btn.getAttribute('aria-label') || '')) {
        btn.setAttribute('aria-label', 'Rerun with AI \u2014 costs about ' + own.text + onModel(own));
      }
    }
    // Per button, from the probe's own answer for that verb. Inferring the paid one from
    // the free one would draw a $5 control over a server that has no model step beside it.
    if (!window.HR.can(btn.getAttribute('data-rerun'), btn.getAttribute('data-tab'),
                       btn.getAttribute('data-card'))) return;
    btn.hidden = false;
    btn.removeAttribute('aria-disabled');
    // The `Served` badge stays beside it: Victor wants the badge to say what this copy
    // is, and the ↺ to be only the press.
  }

  // A rebuild swapped part of the page in place (`panels.js`, or a card redrawn above): the
  // buttons that left are dropped from the set every run disables, the new ones join it,
  // wired and raised exactly as the ones the page loaded with. A card's own ring is moved
  // into the new card rather than replaced, so it is already bound and is skipped.
  document.addEventListener('hr:swapped', function (ev) {
    var root = ev.detail;
    if (!root || !root.querySelectorAll) return;
    buttons = buttons.filter(function (b) { return b.isConnected; });
    [].forEach.call(root.querySelectorAll('button.chip-rerun[data-rerun]'), function (btn) {
      if (btn.__hrRerun) return;
      buttons.push(btn);
      bind(btn);
      raise(btn);
      // A run is going (every button is disabled while one is): the new one waits with them.
      if (buttons.some(function (b) { return b.disabled && b !== btn; })) btn.disabled = true;
    });
  });

  // A run that was going when this page loaded. The reader pressed Rerun, waited, and
  // refreshed to see whether it was done -- and the old page answered with a masthead
  // that looked exactly as it did before the press. The server still has the run; ask it,
  // follow the tail to the same end the press would have, and reload when it is done. Only
  // the two reruns: a diagram's own rerun has its own glyph and is not this band's news.
  function adopt() {
    if (!window.HR.status || !window.HR.follow) return;
    window.HR.status().then(function (st) {
      if (!st || !st.active || (st.kind !== 'rerun' && st.kind !== 'rerun_ai')) return;
      var id = st.kind === 'rerun_ai' ? '__rerun_ai__' : '__rerun__';
      var mine = null;
      buttons.forEach(function (b) {
        b.disabled = true;
        if (!mine && b.getAttribute('data-rerun') === id && !b.hasAttribute('data-tab')
            && !b.hasAttribute('data-card')) {
          mine = b; b.classList.add('running');
        }
      });
      progress.start(st.kind, st.started);
      progress.update(st.active);
      var since = window.HR.stampNow ? window.HR.stampNow() : null;
      window.HR.follow(st.active, progress.update).then(function (snap) {
        if (snap.state === 'done') {
          progress.finish();
          stash(snap);
          landed(mine || buttons[0], since);
          return;
        }
        stop(mine || buttons[0], 'the rebuild did not finish', snap);
      }).catch(function (e) {
        stop(mine || buttons[0], e.message || 'the review server could not be reached', null);
      });
    }).catch(function () {});
  }
})();
