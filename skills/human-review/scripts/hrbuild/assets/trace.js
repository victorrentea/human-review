// The 📺 on a covering-tests row whose test was recorded: the way into the recording.
//
// Served, it is a link to the Playwright trace viewer copied beside this page, opened in
// a window of its own — the viewer is a three-pane application and gets the whole
// screen, where the review page keeps its place in this one. The viewer reads the
// recording with `fetch`, so a page opened off disk — out of the downloadable zip, or
// straight from `.human-review/` — has nothing to hand it, not even a file sitting
// beside it. There the 📺 copies the line that opens the same recording natively.
//
// The registry is written per build by `render_traces`; the map's rows are drawn by its
// own inline script and do not know what the run recorded, and should not have to. So
// the pairing is done here, from the outside, after the map has drawn.
(function () {
  var el = document.getElementById('hr-traces');
  if (!el) return;
  var reg; try { reg = JSON.parse(el.textContent); } catch (e) { return; }
  var byKey = {};
  (reg.tests || []).forEach(function (t) { byKey[t.test] = t; });
  var served = !!reg.viewer && location.protocol !== 'file:';

  function decorate() {
    var rows = document.querySelectorAll('.rm-t[data-id]');
    Array.prototype.forEach.call(rows, function (row) {
      if (row.querySelector('.rm-tv')) return;
      var id = row.getAttribute('data-id') || '';
      var m = /^(.*?)(?::(\d+))?$/.exec(id);
      var key = (m[1] || '').split('/').pop() + (m[2] ? ':' + m[2] : '');
      var t = byKey[key];
      if (!t) return;
      var where = row.querySelector('.rm-tw');
      if (!where) return;
      var tv = document.createElement('a');
      tv.className = 'rm-tv';
      tv.textContent = '📺';
      // Said once, by the branch that is actually taken. The label used to be written
      // before the fork and claimed "open the recording of this test" in both, so off
      // disk a screen reader announced an open over a control that copies — the one
      // thing an aria-label must never get wrong, because it is the only description
      // that reader gets.
      if (served) {
        tv.setAttribute('aria-label', 'open the recording of this test');
        // Absolute, because the viewer resolves `?trace=` against its own document and
        // not against ours: a relative path would be looked for inside the viewer's folder.
        tv.href = reg.viewer + '?trace=' + encodeURIComponent(new URL(t.trace, location.href).href);
        tv.target = '_blank'; tv.rel = 'noopener';
        tv.setAttribute('data-tip', 'Open test replay in a new window');
        tv.addEventListener('click', function (ev) { ev.stopPropagation(); });
      } else {
        tv.href = '#';
        tv.setAttribute('aria-label', 'copy the command that opens the recording of this test');
        tv.setAttribute('data-tip', 'Recorded. Copy the command that opens the replay natively'
          + (reg.viewer ? ' \u2014 or serve this page (scripts/serve-review.py) to open it from here' : ''));
        tv.addEventListener('click', function (ev) {
          ev.preventDefault(); ev.stopPropagation();
          // The page's own clipboard and the page's own toast. This used to be a third
          // `copy()` and a two-second swap of `data-tip` \u2014 which rewrites an attribute
          // the tooltip has already rendered, so nothing appeared and the only silent
          // copy control on the page was this one. Every other one says it in the toast.
          window.HR.copy(t.cmd).then(function () {
            window.HR.flash('Copied \u2014 paste it in a terminal');
          });
        });
      }
      where.parentNode.insertBefore(tv, where);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', decorate);
  else decorate();
})();
