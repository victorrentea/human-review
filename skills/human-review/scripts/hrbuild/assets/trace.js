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

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    var ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.top = '-1000px';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    return Promise.resolve();
  }

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
      tv.setAttribute('aria-label', 'open the recording of this test');
      if (served) {
        // Absolute, because the viewer resolves `?trace=` against its own document and
        // not against ours: a relative path would be looked for inside the viewer's folder.
        tv.href = reg.viewer + '?trace=' + encodeURIComponent(new URL(t.trace, location.href).href);
        tv.target = '_blank'; tv.rel = 'noopener';
        tv.setAttribute('data-tip', 'Open test replay in a new window');
        tv.addEventListener('click', function (ev) { ev.stopPropagation(); });
      } else {
        tv.href = '#';
        tv.setAttribute('data-tip', 'Recorded. Copy the command that opens the replay natively'
          + (reg.viewer ? ' \u2014 or serve this page (scripts/serve-review.py) to open it from here' : ''));
        tv.addEventListener('click', function (ev) {
          ev.preventDefault(); ev.stopPropagation();
          copy(t.cmd).then(function () {
            var was = tv.getAttribute('data-tip');
            tv.setAttribute('data-tip', 'Copied \u2014 run it in a terminal');
            setTimeout(function () { tv.setAttribute('data-tip', was); }, 2000);
          });
        });
      }
      where.parentNode.insertBefore(tv, where);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', decorate);
  else decorate();
})();
