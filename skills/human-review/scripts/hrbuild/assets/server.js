// Is there a review server behind this page, and what will it run for me?
//
// This replaces `location.protocol === 'http:'`, which answered a different question and
// answered it wrongly in the one place it mattered most. The demo published on GitHub
// Pages is https, so the protocol test said "served": four hundred and seventy-seven
// editor handles on that page each fetched `/__open__` against github.io, collected a
// 404, and toasted "Could not open OwnerController.java" at a reader who had clicked a
// perfectly good link. A page cannot tell a web server from *its* web server by looking
// at the scheme. It has to ask, and the answer has to be one only ours can give — hence
// a JSON body carrying our own key: GitHub's 404 page is HTML and does not parse, and
// something else on :7654 parses but says nothing about human review.
//
// The probe is asynchronous, and everything downstream of it is written to start in the
// degraded state and *rise* when it answers. Never the other way round. A button drawn
// as live that falls back to the clipboard 30ms later has already been clicked by then,
// and has already lied; one that appears capable a moment after the page paints has cost
// nobody anything.
//
// It carries a list of actions rather than a boolean for the same reason. "Am I served?"
// is not what a button needs to know — `cue-drive` needs to know whether *drive-to-cue*
// is on offer here, and a build that stopped declaring it has to be able to take the
// verb away from a page that is still open.
window.HR = (function () {
  var caps = null, settled = false, waiting = [];

  // Three copies of this page, not two: off disk (`file:`), served by `serve-review.py`,
  // and **online** — GitHub Pages, or any server that is not ours. Online nothing on this
  // Mac can be reached: no review server to run a command, no editor to open a file in,
  // and a command copied from here names folders on the machine that built the page. Such
  // a control stays on screen, greyed, and says how to get the copy where it works.
  //
  // Decided here, before anything paints, and in the degraded direction like everything
  // else in this file: every page that is not `file:` starts `hr-online`, and the class
  // comes off when the probe answers. The probe itself is asked only of a loopback host,
  // which is the only place `serve-review.py` ever listens: on github.io it was one red
  // 404 line in the console of every reader, for an answer known in advance.
  var ROOT = document.documentElement;
  var FILE = location.protocol === 'file:';
  var LOOPBACK = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
  if (!FILE) ROOT.classList.add('hr-online');

  // Skipped on `file:` and off loopback — a host, never a protocol. The *general* protocol
  // check is the bug this whole probe replaced — the demo on GitHub Pages is https, so `location.protocol === 'http:'` said
  // "not served" about a page that was, and `=== 'https:'` would say "served" about one
  // that is not. `file:` is different in kind: a page read off disk cannot have been
  // served by us under any circumstances, and fetching a relative URL from it is not a
  // failed probe, it is a request the browser refuses and logs as an error in the console
  // of every reader who opens the guide off disk. Skipping it is the difference between a
  // clean console and one red line that means nothing. Off loopback is the same in kind:
  // `serve-review.py` binds 127.0.0.1 and nothing else, so on github.io the answer is
  // known before the question and asking it was a 404 in every reader's console.
  var ready = (FILE || !LOOPBACK ? Promise.resolve(null)
    : fetch('/__human_review__', {cache: 'no-store'})
    .then(function (r) { return r.ok ? r.json() : null; })
    // `humanReview` present, or this is somebody else's JSON on somebody else's port.
    .then(function (j) { return (j && j.humanReview) ? j : null; })
    // And a fetch that failed for any other reason — no server on this port, somebody
    // else's server, a reap mid-load — is the same answer: not served. It is the normal
    // path out of the zip and off GitHub Pages, not a failure to report.
    .catch(function () { return null; }))
    .then(function (j) {
      caps = j; settled = true;
      if (j) ROOT.classList.remove('hr-online');
      waiting.splice(0).forEach(function (fn) { try { fn(j); } catch (e) {} });
      return j;
    });

  // Two ids are not manifest entries and never can be: the two reruns are the server's own
  // commands, answered by the probe rather than declared by a build (see `rerun` below).
  // They are spelt like actions anyway so that every control on the page — the play mark
  // beside a printed command included — asks one question and gets one answer, instead of
  // each caller growing its own special case for the two verbs that are not in the list.
  // `__rerun_tests__` is ↺⏳: run what takes long, then rebuild — narrowed to the Tests
  // tab's suites when a tab is named, every slow producer when not (the masthead's). It
  // rides the free rerun's endpoint (same lock, same reload hold) with `mode: "tests"`,
  // and the two scopes are two answers of the probe, since a build may declare either.
  var OWN = {'__rerun__': 'rerun', '__rerun_ai__': 'rerunAi', '__rerun_tests__': 'rerunTests'};

  function can(id, tab, card) {
    // One draw.io card's own ring: its redraw is a manifest entry of its own, declared by
    // the build beside the card (`__rerun__:card:<name>`), so a server or a build without
    // it leaves the ring down rather than offering a press that reruns the whole tab.
    if (card) return !!(caps && caps.actions && caps.actions[id + ':card:' + card]);
    if (id === '__rerun_tests__' && !tab) return !!(caps && caps.rerunTestsAll);
    if (OWN[id]) return !!(caps && caps[OWN[id]]);
    return !!(caps && caps.actions && caps.actions[id]);
  }

  // Fires once, with the answer, whenever it arrives — before or after registration.
  // `settled` and not `caps !== null`, because "no server" is an answer and null is how
  // it is spelt.
  function onready(fn) { if (settled) fn(caps); else waiting.push(fn); }

  // Poll rather than stream. `./start-docker.sh up` is a docker build: minutes of output
  // this page shows one line of. An EventSource would be a second protocol, a second
  // failure mode and a connection held open across the reap, to deliver six words a
  // second more promptly than a timer does.
  function poll(snap, onprogress) {
    if (onprogress) { try { onprogress(snap); } catch (e) {} }
    if (snap.state !== 'running') return Promise.resolve(snap);
    return new Promise(function (resolve, reject) {
      setTimeout(function () {
        fetch('/__run_status__?run=' + encodeURIComponent(snap.run), {cache: 'no-store'})
          .then(function (r) {
            if (!r.ok) throw new Error('the review server lost track of that run');
            return r.json();
          })
          .then(function (next) { resolve(poll(next, onprogress)); })
          .catch(reject);
      }, 700);
    });
  }

  // Resolves with the finished snapshot ({state, exit, output, result}) whatever the
  // exit code — a command that ran and failed is an answer, not an exception. It rejects
  // only when the *request* could not be made or the run could not be followed, which is
  // the case where the caller has to fall back to the clipboard.
  function run(id, params, onprogress) {
    if (!can(id, params && params.tab, params && params.card)) return Promise.reject(new Error(id + ' is not available here'));
    // The two server-owned verbs route to their own endpoints. Here rather than in every
    // caller: `run(id)` is what the whole page reaches for, and a play mark beside the
    // refresh command that had to know it was special would be the one control on the page
    // whose wiring depended on which command it was printing.
    // `params.tab` narrows either one to a tab's own producers (the ↻ beside its pill).
    if (id === '__rerun__') return rerun(onprogress, params && params.tab, params && params.card);
    if (id === '__rerun_ai__') return rerunAi(onprogress, params && params.tab);
    if (id === '__rerun_tests__') {
      var tab = params && params.tab;
      return ask('/__rerun__', tab ? 'rerunTests' : 'rerunTestsAll', onprogress, tab, 'tests');
    }
    return fetch('/__run__', {
      method: 'POST', cache: 'no-store',
      // Both halves deliberate. POST + a non-simple Content-Type is not a request a
      // cross-origin page may send without a preflight, and the server answers no
      // preflight — so the browser refuses on our behalf before anything arrives. The
      // token is the belt to that pair of braces: it is minted per server process and
      // handed out only over the same-origin-guarded probe above, so a page that never
      // read the probe cannot produce it.
      headers: {'Content-Type': 'application/json',
                'X-Human-Review-Token': (caps && caps.token) || ''},
      body: JSON.stringify({id: id, params: params || {}})
    }).then(function (r) {
      if (r.ok) return r.json();
      // The server explains its refusals in the body — "n is not a valid int",
      // "cue-drive is not an action this review declares" — and a reader who sees the
      // sentence can act on it where a generic shrug leaves them nothing.
      return r.text().then(function (t) { throw new Error(t || 'the review server refused'); });
    }).then(function (first) { return poll(first, onprogress); });
  }

  // The masthead's Rerun. Same request braces as `run` — POST, a Content-Type that
  // forces a preflight nobody answers, the per-process token — and the same poller. What
  // it does not send is an id, because there is nothing for the page to name: the command
  // is the server's own refresh program. So this is gated on `caps.rerun`, which the
  // probe answers, and not on a manifest entry a build could forget to write.
  function rerun(onprogress, tab, card) {
    return ask('/__rerun__', 'rerun', onprogress, tab, undefined, card);
  }

  // The same verb with the model's half in front of it: the requirements matrix and the
  // per-test catalogue, rewritten by `claude -p --model sonnet`, and then the same static
  // refresh with `--allow-model`. A second endpoint and a second capability, not a flag on
  // the first — the difference between the two is money, and a boolean in a request body is
  // the wrong place for that to live: a page that sent it by mistake would have bought a
  // judgement nobody asked for. The server shares one lock between them, so a click on
  // either while the other is working joins the run in flight rather than starting a
  // second build over the same directory.
  function rerunAi(onprogress, tab) {
    return ask('/__rerun_ai__', 'rerunAi', onprogress, tab);
  }

  function ask(route, capability, onprogress, tab, mode, card) {
    if (!caps || !caps[capability]) {
      return Promise.reject(new Error('this page cannot rebuild itself here'));
    }
    return fetch(route, {
      method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json',
                'X-Human-Review-Token': (caps && caps.token) || ''},
      body: JSON.stringify(Object.assign(tab ? {tab: tab} : {}, mode ? {mode: mode} : {},
                                         card ? {card: card} : {}))
    }).then(function (r) {
      if (r.ok) return r.json();
      // A refusal may be a sentence or it may be a refusal *with the run it is refusing
      // for* — the paid rerun answers 409 and names what is already going, because "yours
      // did not start" is useless to a reader who cannot see what did.
      return r.text().then(function (t) {
        var body = null;
        try { body = JSON.parse(t); } catch (e) {}
        var err = new Error((body && body.error) || t || 'the review server refused');
        err.status = r.status;
        err.busy = body;
        throw err;
      });
    }).then(function (first) { return poll(first, onprogress); });
  }

  // Is anything running on this server at all — not "how is *my* run doing". There was no
  // way to ask, and the gap cost real money: a press on the paid button was joined in
  // silence to a paid run somebody else had started an hour earlier, over a working tree
  // that had moved since, and the only way out was to pay for a second one. A page cannot
  // warn about a run it cannot see.
  function status() {
    return fetch('/__run_status__', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('the review server did not answer');
      return r.json();
    });
  }

  // Where the reader was, kept across the reload a command ends in.
  //
  // On HR because three controls now end in `location.reload()` — the masthead's two
  // Reruns and every command declared with `reload` — and the reload may not even be
  // theirs: the server watches the directory it serves, so a build finishing can reload
  // the tab first. All of them have to land on the same saved place, and a second copy of
  // this in another script block is how two of them end up landing differently.
  //
  // Per page, because a reader keeps several of these open and each is a different branch.
  // Which *tab* they were on needs nothing: it is in `location.hash`, which a reload keeps.
  var PLACE = 'hr-rerun-place:' + location.pathname;

  function keepPlace() {
    try {
      sessionStorage.setItem(PLACE, JSON.stringify({y: window.pageYOffset}));
    } catch (e) {}
  }

  window.addEventListener('load', function () {
    var saved = null;
    try {
      saved = JSON.parse(sessionStorage.getItem(PLACE) || 'null');
      sessionStorage.removeItem(PLACE);
    } catch (e) {}
    // Consumed, always: a place restored twice is a page that will not let the reader
    // scroll away from where they once pressed a button.
    if (saved && typeof saved.y === 'number' && saved.y > 0) {
      window.scrollTo(0, saved.y);
    }
  });

  // The clipboard, once, for the whole page.
  //
  // `navigator.clipboard` is not available on a `file://` page in every browser — and a
  // page read off disk is precisely the copy whose only route is the clipboard, so the
  // fallback is not a nicety there, it is the feature. A hidden textarea and
  // `document.execCommand('copy')` is the one thing that works in that case.
  //
  // Here rather than in each script that needs it: there were two of these, they had
  // drifted (one had the fallback, one did not), and the one without it was the one on the
  // control that only exists off disk.
  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(fallback);
    }
    return Promise.resolve(fallback());
    function fallback() {
      var box = document.createElement('textarea');
      box.value = text;
      box.setAttribute('readonly', '');
      box.style.cssText = 'position:fixed;top:-1000px;opacity:0';
      document.body.appendChild(box);
      box.select();
      try { document.execCommand('copy'); } catch (e) { /* nothing else to try */ }
      box.remove();
    }
  }

  // The last line the command has printed, for a control with room for one line.
  function tail(snap) {
    var lines = (snap.output || '').split('\n');
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    return lines.length ? lines[lines.length - 1].trim() : '';
  }

  // Live reload, the way a dev server does it: the build rewrites `.human-review/`, and
  // the tab showing it catches up on its own.
  //
  // It matters more here than on a dev server. This page is read *while* it is being
  // rebuilt — a reviewer reads a finding, asks for the diagram to be re-rendered or the
  // report to be regenerated, and goes on reading. Until now the only page that reloaded
  // itself was the one whose own button did the rebuilding; a rebuild from the terminal
  // beside it left the reader looking at a report that no longer matched the disk, with
  // nothing on screen to say so. Silent staleness is the failure mode this whole page is
  // built against.
  //
  // Polled, not streamed, and for the reason the run poller gives above: an EventSource
  // is a second protocol and a connection held open across the reap, in exchange for a
  // second of promptness on a page nobody is timing. The server does the debouncing —
  // the stamp only moves once the tree has stopped being written — so a rebuild that
  // takes twenty seconds reloads this tab once, at the end, and not on its first file.
  // A draw.io card's redraw changes the page on disk *and* hands the page the new card,
  // which it swaps in place (`rerun.js`). The reload the watcher would then do is the
  // forty-second round trip the swap exists to avoid, and it would lose the reader's place:
  // so while a swap is under way the watcher does not reload, and when it is done it adopts
  // the stamp the server published for exactly the tree the swap came from.
  //
  // And a rebuild no longer has to mean a reload. The build also writes the page cut along
  // its tabs (`hrbuild/shared/panels.py`), the watch answer carries their hashes, and
  // `panels.js` (`HR.catchUp`) swaps in place the tabs that changed, keeping the reader's
  // tab, scroll and open folds. It refuses — the shell moved, a tab came or went, a panel
  // whose own script cannot be re-run changed, anything threw — and then this reloads, as
  // it always did.
  var seenStamp = null, holdReload = 0, watching = false, waiters = [];
  function adoptStamp(stamp) { if (stamp) seenStamp = stamp; }
  function quiet(on) { holdReload = Math.max(0, holdReload + (on ? 1 : -1)); }
  function stampNow() { return seenStamp; }
  function reloadKeepingPlace() { keepPlace(); location.reload(); }

  function catchUp(w) {
    if (!window.HR.catchUp) { location.reload(); return Promise.resolve(false); }
    holdReload++;
    return window.HR.catchUp(w).then(function () {
      seenStamp = w.stamp;
      holdReload--;
      waiters.splice(0).forEach(function (fn) { try { fn(); } catch (e) {} });
      return true;
    }, function () { reloadKeepingPlace(); return false; });
  }

  // For a control whose run rebuilt the page (the ↺ presses, rerun.js): resolves once the
  // page shows that build — in place — and never when it took a reload to get there.
  // `since` is the stamp the page had when the run started: the server holds the watcher
  // for the length of a run, so a stamp other than that one is the run's build, possibly
  // already swapped in by the poll before the run's own poller heard it was done.
  function afterRebuild(since) {
    if (!watching) { reloadKeepingPlace(); return new Promise(function () {}); }
    if (since && seenStamp !== since) return Promise.resolve();
    return new Promise(function (resolve) {
      var t = setTimeout(reloadKeepingPlace, 20000);
      waiters.push(function () { clearTimeout(t); resolve(); });
    });
  }

  onready(function (j) {
    if (!j || !j.watch) return;
    var misses = 0;
    seenStamp = j.watch;
    watching = true;
    (function next() {
      // Slower when the tab is in the background: it will be reloaded before anyone
      // looks at it either way, and a dozen parked reports are a dozen pollers.
      setTimeout(function () {
        fetch('/__watch__', {cache: 'no-store'})
          .then(function (r) { if (!r.ok) throw new Error('refused'); return r.json(); })
          .then(function (w) {
            misses = 0;
            // `reload()` and not a cache-buster: the server sends no-store.
            if (w.stamp && w.stamp !== seenStamp && !holdReload) {
              catchUp(w).then(function (inPlace) { if (inPlace) next(); });
              return;
            }
            next();
          })
          // The server is mortal by design — idle for `--idle-minutes` and it is gone,
          // under a tab that is still open. That is not an error to report, it is the
          // end of the poll: three tries so a blip does not end it, then silence.
          .catch(function () { if (++misses < 3) next(); else watching = false; });
      }, document.hidden ? 5000 : 1000);
    })();
  });

  onready(function (j) {
    if (!j) return;
    // The favicon glows green: a reader keeps several of these open — one per branch, a
    // static copy of an old one beside a live one — and the tab strip is where they pick
    // between them, long before anything in the page is on screen. The icon and not the
    // title, because at forty tabs the icon is all of the tab that is left. The badge in
    // the title row says the same thing, but only to someone already looking at the page.
    var icon = document.getElementById('hr-favicon');
    if (icon && icon.dataset.served) {
      if (!icon.dataset.static) icon.dataset.static = icon.href;
      icon.href = icon.dataset.served;
    }
    // Nothing left to copy: the Serve line is the one that got the reader here.
    var serveChip = document.getElementById('hr-serve');
    if (serveChip) serveChip.hidden = true;
    var chip = document.getElementById('hr-mode');
    if (!chip) return;
    chip.textContent = 'Served';
    chip.classList.add('chip-served');
    // The badge replaces `Static` and carries what serving means, so the ↺ beside it can
    // keep a one-line hover about what *it* does.
    chip.setAttribute('data-tip', 'Served by the review server (serve-review.py): the '
      + 'buttons on this page run their action here instead of copying a command, '
      + 'recordings play in the page, and \u21BA regenerates it.');
    // A \u23F9\uFE0F after the word, inside the badge: stop the server now rather than wait
    // out its idle timeout. Inside the pill, not a round button beside it — one pill reads
    // as "served, and here is how to stop that", two read as two states. Its own button,
    // with its own tip, so the word before it keeps the badge's hover.
    // The page stays on screen as it was, and says so.
    if (j.stop && !document.getElementById('hr-stop')) {
      var stop = document.createElement('button');
      stop.type = 'button';
      stop.id = 'hr-stop';
      stop.className = 'chip-stop';
      stop.textContent = '\u23F9\uFE0F';
      stop.setAttribute('aria-label', 'Stop the review server');
      stop.setAttribute('data-tip', 'Stop the review server now. It stops by itself after '
        + 'a few idle hours anyway; this page stays open, as a static copy.');
      chip.appendChild(stop);
      stop.addEventListener('click', function (ev) {
        ev.stopPropagation();
        stop.disabled = true;
        fetch('/__stop__', {
          method: 'POST', cache: 'no-store',
          headers: {'Content-Type': 'application/json',
                    'X-Human-Review-Token': j.token || ''},
          body: '{}'
        }).then(function (r) {
          if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
          stop.remove();
          chip.hidden = false;
          chip.textContent = 'Stopped';
          chip.classList.remove('chip-served');
          chip.setAttribute('data-tip', 'The review server was stopped. Buttons on this page '
            + 'no longer run anything; start it again with the Serve command.');
          if (icon && icon.dataset.static) icon.href = icon.dataset.static;
          if (window.HR.flash) window.HR.flash('Review server stopped');
        }).catch(function (e) {
          stop.disabled = false;
          if (window.HR.flash) window.HR.flash(e.message || 'Could not stop the server');
        });
      });
    }
    // …and where the rerun chip can really run, *it* is this badge and this one goes: the
    // two were one fact written twice. The word stays for the served page whose server
    // cannot rebuild it — rarer than it sounds, and the only case where `served` has
    // something to say that the glyph beside it would not be able to honour. RERUN_JS
    // does the hiding, because it is the half that knows whether the chip came up.
    // Where an action can actually run, the offer under the diagram changes from `run
    // this` to `click here` and the command stops being shown: the button does the job,
    // and a shell line beside it is for a reader who is not here. Per action and not per
    // page — one block can carry four, and a server that answers for the re-render does
    // not necessarily answer for the rest.
    // Every offer on the page, not only the ones under a diagram: the aftermath band's
    // regenerate is the same control in a different place, and a selector naming one of the
    // two places is how the second one silently ships with both buttons on screen.
    raiseRunHere(document);
    // And inside a card swapped in after load (`rerun.js` redraws a draw.io card in place):
    // its Revert ships `hidden` like every other play mark, and nothing else would raise it.
    document.addEventListener('hr:swapped', function (ev) { raiseRunHere(ev.detail); });
  });

  function raiseRunHere(root) {
    if (!root || !root.querySelectorAll) return;
    [].forEach.call(root.querySelectorAll('button.runhere[data-action]'),
        function (b) {
      if (!can(b.getAttribute('data-action'))) return;
      b.setAttribute('data-tip', b.getAttribute('data-tip-served')
        || b.getAttribute('data-tip'));
      var offer = b.closest ? b.closest('.offer') : null;
      if (offer) offer.classList.add('served');
      // The run glyph beside an offer. It ships `hidden` in every copy of the report — a
      // glyph that says "this runs here" on a page with nothing behind it is a lie in one
      // character — and this is the line that raises it, per action, off the same answer
      // that decides the words beside it.
      //
      // And the clipboard beside it goes, in the same breath. The two used to sit side by
      // side on a served page and the pair asked the reader a question the page already
      // knew the answer to: one of them runs the command here, the other hands them the
      // line to go and run somewhere else, and nothing on screen said which was which
      // until they had pressed one. Exactly one glyph per command, per copy of the report
      // — the clipboard where nothing can run, this where something can — so the mark
      // *is* the answer instead of an option beside it.
      if (!b.classList.contains('cmd-run')) return;
      b.hidden = false;
      var pair = b.closest ? b.closest('.cmd') : null;
      var clip = pair && pair.querySelector('.cmd-copy');
      if (clip) clip.hidden = true;
    });
  }

  // ---- Online: what cannot work here is greyed, and says so ----------------------------
  //
  // One mechanism for every such control: the attribute `data-local` (its value, when
  // given, replaces the hint), plus the two kinds of link that name this Mac by their
  // scheme. Online they are greyed by core.css, inert to a click, which explains instead,
  // and their tooltip is the hint (tip.js asks `HR.localOnly`). Adding a control to the set
  // is adding the attribute where it is drawn. Served and off disk nothing changes: the
  // class is not on <html> there.
  var LOCAL = '[data-local], a[href^="drawio:"], a[href^="vscode:"]';
  var LOCAL_HINT = 'Works on a local copy — see Serve';
  function localOnly(el) {
    if (!ROOT.classList.contains('hr-online') || !el || !el.closest) return null;
    return el.closest(LOCAL);
  }
  function localHint(el) { return (el && el.getAttribute('data-local')) || LOCAL_HINT; }

  // Capture, on the document: the first listener to see the click, so no script's own
  // handler — copy, run, open in the editor — ever sees it.
  document.addEventListener('click', function (ev) {
    var el = localOnly(ev.target);
    if (!el) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    if (window.HR.flash) window.HR.flash(localHint(el));
  }, true);

  // A source reference is the one local-only control with a twin online: the same file on
  // GitHub, at the commit this page quotes (`data-hr-blob`, stamped by the build). Only a
  // file inside the checkout and outside the review's own folder, which is not committed;
  // anything else keeps its `vscode:` href and is greyed with the rest.
  var XLINK = 'http://www.w3.org/1999/xlink';
  function githubLinks(root) {
    var st = ROOT.dataset, blob = st.hrBlob, home = (st.hrRoot || '').replace(/\/+$/, '');
    var own = st.hrDir ? st.hrDir + '/' : null;
    [].forEach.call(root.querySelectorAll('a[href^="vscode:"]'), function (a) {
      var m = /^vscode:\/\/file\/*(\/[^:]*?)(?::(\d+))?(?::\d+)?$/
        .exec(decodeURIComponent(a.getAttribute('href')));
      var rel = m && home && m[1].indexOf(home + '/') === 0 ? m[1].slice(home.length + 1) : '';
      // "in VS Code" on its face: a link about the editor, not about the file — its "on
      // GitHub" twin is already beside it.
      if (!blob || !rel || rel.indexOf('.human-review/') === 0 || (own && rel.indexOf(own) === 0)
          || a.querySelector('.ide-word')) return;
      var line = m[2] || '1';
      var end = window.HR.endOf ? window.HR.endOf(a, line) : null;
      var url = blob + '/' + rel.split('/').map(encodeURIComponent).join('/')
        + (line !== '1' || end ? '#L' + line + (end ? '-L' + end : '') : '');
      a.setAttribute('href', url);
      if (a.hasAttributeNS(XLINK, 'href')) a.setAttributeNS(XLINK, 'xlink:href', url);
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener');
      ['data-tip', 'aria-label'].forEach(function (k) {
        var t = a.getAttribute(k);
        if (!t) return;
        a.setAttribute(k, /^Click to open in (the )?editor$/.test(t) ? 'Open on GitHub'
          : t.replace(/ in (VS Code|the editor)\b/, ' on GitHub'));
      });
    });
  }

  onready(function (j) {
    if (j || FILE) return;
    function settle() {
      githubLinks(document);
      [].forEach.call(document.querySelectorAll(LOCAL), function (el) {
        el.setAttribute('aria-disabled', 'true');
      });
      // The footer's "Browse: demo 1 · demo 2", read on one of those pages: the
      // link to itself becomes its plain name, the other demo stays a link.
      [].forEach.call(document.querySelectorAll('.takeaway-online a[href]'), function (a) {
        var to = new URL(a.href, location.href);
        if (to.host === location.host && to.pathname === location.pathname) {
          var here = document.createElement('span');
          here.textContent = a.textContent;
          here.setAttribute('aria-current', 'page');
          a.replaceWith(here);
        }
      });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', settle);
    else settle();
  });

  // A review card whose PR thread was resolved on GitHub — in the browser, or from an
  // IDE's PR panel — is struck through here, and each pile's count gets the struck number
  // in front of it: `~~2~~/9 open` (Victor, 20 Oct 2026). Asked every 5 s while the tab
  // is in front, so a thread resolved during a demo shows up while the audience watches.
  // Links are looked up on every pass: an in-place catch-up swaps the cards.
  onready(function (j) {
    if (!j || !j.prThreads) return;
    function pileOf(li) {
      return li.classList.contains('n-assumed') ? 'assumed'
        : li.classList.contains('fixed') ? 'fixed' : 'open';
    }
    function mark(el, n) {
      var old = el.querySelector(':scope > .gh-res-n');
      if (old) old.remove();
      if (!n) return;
      var s = document.createElement('span');
      s.className = 'gh-res-n';
      s.setAttribute('data-tip', n + ' of these resolved on GitHub');
      s.innerHTML = '<s>' + n + '</s>/';
      el.insertBefore(s, el.firstChild);
    }
    function paint(ids) {
      var got = {}, seen = {}, n = {open: 0, fixed: 0, assumed: 0};
      ids.forEach(function (i) { got[String(i)] = true; });
      [].forEach.call(document.querySelectorAll('a.f-gh[href*="#discussion_r"]'), function (a) {
        var li = a.closest('li'), id = (a.href.match(/discussion_r(\d+)/) || [])[1];
        if (!li || !id) return;
        var on = !!got[id];
        li.classList.toggle('gh-resolved', on);
        if (on && !seen[id]) { seen[id] = true; n[pileOf(li)]++; }
      });
      var lede = {assumed: '#assumed', open: '#first', fixed: '#fixed'};
      Object.keys(lede).forEach(function (k) {
        [].forEach.call(document.querySelectorAll('.pilelede a[href="' + lede[k] + '"]'),
          function (a) { mark(a, n[k]); });
      });
      [].forEach.call(document.querySelectorAll('.chip-link[href="#review"] b'), function (b) {
        var t = b.textContent;
        var k = /assumption/.test(t) ? 'assumed' : /open/.test(t) ? 'open' : /fixed/.test(t) ? 'fixed' : '';
        if (k) mark(b, n[k]);
      });
    }
    var misses = 0;
    (function next(delay) {
      setTimeout(function () {
        if (document.hidden) { next(5000); return; }
        fetch('/__pr_threads__', {cache: 'no-store'})
          .then(function (r) { if (!r.ok) throw new Error('refused'); return r.json(); })
          .then(function (t) { misses = 0; if (t && t.resolved) paint(t.resolved); next(5000); })
          .catch(function () { if (++misses < 3) next(5000); });
      }, delay);
    })(0);
  });

  // `follow`: the poller, for a page that loads while a run is already going -- it has
  // the run's id from `status()` and needs the same tail, at the same cadence, to
  // the same end.
  return {ready: ready, can: can, onready: onready, run: run, rerun: rerun, follow: poll,
          rerunAi: rerunAi, tail: tail, copy: copy, keepPlace: keepPlace,
          status: status, caps: function () { return caps; },
          adoptStamp: adoptStamp, quiet: quiet, stampNow: stampNow, afterRebuild: afterRebuild,
          localOnly: localOnly, localHint: localHint};
})();
