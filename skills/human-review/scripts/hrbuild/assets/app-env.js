// The deployed-app row: what is up, and the one or two things you can do about it.
//
// Two copies of this report exist and the row has to be honest in both, and it is now the
// *same row* in both — one line of verbs, and only the click differs. Served, a verb runs
// its command through the review server and the row keeps its own state: nothing
// answering, so Start; something answering, so the address as a link, then Stop and Where.
// Off disk nothing here can run, so every verb is a clipboard for its command and all
// three are on screen, because which line the reader wants to paste is their business.
//
// What this file decides is *which verbs apply*. Which face each one wears — clipboard or
// play — is SERVER_JS's answer, per action, and it is why the wrapper span is what gets
// hidden here rather than the buttons: two owners of `hidden` on one element is how a
// control ends up flickering between two truths.
//
// A control is hidden when it cannot be used, not greyed. Greying is for a thing you
// could have had under a condition worth teaching; Stop before anything has started is
// not that, it is noise in the four-item row a reader scans in one glance.
//
// Everything is written to start in the degraded state and *rise*. A button drawn as live
// that falls back 30ms later has already been clicked by then, and has already lied.
(function () {
  var bar = document.querySelector('.appenv');
  if (!bar) return;
  var state = bar.querySelector('.appenv-state');
  var addr = bar.querySelector('.appenv-url');
  // The wrapper per verb, not the button: each wrapper holds the clipboard/play pair that
  // `command_html` emitted, and SERVER_JS owns which of the two is up.
  var acts = {start: bar.querySelector('.appenv-start'),
              stop: bar.querySelector('.appenv-stop'),
              where: bar.querySelector('.appenv-where')};
  var reset = bar.querySelector('.appenv-reset');
  // One command at a time. `docker compose up` is minutes, the row stays readable
  // throughout, and a second press in the middle of it is a reader who could not tell the
  // first one had started — a Stop sent into a half-built stack is the worst of them.
  var busy = false;
  // Raised by the probe in SERVER_JS, never assumed: a page on GitHub Pages is https and
  // is not served by us, and the buttons here must not believe otherwise.
  var served = false;
  var links = Array.prototype.slice.call(document.querySelectorAll('a[data-app]'));
  // Per page, not per machine: two review pages describe two branches, and each branch
  // gets its own instance on its own port.
  var KEY = 'human-review:appbase:' + location.pathname;

  function stored() {
    // A browser with site data blocked throws on read; the page must still work.
    try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  }
  function remember(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  // The address the row is about. There is no box to type one into any more — served,
  // Start prints it and we scrape it; off disk, the build's own `base` is the only guess
  // anyone had — so it lives here and in localStorage, which is what survives a reload.
  var current = stored();
  function base() {
    return (current || bar.dataset.fallback || '').replace(/\/+$/, '');
  }

  function apply() {
    var b = base();
    links.forEach(function (a) {
      var path = a.dataset.app;
      if (b) { a.href = b + path; a.classList.remove('dead'); }
      // No base and no fallback: the link has nowhere to point, and saying so by going
      // grey is honest where a live-looking link that 404s is not.
      else { a.removeAttribute('href'); a.classList.add('dead'); }
    });
  }

  var blocked = function (el) { return el.getAttribute('aria-disabled') === 'true'; };
  function arm(el, on, tip) {
    if (!el) return;
    el.setAttribute('aria-disabled', on ? 'false' : 'true');
    el.dataset.tip = tip;
  }
  // Disarmed *and* gone. Both, and in that order, because `hidden` is a style and a
  // stylesheet that failed to load would otherwise leave a live button behind.
  function gate(el, on, tip) { if (!el) return; arm(el, on, tip); el.hidden = !on; }

  // A whole verb, in or out of the row. The tip is not touched: it was written by the
  // build and it says what a click here does, which does not change with the state.
  function show(wrap, on) { if (wrap) wrap.hidden = !on; }

  // The row's whole truth, in one call. Each verb is gated on the thing it actually needs
  // — because a control that can be pressed while its precondition is missing is a
  // control that lies: Reset would fail, and \u25b8 would drive an app that is not there.
  //
  // Off disk the gate is open on all three. None of them can *run* there — they are
  // clipboards — and a clipboard for `stop` is exactly as useful with the app down as up:
  // the reader is pasting it into a terminal, where the state of things is their business
  // and not this page's. Hiding two thirds of the commands behind a health check was the
  // old second row's worst habit and there is no reason to inherit it.
  function setLive(live, why) {
    show(acts.start, !served || !live);
    show(acts.stop, !served || live);
    show(acts.where, !served || live);
    gate(reset, live, 'Put the demo data back to its seed');
    [].forEach.call(document.querySelectorAll('.cue-drive'), function (el) {
      arm(el, live, live ? 'Drive the app to this point' : why);
    });
    // The address is shown only while something answers at it. A URL on a page next to a
    // dead port is the one thing here that can waste a reader's afternoon.
    if (addr) {
      addr.hidden = !live;
      if (live) { addr.href = base(); addr.textContent = base(); }
      else { addr.removeAttribute('href'); addr.textContent = ''; }
    }
  }

  // Live has nothing to say that the address does not say better, so the pill empties and
  // disappears; every other state is a word in its place. `Offline` and not `offline at
  // http://…`: the URL of a thing that is not answering is an invitation to click it.
  function say(kind, text) {
    state.dataset.state = kind;
    state.textContent = text || '';
    state.hidden = !text;
  }

  function probe() {
    var b = base();
    if (!b) {
      say('down', 'Offline');
      setLive(false, 'Nothing is running yet');
      return;
    }
    say('unknown', 'checking\u2026');
    setLive(false, 'Checking whether anything is listening\u2026');
    // /healthz answers with CORS open, so this works from a file:// page too. A failure
    // here means "nothing is listening", which is the normal case for an old report.
    fetch(b + '/healthz', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw 0;
      say('live', '');
      setLive(true);
    }).catch(function () {
      say('down', 'Offline');
      setLive(false, 'Nothing is answering at ' + b + ' \u2014 start it first');
    });
  }

  apply(); probe();

  // Learned once the environment answers on its own, and only then. `adopt` is the whole
  // reason the Start button is worth more than the clipboard: `start-docker.sh` ends by
  // printing the port the host gave it, the server scrapes that line, and the address the
  // reader would otherwise have had to hunt for appears in the row. Two round trips to
  // the terminal become none — the second being the one nobody counts, where you go
  // back to find the URL again because the clipboard has moved on.
  function adopt(url) {
    if (!url) return false;
    current = url.replace(/\/+$/, '');
    remember(current); apply(); probe();
    return true;
  }

  // The copy button used to be this block's own, with its own clipboard call and its own
  // "Copied" label. It is `command_html`'s now, like every other command on the page, and
  // handled by the one clipboard-and-toast handler in EDITOR_JS — a second implementation
  // for one button is how two of them end up behaving differently.

  // While a command is in flight nothing else in the row may be pressed — a Stop sent
  // into the middle of a docker build is a half-torn-down instance nobody asked for —
  // and the pill carries the last line the command printed, which is where a reader who
  // wants to know it is still moving can look. A docker build prints thousands.
  function drive(id, face, then) {
    busy = true;
    setLive(false, 'Waiting for the app \u2014 ' + face.toLowerCase() + '\u2026');
    say('unknown', face + '\u2026');
    return window.HR.run(id, {}, function (snap) {
      var line = window.HR.tail(snap);
      if (line) state.dataset.tip = line;
    }).then(function (done) {
      busy = false;
      state.removeAttribute('data-tip');
      then(done);
    }).catch(function (e) {
      busy = false;
      say('down', face + ' failed');
      state.dataset.tip = e.message || 'the review server is no longer running';
      setLive(false, state.dataset.tip);
    });
  }

  // The run half of one verb, and the click that belongs to it.
  //
  // `stopPropagation` is the whole reason these are bound here rather than left to
  // EDITOR_JS, which already runs any `.runhere[data-action]` on the page: that handler
  // reloads the page when the command finishes, and this row's entire job is to *keep* the
  // page while the app comes up underneath it — the address it scraped, the links it
  // aimed, the place the reader had got to. A listener on the button itself runs in the
  // target phase, before the document-level one in the bubble phase, so stopping there is
  // enough. The clipboard half is deliberately left alone: off disk the generic
  // copy-and-toast handler is exactly right, and a second implementation of it for this
  // row is how two of them end up behaving differently.
  function onrun(wrap, fn) {
    var btn = wrap && wrap.querySelector('.cmd-run');
    if (!btn) return;
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (busy) return;
      fn();
    });
  }

  onrun(acts.start, function () {
    drive('demo-env', 'Starting', function (done) {
      if (done.state === 'done' && adopt(done.result && done.result.base)) return;
      // It ran and printed no URL we recognised, or it failed. Either way the reader is
      // back where they started rather than stuck: `probe` re-reads whatever base we
      // have, and the command below is still there to be run by hand.
      probe();
      if (done.state !== 'done') {
        say('down', 'start failed');
        state.dataset.tip = window.HR.tail(done) || 'the command exited ' + done.exit;
      }
    });
  });

  // Stop forgets the address as well as freeing the port. Remembering it would leave
  // every link in the transcript pointing confidently at nothing, and the next Start will
  // hand us a different port anyway.
  onrun(acts.stop, function () {
    drive('demo-env-stop', 'Stopping', function (done) {
      if (done.state === 'done') { current = ''; remember(''); apply(); }
      probe();
      if (done.state !== 'done') {
        say('down', 'stop failed');
        state.dataset.tip = window.HR.tail(done) || 'the command exited ' + done.exit;
      }
    });
  });

  // Where: the address of the instance, and a way into it.
  //
  // It is the one verb of the three that is not a state change, which is why it is worth a
  // control of its own beside the address it duplicates: the address in the row is the one
  // *this browser* remembers, and Where is the host being asked. A reader who started the
  // stack in another tab, or cleared their site data, or is looking at a page somebody
  // else served has nothing remembered — and this is the button that fixes that without
  // a terminal. When the base is already known it skips the round trip and just opens it.
  onrun(acts.where, function () {
    var b = base();
    if (b) { window.open(b, '_blank', 'noopener'); return; }
    drive('demo-env-url', 'Asking', function (done) {
      if (done.state === 'done' && adopt(done.result && done.result.base)) {
        var u = base();
        if (u) window.open(u, '_blank', 'noopener');
        return;
      }
      probe();
      if (done.state !== 'done') {
        say('down', 'nothing is up');
        state.dataset.tip = window.HR.tail(done) || 'the host knows of no instance';
      }
    });
  });

  // One command per caption. Off disk it is copied, because a file cannot drive a browser
  // on your machine; served, it is run, and what the reader gets back is the app already
  // sitting on the screen the caption describes. Same template either way — the one in
  // `data-drive` for the clipboard, the one the build declared for the server — so the
  // two paths cannot drift into doing different things.
  var tmpl = bar.dataset.drive;
  if (tmpl) document.querySelectorAll('.cue-drive').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (blocked(btn)) return;
      var was = btn.innerHTML;
      function tick(mark, hold) {
        btn.classList.add('copied'); btn.innerHTML = mark;
        setTimeout(function () { btn.classList.remove('copied'); btn.innerHTML = was; }, hold);
      }
      if (window.HR.can('cue-drive')) {
        btn.innerHTML = '&#8943;';
        window.HR.run('cue-drive', {n: btn.dataset.n, base: base()})
          .then(function (done) {
            if (done.state === 'done') { tick('&#10003;', 1400); return; }
            btn.dataset.tip = window.HR.tail(done) || 'the driver exited ' + done.exit;
            tick('&#10007;', 2600);
          })
          .catch(function (e) { btn.dataset.tip = e.message; tick('&#10007;', 2600); });
        return;
      }
      var cmd = tmpl.replace(/\{n\}/g, btn.dataset.n).replace(/\{base\}/g, base());
      window.HR.copy(cmd).then(function () {
        tick('&#10003;', 1400);
      }).catch(function () { btn.dataset.tip = 'could not copy'; });
    });
  });

  window.HR.onready(function () {
    // `demo-env` and not merely "is there a server": a build that stopped declaring the
    // start command has to be able to take the verb away from a page that is still open.
    served = window.HR.can('demo-env');
    if (served) {
      // Which swaps the whole row: the verbs come back and the terminal command steps
      // out, since pressing Start here does the same thing without leaving the page.
      bar.classList.add('appenv-served');
      // Re-ask rather than re-deriving from whatever the pill happens to say: the first
      // probe may still be in flight, and `served` has just changed the answer for two
      // of the three verbs.
      probe();
    }
    // Asking the host used to happen right here, the instant nothing was remembered —
    // which is every first visit, every browser with site data blocked, and every reload
    // a rebuild notification triggers, `location.reload()` in SERVER_JS included. That
    // made *opening the page* run `demo-env-url` — a shell command of the project,
    // `./start-docker.sh url …` — merely because the tab existed, before anyone had
    // touched anything. The row now shows only what it already knows (nothing remembered
    // renders as Offline, same as any other base it cannot reach) and asks the host for
    // real only from a press that already means it: Where, when nothing is remembered
    // yet, falls through to exactly this command below.
  });

  // Explicit, never automatic. Resetting on every link click would throw away work the
  // reviewer was in the middle of; the duplicate rows and unique-constraint collisions it
  // exists to prevent are the reviewer's own repeated form submissions, and they know
  // when they have made a mess.
  if (reset) reset.addEventListener('click', function () {
    if (blocked(reset)) return;
    var b = base();
    if (!b) return;
    reset.disabled = true; reset.textContent = 'Resetting\u2026';
    fetch(b + bar.dataset.reset, {method: 'POST', cache: 'no-store'}).then(function (r) {
      reset.textContent = r.ok ? 'Reset' : 'Reset failed';
    }).catch(function () { reset.textContent = 'Reset failed'; }).then(function () {
      setTimeout(function () { reset.textContent = 'Reset DB'; reset.disabled = false; }, 1400);
    });
  });
})();
