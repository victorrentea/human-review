// Shift+wheel scrolls sideways. Chrome on macOS hands a shifted wheel to the page as an
// ordinary vertical scroll, so a line that runs past the right edge of a `pre.code` — a
// long Gherkin step, a wide diagram — can only be reached by dragging its scrollbar,
// the one gesture that makes the reader leave the line they were reading. We walk up
// from whatever is under the cursor to the first box that can actually move
// horizontally and move it ourselves. If nothing under the cursor can, or it is already
// at the end, the event is left alone and the page scrolls as it always did.
(function () {
  function scroller(el) {
    for (; el && el !== document.body; el = el.parentElement) {
      if (el.scrollWidth <= el.clientWidth + 1) continue;
      var ox = getComputedStyle(el).overflowX;
      if (ox === 'auto' || ox === 'scroll') return el;
    }
    return null;
  }
  window.addEventListener('wheel', function (ev) {
    if (!ev.shiftKey) return;
    // Some pointing devices already report a shifted wheel on the X axis: take whichever
    // axis actually moved, so both kinds of input travel the same distance.
    var delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
    if (!delta) return;
    if (ev.deltaMode === 1) delta *= 16;                 // lines, not pixels
    else if (ev.deltaMode === 2) delta *= 320;           // pages
    var box = scroller(ev.target instanceof Element ? ev.target : null);
    if (!box) return;
    var before = box.scrollLeft;
    box.scrollLeft = before + delta;
    // Only claim the gesture if it moved something; at either end it falls back to the
    // page, so a reader who keeps scrolling past the last column is not stuck.
    if (box.scrollLeft !== before) ev.preventDefault();
  }, { passive: false });
})();
