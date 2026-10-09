// A package's or a Maven module's box on the Structure tab, as a way into that folder.
//
// The build stamps each box it could resolve with `data-folder` — the folder, relative to
// the checkout — and, where the checkout has a github.com remote, wraps its shapes in a
// link to that folder at the reviewed commit. That link is the whole story off disk and
// on the published demo. Served, the click is better spent in the editor the reader
// already has open: the server finds the VS Code window that has this checkout, shows the
// folder selected in its Explorer and raises it. The path goes over relative, and the
// server refuses anything that is not a folder inside its own checkout.
//
// Like every served control here it starts as the static page and rises when the probe
// answers — and only when the probe says `revealFolder`, because a server started before
// `/__reveal_folder__` existed would answer the click with a bare 404.
(function () {
  var boxes = document.querySelectorAll('[data-folder]');
  if (!boxes.length) return;
  window.HR.onready(function (caps) {
    if (!caps || !caps.revealFolder) return;
    var flash = window.HR.flash || function () {};
    Array.prototype.forEach.call(boxes, function (box) {
      var name = box.getAttribute('data-folder-name') || box.getAttribute('data-folder');
      // One tip per box, the one that says what this click does now.
      var link = box.querySelector('a');
      if (link) link.removeAttribute('data-tip');
      box.setAttribute('role', 'button');
      box.setAttribute('tabindex', '0');
      box.setAttribute('aria-label', 'Reveal ' + name + ' in VS Code');
      box.setAttribute('data-tip', 'Reveal ' + name + ' in VS Code');
      function reveal(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        fetch('/__reveal_folder__', {
          method: 'POST', cache: 'no-store',
          headers: {'Content-Type': 'application/json',
                    'X-Human-Review-Token': caps.token || ''},
          body: JSON.stringify({path: box.getAttribute('data-folder')})
        }).then(function (r) {
          if (!r.ok) return r.text().then(function (t) { flash(t || 'the review server refused'); });
          return r.json().then(function (j) {
            flash(j.how === 'revealed'
              ? 'Showing ' + name + ' in VS Code' + (j.window ? ' (' + j.window + ')' : '')
              : 'VS Code is in front, but its Human Review extension is too old to reveal a folder: '
                + 'update it and Reload Window');
          });
        }).catch(function () { flash('the review server is no longer running'); });
      }
      box.addEventListener('click', reveal);
      box.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') reveal(ev);
      });
    });
  });
})();
