// The other half of `code_xref.py`. Every link on the page is already drawn and every
// window already knows its own id; what is left is the four things only a browser can do:
// fold a window, unfold it, scroll to it, and say which line the link was about.
//
// It is placed BEFORE the editor handler on purpose. Both listen for a click on an anchor
// whose href is `vscode://…`, and a cross-reference has one so that holding Cmd still
// opens the file in VS Code, which is the gesture a reader already has for "not here, in
// my editor". Registering first means this handler runs first, and calling preventDefault
// is what tells the editor handler (which checks `defaultPrevented`) to leave the plain
// click alone. Cmd-, Ctrl- and Shift-clicks fall through untouched to it, and to the
// browser behind it.
(function () {
  var node = document.getElementById('xref-index');
  var INDEX = {};
  try { INDEX = JSON.parse((node && node.textContent) || '{}'); } catch (e) { INDEX = {}; }

  function fold(part, shut) {
    part.classList.toggle('xr-shut', shut);
    var stub = part.querySelector('.xr-stub');
    if (!stub) return;
    stub.setAttribute('aria-expanded', String(!shut));
    // What it does, then what it does it to — and the second half is the whole first line,
    // which is the half the stub itself cannot always show: it sits in whatever the source
    // bar has left over, so a long signature is clipped at a width only the browser knows.
    // Reading the clipped end should not cost a click that changes the page.
    stub.setAttribute('data-tip', shut
      ? 'Unfold this excerpt: ' + (stub.dataset.face || '')
      : 'Fold this excerpt away');
  }

  // The requirements map builds a test's excerpts the first time its row is opened, so
  // there is nothing to fold at load: the parts arrive later, in a batch, and each one is
  // recognised by the editor link in its own source bar — the one thing the map already
  // emits that says exactly which window it is.
  function adopt(root) {
    if (!root.querySelectorAll) return;
    // The map sets `.rm-tinner`'s innerHTML in one go, so the nodes the observer hands
    // over ARE the parts — and `querySelectorAll` on an element never returns the element
    // itself. Looking only inside them found nothing, every time.
    var parts = Array.prototype.slice.call(root.querySelectorAll('.rm-part'));
    if (root.matches && root.matches('.rm-part')) parts.unshift(root);
    parts.forEach(function (part) {
      if (part.dataset.xrefId) return;
      var link = part.querySelector('a.srcref:not(.rm-diff)[href^="vscode:"]');
      var seen = link && INDEX[link.getAttribute('href')];
      if (!seen) { part.dataset.xrefId = ''; return; }
      part.dataset.xrefId = seen.id;
      if (!seen.shut) return;
      var stub = document.createElement('button');
      stub.type = 'button';
      stub.className = 'xr-stub';
      stub.dataset.face = seen.face || '';
      stub.innerHTML = '<span class="xr-caret" aria-hidden="true">▸</span>'
        + '<span class="xr-face"></span>';
      stub.querySelector('.xr-face').textContent = seen.face || 'folded';
      // Into the bar's own empty left half, ahead of everything it already holds.
      var bar = part.querySelector(':scope > .rm-srcbar, :scope > .srcbar');
      if (bar) bar.insertBefore(stub, bar.firstChild);
      else part.insertBefore(stub, part.firstChild);
      fold(part, true);
    });
  }

  // Nearest first: one file can be quoted under two tests, and the copy the reader is
  // looking at is the one inside the accordion they have open. Only when the link points
  // outside it does the search widen to the page.
  function target(link) {
    var id = link.getAttribute('data-xref');
    if (!id) return null;
    var sel = '[data-xref-id="' + id.replace(/"/g, '') + '"]';
    var near = link.closest('.rm-tinner') || link.closest('.panel');
    var box = (near && near.querySelector(sel)) || document.querySelector(sel);
    // A window quoted on a tab the reader is not on is not somewhere to send them: the
    // scroll would land on a hidden panel and the page would look like it ignored the
    // click. Nothing rendered means nothing to open here, and the anchor's own href takes
    // over — which opens the file in the editor, the way every other reference does.
    return box && box.offsetParent !== null ? box : null;
  }

  function flash(box, line) {
    Array.prototype.forEach.call(box.querySelectorAll('.ln-row.xr-hit'), function (row) {
      row.classList.remove('xr-hit');
    });
    if (!line) return;
    Array.prototype.forEach.call(box.querySelectorAll('.ln-row'), function (row) {
      var no = row.querySelector('.ln, .rm-ln');
      if (!no || no.textContent.trim() !== String(line)) return;
      // Restarting an animation needs the class gone and a reflow read before it is put
      // back, or a second click on the same line does nothing at all.
      row.classList.remove('xr-hit');
      void row.offsetWidth;
      row.classList.add('xr-hit');
    });
  }

  document.addEventListener('click', function (ev) {
    var stub = ev.target.closest && ev.target.closest('.xr-stub');
    if (stub) {
      var part = stub.closest('.rm-part');
      if (part) fold(part, !part.classList.contains('xr-shut'));
      return;
    }
    var link = ev.target.closest && ev.target.closest('a.xref');
    if (!link) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;
    var box = target(link);
    if (!box) return;          // nothing quoted here: the href opens it in the editor
    ev.preventDefault();
    var part = box.closest('.rm-part');
    // A second click on a link whose window is already open puts it back, so the reader
    // who followed a step and read its glue has the same handle to be rid of it again.
    if (part && part.classList.contains('xr-shut')) fold(part, false);
    else if (part && link.dataset.xrefOpen === 'yes') {
      link.dataset.xrefOpen = 'no';
      fold(part, true);
      return;
    }
    if (part) link.dataset.xrefOpen = 'yes';
    flash(box, link.getAttribute('data-xref-line'));
    box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  });

  adopt(document);
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        Array.prototype.forEach.call(record.addedNodes, function (added) {
          if (added.nodeType === 1) adopt(added);
        });
      });
    }).observe(document.body, {childList: true, subtree: true});
  }
})();
