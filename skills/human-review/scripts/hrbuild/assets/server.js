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

  // `file:` and nothing else. The *general* protocol check is the bug this whole probe
  // replaced — the demo on GitHub Pages is https, so `location.protocol === 'http:'` said
  // "not served" about a page that was, and `=== 'https:'` would say "served" about one
  // that is not. `file:` is different in kind: a page read off disk cannot have been
  // served by us under any circumstances, and fetching a relative URL from it is not a
  // failed probe, it is a request the browser refuses and logs as an error in the console
  // of every reader who opens the guide off disk. Skipping it is the difference between a
  // clean console and one red line that means nothing.
  var ready = (location.protocol === 'file:' ? Promise.resolve(null)
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
      waiting.splice(0).forEach(function (fn) { try { fn(j); } catch (e) {} });
      return j;
    });

  // Two ids are not manifest entries and never can be: the two reruns are the server's own
  // commands, answered by the probe rather than declared by a build (see `rerun` below).
  // They are spelt like actions anyway so that every control on the page — the play mark
  // beside a printed command included — asks one question and gets one answer, instead of
  // each caller growing its own special case for the two verbs that are not in the list.
  var OWN = {'__rerun__': 'rerun', '__rerun_ai__': 'rerunAi'};

  function can(id) {
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
    if (!can(id)) return Promise.reject(new Error(id + ' is not available here'));
    // The two server-owned verbs route to their own endpoints. Here rather than in every
    // caller: `run(id)` is what the whole page reaches for, and a play mark beside the
    // refresh command that had to know it was special would be the one control on the page
    // whose wiring depended on which command it was printing.
    // `params.tab` narrows either one to a tab's own producers (the ↻ beside its pill).
    if (id === '__rerun__') return rerun(onprogress, params && params.tab);
    if (id === '__rerun_ai__') return rerunAi(onprogress, params && params.tab);
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
  function rerun(onprogress, tab) {
    return ask('/__rerun__', 'rerun', onprogress, tab);
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

  function ask(route, capability, onprogress, tab) {
    if (!caps || !caps[capability]) {
      return Promise.reject(new Error('this page cannot rebuild itself here'));
    }
    return fetch(route, {
      method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json',
                'X-Human-Review-Token': (caps && caps.token) || ''},
      body: tab ? JSON.stringify({tab: tab}) : '{}'
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
  onready(function (j) {
    if (!j || !j.watch) return;
    var seen = j.watch, misses = 0;
    (function next() {
      // Slower when the tab is in the background: it will be reloaded before anyone
      // looks at it either way, and a dozen parked reports are a dozen pollers.
      setTimeout(function () {
        fetch('/__watch__', {cache: 'no-store'})
          .then(function (r) { if (!r.ok) throw new Error('refused'); return r.json(); })
          .then(function (w) {
            misses = 0;
            // `reload()` and not a cache-buster: the server sends no-store.
            if (w.stamp && w.stamp !== seen) { location.reload(); return; }
            next();
          })
          // The server is mortal by design — idle for `--idle-minutes` and it is gone,
          // under a tab that is still open. That is not an error to report, it is the
          // end of the poll: three tries so a blip does not end it, then silence.
          .catch(function () { if (++misses < 3) next(); });
      }, document.hidden ? 5000 : 1000);
    })();
  });

  onready(function (j) {
    if (!j) return;
    // A play mark in front of the tab's title, where the favicon already is. A reader
    // keeps several of these open — one per branch, a static copy of an old one beside a
    // live one — and the tab strip is where they pick between them, long before anything
    // in the page is on screen. The badge in the title row says the same thing, but only
    // to someone already looking at the page.
    //
    // Play and not a green dot: green on this page means a check passed, and a report
    // whose tab turns green when a server happens to be up would be saying the branch is
    // fine. This says one thing only — something is running behind it.
    if (document.title.indexOf('▶') !== 0) {
      document.title = '▶️ ' + document.title;
    }
    // Nothing left to copy: the Serve line is the one that got the reader here.
    var serveChip = document.getElementById('hr-serve');
    if (serveChip) serveChip.hidden = true;
    var chip = document.getElementById('hr-mode');
    if (!chip) return;
    chip.textContent = 'served';
    chip.classList.add('chip-served');
    chip.setAttribute('data-tip', 'Served by the review server: commands run from this '
      + 'page, and recordings play in it.');
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
    // revert is the same control in a different place, and a selector naming one of the
    // two places is how the second one silently ships with both buttons on screen.
    [].forEach.call(document.querySelectorAll('button.runhere[data-action]'),
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
  });

  // `follow`: the poller, for a page that loads while a run is already going -- it has
  // the run's id from `status()` and needs the same tail, at the same cadence, to
  // the same end.
  return {ready: ready, can: can, onready: onready, run: run, rerun: rerun, follow: poll,
          rerunAi: rerunAi, tail: tail, copy: copy, keepPlace: keepPlace,
          status: status, caps: function () { return caps; }};
})();
