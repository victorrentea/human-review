// Diff / New / Old. Delegated on `document` rather than bound per widget, so a
// `.dgmviews` that reaches a section body some other way — expanded from `{{drawio:...}}`,
// or written by hand — picks up the identical behaviour with no registration step.
(function () {
  function show(views, state) {
    views.setAttribute('data-state', state);
    views.querySelectorAll(':scope > .dgmpane').forEach(function (pane) {
      pane.hidden = pane.getAttribute('data-view') !== state;
    });
    var pair = (views.closest('.diagram') || views).querySelector('.dgm-newold');
    if (pair) {
      pair.querySelectorAll('u[data-view]').forEach(function (word) {
        word.classList.toggle('on', word.getAttribute('data-view') === state);
      });
    }
    // From the card, not from `views`: on a paired diagram the bar has been lifted out
    // of `.dgmviews` and into the header's row (see the merge below).
    (views.closest('.diagram') || views).querySelectorAll('.dgmbar button[data-go]').forEach(function (b) {
      var go = b.getAttribute('data-go');
      b.setAttribute('aria-pressed',
        String(go === 'diff' ? state === 'diff' : state !== 'diff'));
    });
  }
  // From the delta, the first click lands on New; from New it lands on Old, and back.
  // Two words, one button, and the same answer whichever control you reached for.
  function flip(views) {
    if (!views) return;
    var has = function (v) { return !!views.querySelector(':scope > .dgmpane[data-view="' + v + '"]'); };
    var now = views.getAttribute('data-state');
    var next = now === 'new' ? 'old' : 'new';
    if (!has(next)) next = next === 'new' ? 'old' : 'new';
    if (has(next)) show(views, next);
  }
  document.addEventListener('click', function (ev) {
    var button = ev.target.closest('.dgmbar button[data-go]');
    if (button) {
      var views = button.closest('.dgmviews')
        || (button.closest('.diagram') || document).querySelector('.dgmviews');
      if (button.getAttribute('data-go') === 'diff') show(views, 'diff');
      else flip(views);
      return;
    }
    // The header, but never a link inside it: the source path opens an editor.
    var head = ev.target.closest('.diagram.dgm-toggles > .head');
    if (head && !ev.target.closest('a')) flip(head.parentElement.querySelector('.dgmviews'));
    // Same bargain for the merged row: its empty middle is the large hit area the header
    // used to be, and a click on the path still opens the editor rather than swapping the
    // picture out from under it.
    var bar = ev.target.closest('.diagram.dgm-toggles .dgmbar');
    if (bar && !ev.target.closest('a')) flip(bar.closest('.diagram').querySelector('.dgmviews'));
  });

  // The merge. Only on a paired card (`dgm-bare`), which is the one that lost its title
  // and was left with a header row holding a single right-aligned path — a whole row of
  // page for one filename. Elsewhere the header still carries a real heading and earns
  // its own line.
  //
  // The bar keeps its place inside `.dgmviews`, because the stylesheet paints the buttons
  // off `[data-state]` on that element; what travels is the header's contents. `show` and
  // the click handler are the two places that then have to look the bar up from the card
  // instead of from `views`, and they do.
  document.querySelectorAll('.diagram.dgm-bare > .head').forEach(function (head) {
    var bar = head.parentElement.querySelector('.dgmviews > .dgmbar');
    if (!bar) return;
    while (head.firstChild) bar.appendChild(head.firstChild);
  });
})();
