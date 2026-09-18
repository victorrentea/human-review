// A framed report sizes itself: it posts its height and we grow the frame to fit, so
// the page keeps the only scrollbar. A frame that scrolls internally traps the wheel
// and hides how much of it is left.
var dvFrames = [];
window.addEventListener('message', function (e) {
  var d = e.data;
  if (!d || d.type !== 'dv-height' || !d.height) return;
  Array.prototype.forEach.call(document.querySelectorAll('iframe'), function (f) {
    if (f.contentWindow !== e.source) return;
    f.style.height = (d.height + 4) + 'px';
    if (dvFrames.indexOf(f) < 0) dvFrames.push(f);
  });
  dvStick();
});

// The price of that bargain: a frame grown to its full height has no scrollport, so a
// toolbar inside it cannot pin itself -- `sticky` never fires and `fixed` pins to the
// same full-height box. We are the document that scrolls, so we are the one that knows.
// Post how far our own pinned masthead has run past the top of each frame and let it
// slide its toolbar down by that much; the frame clamps the number to its own height.
// Only frames that have announced themselves with a `dv-height` are talked to, so an
// embedded report that knows nothing of this is never sent anything.
function dvStick() {
  if (!dvFrames.length) return;
  var top = parseFloat(getComputedStyle(document.documentElement)
                       .getPropertyValue('--strip-h')) || 0;
  dvFrames.forEach(function (f) {
    var r = f.getBoundingClientRect();
    // A frame in a hidden panel measures as nothing; there is nothing to pin over.
    if (!r.height) return;
    // `clientTop` is the frame's own top border: the rect is the border box, but the
    // offset we are posting is measured from inside it, and the one pixel between the
    // two is a pixel of the frame's content showing above a bar that looked flush.
    // Floored, not rounded, for the same reason -- at a fractional scroll position the
    // bar is better a hair under the masthead than a hair below it.
    var y = Math.floor(top - r.top - (f.clientTop || 0));
    f.contentWindow.postMessage({type: 'dv-stick', top: Math.max(0, y)}, '*');
  });
}
var dvStickQueued = false;
function dvQueueStick() {
  if (dvStickQueued) return;
  dvStickQueued = true;
  requestAnimationFrame(function () { dvStickQueued = false; dvStick(); });
}
window.addEventListener('scroll', dvQueueStick, {passive: true});
window.addEventListener('resize', dvQueueStick);
// Switching tabs, or opening `show all`, moves a frame without scrolling the page. The
// listener is on capture so it is queued before the tab handler runs; the frame is
// re-measured in the animation frame after, by which time the panel has swapped.
document.addEventListener('click', dvQueueStick, true);
