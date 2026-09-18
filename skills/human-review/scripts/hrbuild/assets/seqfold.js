// The Sequence tab opens on its table of contents: every test pair folded shut, so the
// tab's first screen is one line per test instead of the top of whichever sequence happens
// to be first. A sequence is three or four screens of arrows; four of them stacked is a
// wall the reader has to scroll past to find out what is on the tab at all.
//
// Why here and not `<details>` without `open` in the markup, which is the obvious way: the
// click targets inside every diagram are transparent rects sized from `getBBox()`, and
// `getBBox()` on anything inside a `display:none` subtree returns zeros. A pair born shut
// would cost its sequence every handle on it, silently. So the pairs are born open, every
// script that measures them runs, and the folding happens after — the same bargain TABS_JS
// makes with the panels, for the same reason, and this runs before it for the same one.
//
// A reader arriving on a deep link is the exception: the pair the link names is what they
// asked for, and it stays open.
//
// Which pair is open is in the URL, both ways round. A reader who opens one and sends the
// address sends the picture they are looking at, not the tab it is on — the same handle
// the 🕵️ on the Tests tab already jumps through. `replaceState`, not `location.hash`,
// because assigning the hash scrolls the page out from under the click that caused it.
(function () {
  var wanted = decodeURIComponent((location.hash || '').slice(1));
  // Not scoped to a panel id: the tab this block lands on is named by the content file.
  document.querySelectorAll('details.testpair[open]').forEach(function (pair) {
    if (pair.id && pair.id === wanted) return;
    pair.open = false;
  });

  function remember(id) {
    if (history.replaceState) history.replaceState(null, '', '#' + id);
    else location.hash = id;
  }

  document.querySelectorAll('details.testpair[id]').forEach(function (pair) {
    // `toggle` fires asynchronously, so the folding above arrives here too — harmlessly:
    // a pair being shut only rewrites the hash when the hash is naming that very pair,
    // which at load is true of the one pair this loop leaves open.
    pair.addEventListener('toggle', function () {
      var hash = decodeURIComponent((location.hash || '').slice(1));
      if (pair.open) remember(pair.id);
      else if (hash === pair.id) {
        var panel = pair.closest('.panel');
        remember(panel && panel.id ? panel.id : '');
      }
    });
  });

  // The fold's row carries the quoted test's source bar, and those are links: to VS Code,
  // to the compare page on github.com. A click on one is also a click inside a <summary>,
  // which a browser may read as a request to toggle the fold — so the reader would open
  // the file AND have the block they were reading fold away under them.
  //
  // The fold is put back rather than the click stopped. `stopPropagation` is the usual
  // remedy and it is wrong here: the handler that turns a `vscode://` reference into an
  // *open diff* is a listener on `document`, so silencing the event on its way there would
  // trade one bug for a better-hidden one. This runs after the browser's own activation
  // behaviour and before the next paint, so a fold that never moved is left alone and one
  // that did is put back with nothing drawn in between.
  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('details.testsrc > summary a');
    if (!link) return;
    var fold = link.closest('details.testsrc');
    var was = fold.open;
    requestAnimationFrame(function () { if (fold.open !== was) fold.open = was; });
  }, true);
})();
