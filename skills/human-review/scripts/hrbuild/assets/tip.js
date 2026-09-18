// One tooltip for the whole page. The native `title` is unstyleable, unresizable and
// waits ~500ms — long enough that a reviewer reads the icon, gives up, and moves on.
// Listeners are delegated on `document` so markup written later by any of the other
// scripts picks the behaviour up with no registration step.
(function () {
  var css = document.createElement('style');
  css.textContent =
    // 15px, the page's own body size, at normal weight. It was 1.05rem/600 -- a hint
    // set LARGER and heavier than the sentence it explains, which reads as the page
    // shouting an aside.
    '.tip{position:fixed;z-index:9999;pointer-events:none;background:rgba(20,20,22,.96);' +
    'color:#fff;font:400 15px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;' +
    'padding:.6rem .9rem;border-radius:.6rem;max-width:22rem;box-shadow:0 10px 30px rgba(0,0,0,.35);' +
    // A repo-relative path is one long token as far as line breaking is concerned: no
    // spaces, and a slash is not a break opportunity. So the longest tip on the page --
    // the one naming a Java file five packages deep -- overflowed the bubble and was
    // clipped at its edge, which reads as the page running off the screen. `anywhere`
    // gives the browser leave to break inside the token; the max-width then holds, and
    // `place()` can keep a box it has correctly measured on screen.
    'overflow-wrap:anywhere;' +
    'opacity:0;transform:translateY(4px);transition:opacity 120ms ease,transform 120ms ease}' +
    '.tip.visible{opacity:1;transform:translateY(0)}' +
    // `.oneline` is the first thing tried for a plain-text tip: the whole label on one
    // row, as wide as it needs to be, up to the viewport. The 22rem wrap above had been
    // folding "Open in VS Code: petclinic-test/src/add-visit.spec.ts" in the middle of
    // the file name -- and a path broken across rows is a path the reader cannot read
    // at a glance, which is the one job that tip has. `show()` drops the class again
    // when the row would not fit, so a paragraph-sized hint still wraps.
    '.tip.oneline{white-space:nowrap;max-width:calc(100vw - 16px)}' +
    // A tip that lists identifiers lists them: one per line, in code type, with a marker
    // -- not welded into a comma-separated sentence the reader has to parse to find out
    // whether their own library is in it. `.tipfoot` is for the sentence that genuinely
    // is one, set apart and quieter so the list stays the thing being read.
    '.tip ul.tiplist{margin:0;padding-left:1.1rem;list-style:disc;' +
    'font:400 13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}' +
    '.tip ul.tiplist li{margin:0}' +
    '.tip p.tipfoot{margin:.5rem 0 0;font-size:13px;opacity:.72}' +
    '.tip p.tipfoot:first-child{margin:0;opacity:1}' +
    // One convention for the pointer: a mark that only explains itself gets the question
    // mark, so hovering tells you there is something to read AND that clicking does
    // nothing. Anything you can act on -- a link, a button, a row that opens -- keeps
    // the hand it already had.
    //
    // The last clause is about marks that live *inside* something actionable, which the
    // element-level list above cannot see. The tab strip's badges are the case that found
    // it: `Tests 1` is a <span role=img> with its own tooltip sitting inside the tab
    // <button>, so the cursor turned into a question mark over the badge and back into a
    // hand a pixel to its left -- while a click anywhere in there, badge included, opens
    // the tab. Excluding them is the whole fix: `cursor` inherits, so a badge that gets no
    // rule of its own simply keeps the pointer its button already set.
    '[data-tip]:not(a):not(button):not([role=button]):not(summary):not(label)' +
    ':not(:is(a,button,[role=button],summary,label) *)' +
    '{cursor:help}';
  document.head.appendChild(css);

  var bubble = document.createElement('div');
  bubble.className = 'tip';
  bubble.setAttribute('role', 'tooltip');
  document.body.appendChild(bubble);
  var timer = null, current = null;

  function hide() {
    clearTimeout(timer);
    current = null;
    bubble.classList.remove('visible');
  }

  function place(el) {
    var r = el.getBoundingClientRect(), b = bubble.getBoundingClientRect(), left, top;
    // `data-tip-side="right"` is for a tip tall enough to be a panel rather than a
    // label: a stack of bullets floated above the phrase covers the sentence the reader
    // is in the middle of, and pushes the page's own content out of view. Beside it, the
    // sentence stays readable. Flips to the left margin when the right one is too narrow.
    if (el.getAttribute('data-tip-side') === 'right') {
      left = r.right + 12;
      if (left + b.width > window.innerWidth - 8) left = r.left - b.width - 12;
      top = r.top + r.height / 2 - b.height / 2;
    } else {
      left = r.left + r.width / 2 - b.width / 2;
      // Above by default; below when the top of the viewport is in the way.
      top = r.top - b.height - 10;
      if (top < 8) top = r.bottom + 10;
    }
    left = Math.max(8, Math.min(left, window.innerWidth - b.width - 8));
    top = Math.max(8, Math.min(top, window.innerHeight - b.height - 8));
    bubble.style.left = left + 'px';
    bubble.style.top = top + 'px';
  }

  function show(el) {
    // `data-tip-html` is for a tip that has to SHOW a component rather than name it -- a
    // coverage badge, say, where "UI x2" in prose makes the reader translate back to the
    // badge they are looking at. The markup is the page's own; nothing user-supplied
    // reaches here. Plain `data-tip` stays the default and stays escaped.
    var html = el.getAttribute('data-tip-html'), text = el.getAttribute('data-tip');
    if (!html && !text) return;              // data-tip="" shows nothing, by design
    current = el;
    if (html) bubble.innerHTML = html; else bubble.textContent = text;
    bubble.classList.remove('visible');
    // One row when the row fits the screen; otherwise back to the wrapping box. Measured,
    // not guessed from the character count: a path and a sentence of the same length are
    // very different widths. Markup tips (lists) always wrap.
    bubble.classList.toggle('oneline', !html);
    if (!html && bubble.scrollWidth > window.innerWidth - 16) bubble.classList.remove('oneline');
    place(el);
    timer = setTimeout(function () {
      if (current !== el) return;
      place(el);
      bubble.classList.add('visible');
    }, 150);
  }

  function trigger(ev) {
    var el = ev.target.closest && ev.target.closest('[data-tip],[data-tip-html]');
    if (!el || el === current) return;
    hide();
    show(el);
  }

  document.addEventListener('pointerover', trigger);
  document.addEventListener('focusin', trigger);   // focus/blur do not bubble
  document.addEventListener('pointerout', function (ev) {
    if (current && !current.contains(ev.relatedTarget)) hide();
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('touchstart', hide, {passive: true});
  window.addEventListener('scroll', hide, true);   // a fixed bubble would float away
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') hide(); });
})();
