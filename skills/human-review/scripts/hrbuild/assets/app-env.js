// The deployed-app row: what is up, and the one or two things you can do about it.
//
// Two copies of this report exist and the row has to be honest in both, and it is now the
// *same row* in both — one line of verbs, and only the click differs. Served, a verb runs
// its command through the review server and the row keeps its own state: nothing
// answering, so Start; something answering, so the address as a link, then Stop. Off disk
// nothing here can run, so every verb is a clipboard for its command and both are on
// screen, because which line the reader wants to paste is their business.
//
// There is no Where any more. Served, the row asks the host itself, once, when the address
// it remembers does not answer — a reader who started the stack in another tab gets the
// link without having to know there was a question to ask.
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
  // The containers a Start is bringing up, one chip each, while it brings them up.
  //
  // "Starting…" can stand in this row for minutes — a first build, then a database that
  // takes its time to say healthy — and on a stage, with a room watching, nothing on it
  // told "still coming" from "broken" (7 Oct 2026). Docker Desktop could, one window away.
  // So the row shows what Docker Desktop shows, as small as it can be said: one chip per
  // container, its service name and a light — grey while it is created or its healthcheck
  // is still out, green once it runs (or a one-shot exited 0), red once it exited non-zero
  // or went unhealthy, with the last line it printed as the hover, which is where the
  // reason is. Before there are containers there are images being built, and those are
  // chips too, so a first build is not a blank minute.
  //
  // Read from the server's `/__compose__`, which follows the run's own `docker compose`
  // output — so it is served-only, like the Start that feeds it, and off disk the row is
  // exactly what it was. The chips leave once the Start succeeds, because the address is
  // then the whole story; they stay when it fails, because then they are the story.
  //
  // A factory over the row and nothing else, so the same lines can be lifted into a page
  // built before this existed (a `live-patch-docker-status` script) without dragging the
  // rest of the row's state machine with them.
  // >>> appenv-pods
  function appenvPods(bar) {
    var box = bar.querySelector('.appenv-pods');
    if (!box) {
      box = document.createElement('span');
      box.className = 'appenv-pods';
      box.hidden = true;
      box.setAttribute('aria-live', 'polite');
      var at = bar.querySelector('.appenv-at');
      if (at && at.parentNode) at.parentNode.insertBefore(box, at.nextSibling);
      else (bar.querySelector('.appenv-run') || bar).appendChild(box);
    }
    var run = null, timer = null, live = false;

    // `petclinic-env-backend:806e3de6` and `petclinic-env-frontend:806e3de6` are
    // "backend" and "frontend": the tag and whatever every name starts with say nothing
    // a chip has room for. One image keeps its whole name, there being nothing to compare.
    function shortNames(images) {
      var bare = images.map(function (i) {
        return String(i).replace(/:[^/:]*$/, '').replace(/^.*\//, '');
      });
      if (bare.length < 2) return bare;
      var head = bare.reduce(function (a, b) {
        var n = 0;
        while (n < a.length && a[n] === b[n]) n++;
        return a.slice(0, n);
      });
      var cut = head.lastIndexOf('-') + 1;
      return bare.map(function (b) { return b.slice(cut) || b; });
    }

    var WORD = {up: 'running', done: 'finished', starting: 'starting', down: 'failed',
                build: 'building image', built: 'image built'};

    function chip(light, name, tip) {
      var el = document.createElement('span');
      el.className = 'appenv-pod';
      el.dataset.light = light;
      el.textContent = name;
      el.dataset.tip = tip;
      return el;
    }

    function draw(j) {
      var rows = (j && j.containers) || [], imgs = (j && j.images) || [];
      var parts = [];
      if (rows.length) {
        rows.forEach(function (r) {
          var said = WORD[r.light] + (r.status ? ' — ' + r.status : '');
          parts.push(chip(r.light, r.service,
                          r.light === 'down' && r.tip ? r.service + ': ' + r.tip
                                                      : r.service + ' ' + said));
        });
      } else if (imgs.length) {
        var names = shortNames(imgs.map(function (i) { return i.image; }));
        imgs.forEach(function (i, n) {
          var light = i.state === 'Built' ? 'built' : 'build';
          parts.push(chip(light, names[n], i.image + ' — ' + WORD[light]));
        });
      }
      if (!parts.length) { box.hidden = true; box.textContent = ''; return; }
      var count = document.createElement('span');
      count.className = 'appenv-pods-n';
      count.textContent = rows.length
        ? j.up + '/' + j.total + ' up'
        : imgs.filter(function (i) { return i.state === 'Built'; }).length + '/'
          + imgs.length + ' built';
      box.textContent = '';
      parts.forEach(function (el) { box.appendChild(el); });
      box.appendChild(count);
      box.hidden = false;
    }

    function ask() {
      var id = run;
      return fetch('/__compose__?run=' + encodeURIComponent(id), {cache: 'no-store'})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { if (j && id === run) draw(j); })
        .catch(function () {});
    }

    // Once a second while the run is going. A chained timeout, not an interval: a poll
    // that took longer than a second (docker is slow to answer mid-build) must not stack.
    function loop() {
      if (!live) return;
      ask().then(function () { if (live) timer = setTimeout(loop, 1000); });
    }

    return {
      // Called with every snapshot of the Start; only the first one of a run starts it.
      watch: function (id) {
        if (!id || id === run) return;
        run = id; live = true;
        clearTimeout(timer);
        loop();
      },
      // The run is over. One last look, so a container that died in the final second is
      // on screen red; then, on success, the chips step out for the address.
      finish: function (ok) {
        live = false;
        clearTimeout(timer);
        if (!run) return;
        if (ok) { run = null; draw(null); return; }
        ask();
      },
      clear: function () { live = false; clearTimeout(timer); run = null; draw(null); }
    };
  }
  // <<< appenv-pods

  var bar = document.querySelector('.appenv');
  if (!bar) return;
  var pods = appenvPods(bar);
  var state = bar.querySelector('.appenv-state');
  var addr = bar.querySelector('.appenv-url');
  // The wrapper per verb, not the button: each wrapper holds the clipboard/play pair that
  // `command_html` emitted, and SERVER_JS owns which of the two is up.
  var acts = {start: bar.querySelector('.appenv-start'),
              stop: bar.querySelector('.appenv-stop')};
  // The DB Fixture card under this one (or, on a page built before it had a card of its
  // own, a row inside this band): a Seed button per fixture, all drawn by the build from
  // the project's files. The names never move; only the buttons are armed here, once
  // something answers.
  var resets = bar.querySelector('.appenv-fixtures') || document.querySelector('.appenv-fixtures');
  var SEED_OFF = 'Start the app first';
  // One command at a time. `docker compose up` is minutes, the row stays readable
  // throughout, and a second press in the middle of it is a reader who could not tell the
  // first one had started — a Stop sent into a half-built stack is the worst of them.
  var busy = false;
  // Raised by the probe in SERVER_JS, never assumed: a page on GitHub Pages is https and
  // is not served by us, and the buttons here must not believe otherwise.
  var served = false;
  // Whether the host has been asked where the app is. Once per page: the answer is either
  // an address, which is then remembered, or nothing, and asking again would say nothing.
  var looked = false;
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

  // A whole verb, in or out of the row. The tip is not touched: it was written by the
  // build and it says what a click here does, which does not change with the state.
  function show(wrap, on) { if (wrap) wrap.hidden = !on; }

  // The row's whole truth, in one call. Each verb is gated on the thing it actually needs
  // — because a control that can be pressed while its precondition is missing is a
  // control that lies: Reset would fail, and \u25b8 would drive an app that is not there.
  //
  // Off disk the gate is open on both. Neither can *run* there — they are
  // clipboards — and a clipboard for `stop` is exactly as useful with the app down as up:
  // the reader is pasting it into a terminal, where the state of things is their business
  // and not this page's. Hiding two thirds of the commands behind a health check was the
  // old second row's worst habit and there is no reason to inherit it.
  function setLive(live, why) {
    show(acts.start, !served || !live);
    show(acts.stop, !served || live);
    // Greyed, never hidden: the fixtures are on screen with the app down (8 Oct 2026),
    // and a Seed that disappears with it would leave a row of names that do nothing.
    resetButtons().forEach(function (el) { arm(el, live, live ? resetTip(el) : SEED_OFF); });
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

  // `failed` is a command that just died: what to say, and the line it died on, in place
  // of a bare `Offline` if nothing answers. It has to ride *through* the probe rather than
  // be written after it — the health check answers asynchronously, and writing the failure
  // first meant `Offline` landed on top of it a moment later. A start that dies in two
  // seconds then looked like a button that did nothing at all.
  function probe(failed) {
    var b = base();
    function down(tip) {
      say('down', failed ? failed.word : 'Offline');
      if (failed) state.dataset.tip = failed.tip;
      else state.removeAttribute('data-tip');
      setLive(false, failed ? failed.tip : tip);
    }
    if (!b) { if (!look(failed)) down('Nothing is running yet'); return; }
    say('unknown', 'checking\u2026');
    setLive(false, 'Checking whether anything is listening\u2026');
    // /healthz answers with CORS open, so this works from a file:// page too. A failure
    // here means "nothing is listening", which is the normal case for an old report.
    fetch(b + '/healthz', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw 0;
      say('live', '');
      setLive(true);
      listFixtures(b);
    }).catch(function () {
      if (!look(failed)) down('Nothing is answering at ' + b + ' \u2014 start it first');
    });
  }

  // Nothing answers at the address this browser remembers — or it remembers none — so ask
  // the host whether an instance is up somewhere else, and take its address if one is.
  // Served only, since only a served page can run the host's `url` command, and once: the
  // reader who started the stack from a terminal, in another tab, or before clearing their
  // site data lands on the row with the link already in it. It is a read — the host is
  // asked where, nothing is started — which is why it may run merely because the page
  // opened. It replaced the Where button, which asked the same question on a press, and
  // was a button nobody could guess the meaning of.
  //
  // Not after a failure: a start that just died has something to say, and the question has
  // been asked already by then anyway. And not over a Start or Stop pressed meanwhile —
  // those own the row until they finish.
  function look(failed) {
    if (failed || looked || !served || busy || !window.HR.can('demo-env-url')) return false;
    looked = true;
    say('unknown', 'looking\u2026');
    setLive(false, 'Asking the host whether the app is already up\u2026');
    window.HR.run('demo-env-url', {}).then(function (done) {
      if (busy) return;
      if (done.state === 'done' && adopt(done.result && done.result.base)) return;
      probe();
    }).catch(function () { if (!busy) probe(); });
    return true;
  }

  function resetButtons() {
    return resets ? [].slice.call(resets.querySelectorAll('.appenv-reset')) : [];
  }
  // What each Seed puts back, in words. A fixture is a named set of extra demo rows the
  // environment loads on top of the seed; when the environment describes it (`about`),
  // the description joins the tip.
  function resetTip(el) {
    var name = el && el.dataset.fixture, about = el && el.dataset.about;
    if (!name) return 'Reset the DB to the seed, the starting data';
    return 'Reset the DB to the seed, then load the \u201c' + name + '\u201d fixture on top'
           + (about ? ': ' + about : '');
  }
  // A fixture as the environment lists it: a bare name, or `{name, about}` — and a
  // top-level `about: {name: text}` map is read too.
  function fixtureOf(item, info) {
    var name = typeof item === 'string' ? item : (item && item.name);
    if (!name) return null;
    var about = (item && typeof item === 'object' && (item.about || item.description))
                || (info.about && typeof info.about === 'object' && info.about[name]) || '';
    return {name: String(name), about: String(about)};
  }

  // The names are the build's; the environment is asked only what it can actually load.
  // An instance started from an image older than a fixture's SQL would answer its Seed
  // with a 404, so that one Seed is greyed and says why — the name stays, because the
  // fixture is the branch's. No list in the answer (a sidecar from before fixtures) or
  // no answer at all changes nothing.
  function listFixtures(b) {
    if (!bar.dataset.reset) return;
    fetch(b + bar.dataset.reset, {cache: 'no-store'}).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (info) {
      // The row may have moved on while this was in flight — stopped, or pointed at
      // another instance — and its answer is not this one's.
      if (!info || base() !== b || state.dataset.state !== 'live'
          || !Array.isArray(info.fixtures)) return;
      var found = {};
      info.fixtures.forEach(function (item) {
        var f = fixtureOf(item, info);
        if (f) found[f.name] = f;
      });
      resetButtons().forEach(function (el) {
        var name = el.dataset.fixture || '';
        if (!name) return;
        if (!found[name]) {
          arm(el, false, 'The running app has no \u201c' + name + '\u201d fixture: it '
                         + 'was started from a commit without ' + name + '.sql');
          return;
        }
        if (found[name].about) el.dataset.about = found[name].about;
        arm(el, true, resetTip(el));
      });
    }).catch(function () {});
  }

  // What a verb that exited non-zero leaves in the row: its own word, and the last line it
  // printed as the tip, which is where the reason is.
  function failure(word, done, otherwise) {
    return {word: word, tip: window.HR.tail(done) || otherwise};
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
    // Start is the one verb that brings containers up; Stop's chips would only be a row
    // of things going away, which the address disappearing already says.
    var watched = id === 'demo-env';
    pods.clear();
    return window.HR.run(id, {}, function (snap) {
      var line = window.HR.tail(snap);
      if (line) state.dataset.tip = line;
      if (watched) pods.watch(snap.run);
    }).then(function (done) {
      busy = false;
      state.removeAttribute('data-tip');
      if (watched) pods.finish(done.state === 'done');
      then(done);
    }).catch(function (e) {
      busy = false;
      if (watched) pods.finish(false);
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

  function startApp() {
    drive('demo-env', 'Starting', function (done) {
      if (done.state === 'done' && adopt(done.result && done.result.base)) return;
      // It ran and printed no URL we recognised, or it failed. Either way the reader is
      // back where they started rather than stuck: `probe` re-reads whatever base we
      // have, and the command below is still there to be run by hand.
      probe(done.state === 'done' ? null
            : failure('start failed', done, 'the command exited ' + done.exit));
    });
  }
  onrun(acts.start, startApp);

  // A Start already in flight when this page loads: pick it up instead of saying Offline.
  //
  // The page reloads itself whenever anything rewrites the report beside it, and on the
  // demo day (7 Oct 2026) something did, a few seconds into a Start. The reloaded tab had
  // forgotten the run — "Offline", no chips — while the stack came up behind it, and only
  // a manual refresh, after the fact, found the address. So a served page asks first
  // whether a `demo-env` run is going, and if one is it presses Start itself: the server
  // hands a second press the run already in flight rather than a second build, so this is
  // the very same follow — chips, then the address — the original press would have had.
  function resume() {
    return fetch('/__run_status__', {cache: 'no-store'}).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      var a = j && j.active;
      if (!a || a.action !== 'demo-env' || a.state !== 'running' || busy) return false;
      startApp();
      return true;
    }).catch(function () { return false; });
  }

  // Stop forgets the address as well as freeing the port. Remembering it would leave
  // every link in the transcript pointing confidently at nothing, and the next Start will
  // hand us a different port anyway.
  onrun(acts.stop, function () {
    drive('demo-env-stop', 'Stopping', function (done) {
      if (done.state === 'done') { current = ''; remember(''); apply(); }
      probe(done.state === 'done' ? null
            : failure('stop failed', done, 'the command exited ' + done.exit));
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
      // of the three verbs. Unless a Start is already running, which owns the row.
      resume().then(function (resumed) { if (!resumed) probe(); });
    }
    // The `probe` above is also what asks the host where the app is, when the remembered
    // address does not answer — see `look`. It waits for this point because `served` is
    // what says the host can be asked at all.
  });

  // Explicit, never automatic. Resetting on every link click would throw away work the
  // reviewer was in the middle of; the duplicate rows and unique-constraint collisions it
  // exists to prevent are the reviewer's own repeated form submissions, and they know
  // when they have made a mess.
  //
  // One listener for the whole row.
  if (resets) resets.addEventListener('click', function (ev) {
    var btn = ev.target.closest('.appenv-reset');
    if (!btn || blocked(btn) || btn.disabled) return;
    var b = base();
    if (!b) return;
    var name = btn.dataset.fixture, face = btn.textContent, all = resetButtons();
    all.forEach(function (el) { el.disabled = true; });
    // Marks, not words: "Seeding…" is twice as wide as "Seed", and a button that grows
    // mid-press shoves every fixture after it sideways (the min-width holds these).
    btn.textContent = 'Seed\u2026';
    fetch(b + bar.dataset.reset + (name ? '/' + encodeURIComponent(name) : ''),
          {method: 'POST', cache: 'no-store'}).then(function (r) {
      btn.textContent = r.ok ? 'Seed \u2713' : 'Seed \u2717';
    }).catch(function () { btn.textContent = 'Seed \u2717'; }).then(function () {
      setTimeout(function () {
        btn.textContent = face;
        all.forEach(function (el) { el.disabled = false; });
      }, 1400);
    });
  });
})();
