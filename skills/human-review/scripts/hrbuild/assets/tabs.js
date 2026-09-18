// The tab strip. Runs *last* on purpose: every panel is in the document and visible
// while the earlier scripts measure it, because getBBox() on anything inside a
// display:none subtree returns zeros — which would silently cost every sequence
// diagram its click targets. This script is what hides them, after the measuring.
(function () {
  var strip = document.querySelector('.tabstrip');
  if (!strip) return;
  var tabs = Array.prototype.slice.call(strip.querySelectorAll('button.tab'));
  var panels = tabs.map(function (t) { return document.getElementById(t.getAttribute('aria-controls')); });
  // Outside the strip now — at the foot of the page — so it is looked up on the document.
  var showAll = document.querySelector('button.allbtn');
  var active = 0;
  // What is actually stuck to the top of the viewport. The strip travels inside the
  // masthead, so the block that pins -- and whose height a deep link has to clear -- is
  // the masthead, not the strip inside it. A page built without one falls back to the
  // strip, which is what every measurement below used to be about.
  var sticky = strip.closest('.masthead') || strip;

  // How far a deep link has to clear the sticky strip is the strip's rendered height,
  // and that is not a constant: the pills wrap to a second row as soon as they outgrow
  // the track, which happens on a narrow window and happened for good the day the
  // thirteenth tab landed. Publish the measurement as `--strip-h` and let the CSS do
  // the arithmetic, so one row, two rows and whatever the next tab does are all correct
  // without anyone editing a number.
  function syncStripHeight() {
    var h = sticky.getBoundingClientRect().height;
    if (h > 0) document.documentElement.style.setProperty('--strip-h', h + 'px');
    // The counts line pins under the masthead, so a deep link has to clear both. Same
    // reasoning, same trick: publish the measurement, let the CSS add them up.
    var lede = document.querySelector('.panel .counts.pilelede');
    if (lede) {
      var lh = lede.getBoundingClientRect().height;
      if (lh > 0) document.documentElement.style.setProperty('--lede-h', lh + 'px');
    }
  }
  syncStripHeight();
  if (window.ResizeObserver) new ResizeObserver(syncStripHeight).observe(sticky);
  else window.addEventListener('resize', syncStripHeight);

  // `position:sticky` gives no state to style against: the strip looks identical whether
  // it is sitting in the masthead or pinned over the text. Compare its rendered top with
  // where it would sit unpinned -- offsetTop is relative to `.wrap`, which is static, so
  // the difference IS the scroll the strip has absorbed. Marks the pinned state so the
  // stylesheet can put an edge under it; nothing here measures or sets a height.
  function syncPinned() {
    var pinned = sticky.getBoundingClientRect().top <= 0.5;
    sticky.classList.toggle('pinned', pinned);
  }
  syncPinned();
  window.addEventListener('scroll', syncPinned, {passive: true});
  window.addEventListener('resize', syncPinned);

  function paint() {
    var all = document.body.classList.contains('showall');
    tabs.forEach(function (t, i) {
      // In show-all there is no selected tab: leaving one lit makes the strip claim a
      // filter is applied while every panel is on screen.
      t.setAttribute('aria-selected', String(!all && i === active));
      t.tabIndex = i === active ? 0 : -1;
      if (panels[i]) panels[i].hidden = !all && i !== active;
    });
    if (showAll) {
      showAll.setAttribute('aria-pressed', String(all));
      var label = showAll.getAttribute(all ? 'data-label-on' : 'data-label-off');
      if (label) showAll.textContent = label;
    }
  }

  // A panel holding live media has to know when it comes and when it goes — the Video
  // panel starts its narration on the way in and pauses it on the way out, because a
  // voice-over playing under a panel nobody is looking at is a bug, not a feature.
  // In show-all no panel is *the* active one, so every panel counts as off and nothing
  // starts talking while the reader is somewhere else on the page.
  function announce() {
    var all = document.body.classList.contains('showall');
    panels.forEach(function (p, i) {
      if (!p) return;
      var on = !all && i === active;
      if (p.__panelOn === on) return;
      p.__panelOn = on;
      p.dispatchEvent(new CustomEvent(on ? 'panelshow' : 'panelhide'));
    });
  }

  // The hash is the shareable handle: a reviewer sends "look at #api" and it opens there.
  // replaceState rather than location.hash, which would scroll the page out from under
  // the click that caused it.
  function select(i, remember, keepScroll) {
    if (i < 0 || i >= tabs.length) return;
    active = i;
    paint();
    announce();
    // Panels differ in height by thousands of pixels, so keeping the scroll offset across
    // a tab change drops the reader at an arbitrary point in the new panel — usually its
    // tail. Clicking "Review" and landing in the middle of "already fixed for you" reads
    // as if those were the open findings. Deep links (keepScroll) still scroll to their
    // target, which is the whole point of a deep link.
    if (!keepScroll) {
      var top = sticky.getBoundingClientRect().top + window.pageYOffset - 8;
      window.scrollTo(0, Math.max(0, top));
    }
    if (remember && history.replaceState) {
      history.replaceState(null, '', '#' + tabs[i].getAttribute('aria-controls'));
    }
  }

  function panelIndexOf(node) {
    for (var i = 0; i < panels.length; i++) {
      if (panels[i] && panels[i].contains(node)) return i;
    }
    return -1;
  }

  tabs.forEach(function (t, i) {
    t.addEventListener('click', function () { select(i, true); });
  });

  strip.addEventListener('keydown', function (ev) {
    var step = ev.key === 'ArrowRight' ? 1 : ev.key === 'ArrowLeft' ? -1 : 0;
    if (!step) return;
    ev.preventDefault();
    var next = (active + step + tabs.length) % tabs.length;
    select(next, true);
    tabs[next].focus();
  });

  if (showAll) {
    showAll.addEventListener('click', function () {
      document.body.classList.toggle('showall');
      paint();
      announce();
      // The page just changed length by an order of magnitude; the old offset means nothing.
      window.scrollTo(0, 0);
    });
  }

  // A link into a section that lives on another tab has to switch tabs first, or it
  // scrolls to something the browser is not showing.
  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('a[href^="#"]');
    if (!link) return;
    var target = document.getElementById(decodeURIComponent(link.getAttribute('href').slice(1)));
    if (!target) return;
    var i = panelIndexOf(target);
    if (i >= 0 && i !== active) select(i, false, true);
  });

  // Opening on a deep link: the hash may name a tab, or anything inside one.
  var wanted = decodeURIComponent((location.hash || '').slice(1));
  var start = 0;
  if (wanted) {
    var byTab = tabs.findIndex(function (t) { return t.getAttribute('aria-controls') === wanted; });
    if (byTab >= 0) start = byTab;
    else {
      var node = document.getElementById(wanted);
      var inPanel = node ? panelIndexOf(node) : -1;
      if (inPanel >= 0) {
        start = inPanel;
        setTimeout(function () { node.scrollIntoView(); }, 0);
      }
    }
  }
  select(start, false, Boolean(wanted));
})();
