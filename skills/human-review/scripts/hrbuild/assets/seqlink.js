// The 🕵️ on a covering-tests row whose test drew a sequence: the way from the test to
// the picture of what its run actually did, over on the Sequence tab.
//
// The 📺 beside it opens the same run as a Playwright recording — frame by frame, from
// the browser's side. This opens the other account of it: the calls the run made, in
// order, across the stack. Two doors out of one row, which is why they look alike.
//
// Same shape as TRACE_JS, and for the same reason: the map's rows are drawn by the
// requirements map's own inline script, which knows nothing about diagrams and should
// not have to. The pairing is done from the outside, after the map has drawn, off a
// registry `render_testpairs` writes while it renders the pairs — so a row can only
// ever link to a pair that is really on the page.
(function () {
  var el = document.getElementById('hr-genseq');
  if (!el) return;
  var reg; try { reg = JSON.parse(el.textContent); } catch (e) { return; }
  var byKey = {}, byName = {}, dup = {};
  reg.forEach(function (e) {
    if (!byKey[e.test]) byKey[e.test] = e;
    // The map addresses a row by repo-relative path, and so does the registry, so the
    // lookup above is the one that fires. The basename is a fallback for a map that
    // addresses rows the short way — taken only while it is unambiguous, because two
    // `add-visit.feature` under different modules are two different tests.
    var name = e.test.split('/').pop();
    if (byName[name] && byName[name].pair !== e.pair) dup[name] = true;
    else byName[name] = e;
  });

  function lookup(id) {
    if (byKey[id]) return byKey[id];
    var name = id.split('/').pop();
    return dup[name] ? null : byName[name];
  }

  // Everything the click has to do that a plain hash link would do for us, minus the one
  // thing it cannot: the row it sits on toggles open on a click anywhere that is not the
  // editor link, so the event has to stop here — and stopping it also keeps it from
  // reaching the tab strip's own document-level handler for links into another panel.
  function jump(pair) {
    var target = document.getElementById(pair);
    if (!target) return;
    var panel = target.closest && target.closest('.panel');
    if (panel && !document.body.classList.contains('showall')) {
      var tab = document.querySelector('.tabstrip button.tab[aria-controls="' + panel.id + '"]');
      if (tab && tab.getAttribute('aria-selected') !== 'true') tab.click();
    }
    // A pair the reader folded away earlier is still the answer to this click.
    if (target.tagName === 'DETAILS') target.open = true;
    // After the tab has painted: scrolling to a panel that is still `hidden` measures
    // nothing and lands at the top of the page.
    requestAnimationFrame(function () {
      target.scrollIntoView({block: 'start'});
      target.classList.remove('seq-hit');
      void target.offsetWidth;            // restart the flash on a second click
      target.classList.add('seq-hit');
      if (history.replaceState) history.replaceState(null, '', '#' + pair);
    });
  }

  function decorate() {
    var rows = document.querySelectorAll('.rm-t[data-id]');
    Array.prototype.forEach.call(rows, function (row) {
      if (row.querySelector('.rm-seq')) return;
      var entry = lookup(row.getAttribute('data-id') || '');
      if (!entry) return;
      var where = row.querySelector('.rm-tw');
      if (!where) return;
      var a = document.createElement('a');
      a.className = 'rm-seq';
      a.textContent = '\uD83D\uDD75\uFE0F';
      a.href = '#' + entry.pair;
      a.setAttribute('aria-label', 'open the sequence this test drew');
      a.setAttribute('data-tip', 'Trace it: the calls this test made, on the Sequence tab');
      a.addEventListener('click', function (ev) {
        ev.preventDefault(); ev.stopPropagation();
        jump(entry.pair);
      });
      where.parentNode.insertBefore(a, where);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', decorate);
  else decorate();
})();
