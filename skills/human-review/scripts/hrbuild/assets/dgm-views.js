// Diff / New / Old. Delegated on `document` rather than bound per widget, so a
// `.dgmviews` that reaches a section body some other way — expanded from `{{drawio:...}}`,
// or written by hand — picks up the identical behaviour with no registration step.
// The swap is animated by focus.js (draw.io and PlantUML cards), which watches `data-state` here:
// this file only flips the panes, and must keep flipping them through that attribute.
//
// A sequence beside its test has no Diff, and its New/Old buttons are not in the card: they
// ride in the test's row (`details.testpair > summary`, sequence.py `_lang_label`), a bar of
// their own with a `data-state` the stylesheet paints from. So the buttons a widget answers
// to are looked up from its pair when it has one, else from its card.
(function () {
  function scope(views) {
    return views.closest('details.testpair') || views.closest('.diagram') || views;
  }
  function show(views, state) {
    views.setAttribute('data-state', state);
    views.querySelectorAll(':scope > .dgmpane').forEach(function (pane) {
      pane.hidden = pane.getAttribute('data-view') !== state;
    });
    var around = scope(views);
    around.querySelectorAll('.dgm-newold u[data-view]').forEach(function (word) {
      word.classList.toggle('on', word.getAttribute('data-view') === state);
    });
    around.querySelectorAll('.dgmbar button[data-go]').forEach(function (b) {
      var go = b.getAttribute('data-go');
      b.setAttribute('aria-pressed',
        String(go === 'diff' ? state === 'diff' : state !== 'diff'));
    });
    around.querySelectorAll('.dgmbar[data-state]').forEach(function (bar) {
      bar.setAttribute('data-state', state);
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
      // In a pair's row the buttons sit inside its <summary>: the press must not also
      // fold the pair shut under the picture it just swapped.
      if (button.closest('summary')) ev.preventDefault();
      var views = button.closest('.dgmviews')
        || (button.closest('details.testpair, .diagram') || document).querySelector('.dgmviews');
      // Three states, three targets: Diff, and each word of New/Old picks its own pane.
      // The words used to swap whatever was on, so a click on the lit "Old" went to New.
      var word = ev.target.closest('u[data-view]');
      if (button.getAttribute('data-go') === 'diff') show(views, 'diff');
      else if (word && views && views.querySelector(':scope > .dgmpane[data-view="'
                                                  + word.getAttribute('data-view') + '"]')) {
        show(views, word.getAttribute('data-view'));
      } else flip(views);
      return;
    }
    // The header, but never a link, a button or a checkbox inside it: the source path opens
    // an editor, and the card's action buttons (Edit, Revert) live in the header too, as
    // does its "Show only impacted" filter on a card with no pill row to move it to.
    var head = ev.target.closest('.diagram.dgm-toggles > .head');
    if (head && !ev.target.closest('a, button, label, input')) flip(head.parentElement.querySelector('.dgmviews'));
    // Same bargain for the button row: its empty middle is a larger hit area than the
    // pills, and a click on a link in it still opens the link rather than swapping the
    // picture out from under it.
    var bar = ev.target.closest('.diagram.dgm-toggles .dgmbar');
    if (bar && !ev.target.closest('a')) flip(bar.closest('.diagram').querySelector('.dgmviews'));
  });
})();
