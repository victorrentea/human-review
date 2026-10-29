// A package's or a Maven module's box on the Structure tab, as a way into that folder.
//
// The build stamps each box it could resolve with `data-folder` — the folder, relative to
// the checkout — and, where the checkout has a github.com remote, wraps its shapes in a
// link to that folder at the reviewed commit. That link is the whole story off disk and
// on the published demo. Served, the click is better spent in the editor the reader
// already has open: the server finds the VS Code window that has this checkout, shows the
// folder selected in its Explorer and raises it — or IntelliJ's, in its Project view, when
// that is the IDE picked in the masthead (`HR.ide`, editor.js). The path goes over relative, and the
// server refuses anything that is not a folder inside its own checkout.
//
// Like every served control here it starts as the static page and rises when the probe
// answers — and only when the probe says `revealFolder`, because a server started before
// `/__reveal_folder__` existed would answer the click with a bare 404.
(function () {
  window.HR.onready(function (caps) {
    if (!caps || !caps.revealFolder) return;
    var flash = window.HR.flash || function () {};
    wire(document);
    // And the boxes of a panel a rebuild swaps in (`hr:swapped`, panels.js).
    document.addEventListener('hr:swapped', function (ev) {
      if (ev.detail && ev.detail.querySelectorAll) wire(ev.detail);
    });
    function wire(root) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-folder]'), function (box) {
      var name = box.getAttribute('data-folder-name') || box.getAttribute('data-folder');
      // One tip per box, the one that says what this click does now.
      var link = box.querySelector('a');
      if (link) link.removeAttribute('data-tip');
      box.setAttribute('role', 'button');
      box.setAttribute('tabindex', '0');
      // Worded for the IDE picked in the masthead, and again whenever the pick changes
      // (`hr-ide`, editor.js): a screen reader hears the editor the click will reach.
      function label() {
        var said = 'Reveal ' + name + ' in ' + (window.HR.ideName ? window.HR.ideName() : 'VS Code');
        box.setAttribute('aria-label', said);
        box.setAttribute('data-tip', said);
      }
      label();
      document.addEventListener('hr-ide', label);
      function reveal(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        // Both logos in the masthead red: no IDE is connected, so none is raised.
        if (window.HR.ideOff && window.HR.ideOff()) {
          flash('No IDE connected \u2014 click a logo at the top to connect one');
          return;
        }
        fetch('/__reveal_folder__', {
          method: 'POST', cache: 'no-store',
          headers: {'Content-Type': 'application/json',
                    'X-Human-Review-Token': caps.token || ''},
          body: JSON.stringify({path: box.getAttribute('data-folder'),
                                ide: window.HR.ide ? window.HR.ide() : null})
        }).then(function (r) {
          if (!r.ok) return r.text().then(function (t) { flash(t || 'the review server refused'); });
          return r.json().then(function (j) {
            var ed = window.HR.ideName ? window.HR.ideName(j.ide) : 'VS Code';
            flash(j.how === 'revealed'
              ? 'Showing ' + name + ' in ' + ed + (j.window ? ' (' + j.window + ')' : '')
              : ed + ' is in front, but its Human Review ' + (j.ide === 'idea' ? 'plugin' : 'extension')
                + ' is too old to reveal a folder: update it and Reload Window');
          });
        }).catch(function () { flash('the review server is no longer running'); });
      }
      box.addEventListener('click', reveal);
      box.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') reveal(ev);
      });
    });
    }
  });
})();

// "Show only impacted" (`folders.ONLY_IMPACTED`), in a card's bottom row: hides the boxes the build
// drew quiet (`data-quiet`: nothing of the branch in them) and every arrow to or from one,
// in each view of the card at once. A quiet box a green or red arrow still points at stays,
// and so does that arrow: the filter hides context, never the change itself. Static page
// or served alike, so it does not wait for the probe; delegated, so a swapped-in panel
// needs no wiring.
(function () {
  function changed(link) {
    return Array.prototype.some.call(link.querySelectorAll('path, polygon'), function (el) {
      return /--dgm-diff-(add|del)|#2E7D32|#C62828/i.test(
        (el.getAttribute('style') || '') + (el.getAttribute('stroke') || ''));
    });
  }
  function mark(svg) {
    var links = svg.querySelectorAll('g.link[data-entity-1][data-entity-2]');
    var keep = {};
    Array.prototype.forEach.call(links, function (l) {
      if (changed(l)) keep[l.getAttribute('data-entity-1')] = keep[l.getAttribute('data-entity-2')] = 1;
    });
    var off = {};
    Array.prototype.forEach.call(svg.querySelectorAll('g.entity[data-quiet]'), function (box) {
      if (keep[box.id]) return;
      off[box.id] = 1;
      box.setAttribute('data-only-off', '');
    });
    Array.prototype.forEach.call(links, function (l) {
      if (off[l.getAttribute('data-entity-1')] || off[l.getAttribute('data-entity-2')]) {
        l.setAttribute('data-only-off', '');
      }
    });
  }
  document.addEventListener('change', function (ev) {
    var box = ev.target.closest && ev.target.closest('.dgm-only input');
    var card = box && box.closest('.diagram');
    if (!card) return;
    if (box.checked) card.querySelectorAll('.svgbox svg').forEach(function (svg) {
      if (!svg.hasAttribute('data-only-marked')) { mark(svg); svg.setAttribute('data-only-marked', ''); }
    });
    card.classList.toggle('dgm-only-on', box.checked);
  });
})();
