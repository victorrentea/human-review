// A frozen header for sequence diagrams, like a frozen first row in a spreadsheet. The
// participants are drawn once, at the top, and a sequence is three or four screens tall:
// two screens down, a reader looking at an arrow into the third lifeline from the right
// has to scroll back up to learn whether that is DB or NotificationService. So once the
// row of participant boxes scrolls under the masthead, a copy of it is pinned there, and
// it stays until the diagram itself has scrolled away.
//
// A copy, not the row itself made sticky: the diagram is one SVG inside a box that
// scrolls sideways, and `position:sticky` inside a scroll container sticks to that
// container, not to the page. The copy is its own small SVG, cut from the same viewBox
// at the same scale and slid sideways by exactly as much as the diagram is, so every box
// sits over its own lifeline.
(function () {
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var PAD_TOP = 6, PAD_BOTTOM = 10;          // viewBox units: the boxes plus a lifeline stub
  var diagrams = [];
  function collect(root) {
    root.querySelectorAll('.diagram .svgbox svg').forEach(function (svg) {
      if (svg.querySelector('g.participant-head')) diagrams.push(svg);
    });
  }
  collect(document);
  // A rebuild that swaps a panel in place (`hr:swapped`, from panels.js) takes its diagrams
  // with it and brings new ones: drop the ones that left, take the ones that came. So no
  // early return on a page without any: a swap may bring the first one.
  document.addEventListener('hr:swapped', function (ev) {
    if (!ev.detail || !ev.detail.querySelectorAll) return;
    diagrams = diagrams.filter(function (svg) { return svg.isConnected; });
    collect(ev.detail);
    shown = null;
    schedule();
  });

  var strip = document.createElement('div');
  strip.id = 'seqheads';
  strip.hidden = true;
  document.body.appendChild(strip);
  var shown = null, queued = false;

  // Where the masthead ends. TABS_JS publishes its measured height, however many rows the
  // tab strip wraps onto.
  function pinTop() {
    return parseFloat(getComputedStyle(document.documentElement)
                        .getPropertyValue('--strip-h')) || 0;
  }

  // The band the heads occupy, in viewBox units. Measured on first use rather than up
  // front: SEQFOLD_JS shuts every test pair, and getBBox() inside a display:none subtree
  // is all zeros. By the time a diagram can be scrolled past, it is on screen.
  function band(svg) {
    if (svg._seqBand) return svg._seqBand;
    var top = Infinity, bottom = -Infinity;
    svg.querySelectorAll('g.participant-head').forEach(function (g) {
      var box = g.getBBox();
      if (!box.height) return;
      top = Math.min(top, box.y);
      bottom = Math.max(bottom, box.y + box.height);
    });
    if (!isFinite(top)) return null;
    svg._seqBand = {top: top - PAD_TOP, bottom: bottom + PAD_BOTTOM};
    return svg._seqBand;
  }

  function build(svg, b) {
    var vb = svg.viewBox.baseVal;
    var copy = document.createElementNS(SVG_NS, 'svg');
    copy.setAttribute('viewBox', vb.x + ' ' + b.top + ' ' + vb.width + ' ' + (b.bottom - b.top));
    copy.setAttribute('preserveAspectRatio', 'none');
    // Lifelines first, so the boxes are drawn over their tops exactly as in the original.
    svg.querySelectorAll('g.participant-lifeline, g.participant-head').forEach(function (g) {
      var clone = g.cloneNode(true);
      clone.removeAttribute('id');
      copy.appendChild(clone);
    });
    strip.textContent = '';
    strip.appendChild(copy);
  }

  function update() {
    queued = false;
    var top = pinTop(), pick = null;
    for (var i = 0; i < diagrams.length && !pick; i++) {
      var svg = diagrams[i], r = svg.getBoundingClientRect();
      if (!r.height || r.bottom < top || r.top > top) continue;
      // A folded test pair still has a full-height rect: Chrome keeps a closed <details>'
      // content laid out, only hidden. Without this, the fold above the one being read
      // spans the same line and its heads get pinned over someone else's lifelines.
      if (svg.closest('details:not([open])') || (svg.checkVisibility && !svg.checkVisibility())) continue;
      var b = band(svg), vb = svg.viewBox.baseVal;
      if (!b || !vb.width) continue;
      var scale = r.width / vb.width;
      var headsBottom = r.top + (b.bottom - PAD_BOTTOM - vb.y) * scale;
      var height = (b.bottom - b.top) * scale;
      // Pinned only while the real heads are out of sight and there is still diagram
      // below the copy for it to label.
      if (headsBottom < top && r.bottom > top + height) pick = {svg: svg, r: r, b: b, scale: scale, height: height};
    }
    if (!pick) { strip.hidden = true; return; }
    if (shown !== pick.svg) { build(pick.svg, pick.b); shown = pick.svg; }
    var box = pick.svg.closest('.svgbox').getBoundingClientRect();
    var copy = strip.firstChild;
    strip.style.top = top + 'px';
    strip.style.left = box.left + 'px';
    strip.style.width = box.width + 'px';
    strip.style.height = pick.height + 'px';
    copy.style.left = (pick.r.left - box.left) + 'px';
    copy.style.width = pick.r.width + 'px';
    copy.style.height = pick.height + 'px';
    strip.hidden = false;
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(update);
  }

  // Capture, so a diagram scrolled sideways inside its own box moves its copy too.
  document.addEventListener('scroll', schedule, {capture: true, passive: true});
  window.addEventListener('resize', schedule);
  // A tab switch, a pair folded open or shut, a New / Old toggle: each changes
  // which diagram is under the masthead without scrolling anything.
  document.addEventListener('click', schedule);
  document.addEventListener('toggle', schedule, true);
  schedule();
})();
