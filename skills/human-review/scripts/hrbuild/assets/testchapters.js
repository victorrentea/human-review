// The Tests tab's card, read by kind of test rather than by requirement sentence.
//
// The card used to list each test under the ticket sentence it was paired with. That
// pairing is a reading (a script's or a model's) and it moved between runs, so the card's
// shape moved with it (Victor, 7 Oct 2026: "flaky, unstable, opinionated"). What a test IS
// does not move: E2E, API or unit. So the rows go into three collapsible chapters, headed
// by the same badge each row already wears; the pairing still paints the ticket on the
// left and still lights the rows a sentence click picks, it just no longer files them.
//
// The chapters replace the E2E/API/UNIT filter checkboxes: a chapter that is closed is the
// filter. In their place, the card's title is a choice of which tests it lists (Victor, 20
// Oct 2026), "Tests: [covering the change set]" by default:
//   covering - the tests paired with a sentence of the ticket, the rows wearing the fork;
//   running  - every test whose own coverage ran a changed line, the blue-shield rows;
//   all      - those, and the rows that wear none: the branch's tests no probe measured,
//              the ones it deleted, and every other test the coverage run executed
//              (`#rm-all-tests`, written by tests.py `all_tests_inventory`): name, badge
//              and file link only - no preview to open, so the arrow's slot stays blank and
//              the columns stay put. Those rows are built on first pick, not at load: a big
//              repo runs thousands of tests and nobody asked for them yet.
// A chapter the choice leaves empty is not shown at all.
//
// The untouched tests that only run a changed line (semcov's `fold`, "Untouched and
// unpaired") used to sit behind the checkbox too. They wear the shield - their coverage
// really ran changed code - so a checked box showed shielded rows the unchecked card had
// said were not there (Victor, 19 Oct 2026). The fold was semcov's, written for the old
// sentence-grouped list that stood open and doubled the tab's height; the chapters start
// closed, so the height is no longer the reason, and the shield and the list agree again.
//
// The renderer (reqmap.js) is left to draw the rows; this moves them, after the fact, the
// same way trace.js and seqlink.js decorate them from the outside.
(function () {
  // Run again when a rebuild swaps the Tests tab into an open served page
  // (`data-hr-swap="rerun"`, tests.py): the run before it is taken down first — its
  // popover sits on <body>, not in the panel that went.
  if (window.__hrChaptersOff) { window.__hrChaptersOff(); window.__hrChaptersOff = null; }
  var map = document.querySelector('.reqmap');
  if (!map) return;
  var list = map.querySelector('.rm-code .rm-list');
  if (!list || list.querySelector('.rm-ch')) return;
  var D = {};
  try { D = JSON.parse(map.querySelector('.rm-data').textContent); } catch (e) { D = {}; }
  var TESTS = D.tests || {}, LABEL = D.cats || {e2e: 'E2E', api: 'API', unit: 'Unit'};
  // The subtitles, as few words as say what the badge alone does not.
  var CH = [['e2e', 'end-to-end, from the browser'],
            ['api', 'calling the API directly'],
            ['unit', 'one component, isolated']];
  // In the page's order, not this list's: `TEST_KIND_ORDER` (shared/util.py), which the
  // Sequence tab sorts its pairs by too.
  if (D.order) CH.sort(function (a, b) { return D.order.indexOf(a[0]) - D.order.indexOf(b[0]); });
  // Which chapters start open: none (Victor, 7 Oct 2026). Each heading carries its own
  // +added / -deleted / edited counts, so the closed card reads as a summary by kind.
  var OPEN = {};

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
    });
  }
  // reqmap.js's own unpacking of an identifier into words, so an added row reads like
  // the rows around it.
  function human(name) {
    name = String(name || '');
    if (/\s/.test(name)) return name;
    var w = name.replace(/[_\-]+/g, ' ')
      .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
      .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
      .split(/\s+/).filter(Boolean)
      .map(function (x) { return /^[A-Z0-9]{2,}$/.test(x) ? x : x.toLowerCase(); });
    return w.length ? w[0].charAt(0).toUpperCase() + w[0].slice(1)
      + (w.length > 1 ? ' ' + w.slice(1).join(' ') : '') : name;
  }
  // `OwnerListTest.java` -> `.java`, `add-visit.component.spec.ts` -> `.spec.ts`: the
  // same face the card's own file links wear.
  function ext(file) {
    var m = /((?:\.(?:spec|test|e2e-spec|e2e|cy|it))?\.[A-Za-z0-9]+)$/i.exec(file || '');
    return m ? m[1] : (file || '');
  }
  function catOf(row) {
    var b = row.querySelector('.rm-thead .rm-cat');
    return (b && b.dataset.cat) || 'unit';
  }

  // ---- the chapters --------------------------------------------------------------------
  var chapters = {};
  CH.forEach(function (c) {
    var sec = document.createElement('section');
    sec.className = 'rm-ch';
    sec.dataset.cat = c[0];
    sec.dataset.open = OPEN[c[0]] ? 'yes' : 'no';
    // The caret first, in the column the rows keep for their wire button, then the badge
    // and the subtitle right after it. The subtitle used to wait a blank arrow's width past
    // a 4.5em badge, to start where the rows' names did behind their own badges; the rows
    // wear no badge any more (their names take the kind's colour), and the gap it left
    // read as a hole (Victor, 19 Oct 2026: "remove this space"). While a sentence is
    // picked, `.rm-chhit` says how many of this kind's tests cover it - "none" included.
    // The counts close the row, in the column the card title's grand total stands in, the
    // chapter's total last with its `+N` right before it - the Complexity tab's `+1 12`.
    sec.innerHTML = '<div class="rm-chh" role="button" tabindex="0" aria-expanded="'
      + (OPEN[c[0]] ? 'true' : 'false') + '">'
      + '<span class="rm-chev rm-chcaret" aria-hidden="true">&#9654;</span>'
      + '<span class="rm-cat" data-cat="' + c[0] + '">' + esc(LABEL[c[0]] || c[0]) + '</span>'
      + '<span class="rm-chsub">' + esc(c[1]) + '</span>'
      + '<span class="rm-chhit" hidden></span>'
      + '<span class="rm-chled"></span></div>'
      + '<div class="rm-chb"><div class="rm-cho"></div></div>';
    chapters[c[0]] = sec;
  });

  // A row whose test pins no sentence of the ticket has its wire button disabled, and
  // hidden (reqmap.css). Every E2E row here is one - traced, paired with nothing - and a
  // blank slot said nothing about why. So the slot answers a hover, and only a hover: the
  // button stays out of sight, a click on it does nothing (`NOLINK`, the capture listener
  // below, keeps it from reqmap.js's `link`), and `disabled` goes because a disabled
  // button takes no pointer and so could show no tip.
  var NOLINK = 'Paired with no sentence';
  function quiet(row) {
    var b = row.querySelector('.rm-link[disabled]');
    if (!b) return;
    b.removeAttribute('disabled');
    b.setAttribute('aria-disabled', 'true');
    b.setAttribute('tabindex', '-1');
    b.setAttribute('data-tip', NOLINK);
  }

  // Stable hooks on every row, for whatever else decorates them from outside (the
  // fixture dot, the replay, the sequence link): the kind, the file, the test's own name.
  function hook(row) {
    var id = (row.dataset.id || '').replace(/@base$/, '');
    quiet(row);
    if (!row.dataset.cat) row.dataset.cat = catOf(row);
    if (id && !row.dataset.file) row.dataset.file = id.replace(/:\d+$/, '');
    var t = TESTS[row.dataset.id];
    if (t && !row.dataset.testName) row.dataset.testName = t.title || '';
  }
  Array.prototype.slice.call(list.children).forEach(function (el) {
    if (el.classList.contains('rm-t')) {
      hook(el);
      var sec = chapters[el.dataset.cat] || chapters.unit;
      var body = sec.querySelector('.rm-chb');
      body.insertBefore(el, body.querySelector('.rm-cho'));
    } else if (el.classList.contains('rm-tgroup')) {
      el.parentNode.removeChild(el);        // the sentence headings: no longer the grouping
    }
    // `.rm-fold` buttons stay in the list, hidden by the stylesheet: "All tests" is what
    // opens what they used to.
  });
  CH.forEach(function (c) { list.appendChild(chapters[c[0]]); });

  // ---- "Tests: [covering the change set ▾]", the card's own title ------------------------
  // The title's tail becomes the choice; "Tests:" stays the label in front of it. The
  // filters it replaces stood over the card; their chips go.
  var inv = document.getElementById('rm-all-tests'), INV = null;
  var bar = map.querySelector('.rm-cats');
  if (bar) Array.prototype.forEach.call(bar.querySelectorAll('.rm-catf, .rm-allf'), function (x) {
    x.parentNode.removeChild(x);
  });
  // "covering" carries the 🤖: which test covers which sentence is a model's mapping, not
  // a measurement (Victor, 20 Oct 2026); "running" is measured, so it has none. Each option
  // says "tests" itself, with no "Tests:" in front (Victor, 29 Oct 2026). "running
  // through changed files" is his label; the probe counts changed *lines* (tests.py
  // `coverage_join`), so the hover says that. The third field is the card title's hover.
  var MODES = [['cover', 'Tests covering \uD83E\uDD16 the change',
                'Tests paired with a sentence of the ticket, the ones wearing the fork.'],
               ['run', 'Tests running through changed files',
                'Tests that ran a changed line, as captured by a coverage probe.'],
               ['all', 'All tests', 'All tests, as captured by a coverage probe.']];
  // The choice's own hover: what the total at the end of its row counts, first - that
  // total is the number it chose (CLAUDE.md: a tip about numbers says first what they
  // mean) - then what the choice is, then the `+N ✏️N −N` beside it, which no choice
  // moves. A short line beside the choice used to say the first part, "🤖 matched to the
  // ticket", wrapped into a tall column that broke the row (Victor, 29 Oct 2026); the hover
  // says it whole instead. `n` is the total listed, from `count`.
  var MODE_TIP = {
    cover: function (n) {
      return [n + ': the tests \uD83E\uDD16 matched to a sentence of the ticket, all kinds;'
          + ' each kind\u2019s own count is on its row below.',
        'A model\u2019s reading of which test covers which sentence, not a measurement:'
          + ' the rows wearing the fork.'];
    },
    run: function (n) {
      return [n + ': the tests a coverage probe saw run a changed line of a changed file'
          + ' (not merely its other lines), plus any \uD83E\uDD16 matched to the ticket;'
          + ' each kind\u2019s own count is on its row below.',
        'The probe\u2019s part is measured, not guessed: the lines each test executed,'
          + ' recorded as the suite ran.'];
    },
    all: function (n) {
      return [n + ': every test on record, all kinds: the two other choices, the branch\u2019s'
          + ' tests no probe measured, the ones it deleted, and the rest of the coverage run.',
        'The rest of the run is listed by name and file only, with no preview to open.'];
    }
  };
  var MOVES_TIP = '+N \u270F\uFE0FN \u2212N: the tests this branch added, edited and deleted,'
    + ' the same whichever list is chosen.';
  function modeTip(mode, n) {
    var l = (MODE_TIP[mode] || MODE_TIP.cover)(n);
    return '<div>' + esc(l[0]) + '</div><p class="tipfoot">' + esc(l[1]) + '</p><p class="tipfoot">'
      + esc(MOVES_TIP) + '</p>';
  }
  var head = map.querySelector('.rm-code > .rm-tkhead');
  var who = head && head.querySelector('.cov-head .rm-who');
  var pick = null;
  if (who) {
    who.textContent = '';
    who.hidden = true;
    pick = document.createElement('select');
    pick.className = 'rm-mode';
    pick.setAttribute('aria-label', 'Which tests the card lists');
    pick.innerHTML = MODES.map(function (m, i) {
      return '<option value="' + m[0] + '"' + (i ? '' : ' selected') + '>' + esc(m[1]) + '</option>';
    }).join('');
    head.insertBefore(pick, who.closest('.cov-head').nextSibling);
  }
  // The grand total, on the card's title row in the chapters' own four columns: the sum of
  // their `−`, ✏️ and `+` and of the tests listed (Victor, 19 Oct 2026: the masthead's
  // `tests +20` is the chapters' `+11` and `+9`, and nothing on the card added them up).
  var grand = null;
  if (head) {
    grand = document.createElement('span');
    grand.className = 'rm-chled rm-grand';
    head.insertBefore(grand, head.querySelector('.tledger'));
  }
  Array.prototype.forEach.call(list.querySelectorAll('.rm-t[data-catoff]'), function (r) {
    delete r.dataset.catoff;
  });

  function otherRow(t) {
    var file = t.file || '', cat = CH.some(function (c) { return c[0] === t.cat; }) ? t.cat : 'unit';
    var link = t.href
      ? '<a class="rm-tw srcref" href="' + esc(t.href) + '" data-tip="' + esc(file + ':' + t.line)
        + ' — Open in VS Code" target="_blank" rel="noopener" data-ext-only="">'
        + esc(ext(file.split('/').pop())) + '</a>'
      : '<span class="rm-tw"></span>';
    return '<div class="rm-t rm-other" data-shut="yes" data-open="no" data-cat="' + cat
      + '" data-file="' + esc(file) + '" data-test-name="' + esc(t.title) + '">'
      + '<div class="rm-thead">'
      + '<button class="rm-link" type="button" aria-disabled="true" tabindex="-1" data-tip="'
      + NOLINK + '"></button>'
      + '<span class="rm-cat" data-cat="' + cat + '">' + esc(LABEL[cat] || cat) + '</span>'
      + '<span class="rm-chev" aria-hidden="true">&#9654;</span>'
      + '<span class="rm-tt">' + esc(human(t.title)) + '</span>'
      + link
      + '<span class="rm-run" aria-hidden="true"></span>'
      + '<span class="rm-st rm-st-none" aria-hidden="true"></span></div></div>';
  }
  function buildOthers() {
    if (INV || !inv) return;
    try { INV = JSON.parse(inv.textContent).tests || []; } catch (e) { INV = []; }
    var html = {};
    INV.forEach(function (t) {
      var c = chapters[t.cat] ? t.cat : 'unit';
      html[c] = (html[c] || '') + otherRow(t);
    });
    Object.keys(html).forEach(function (c) {
      chapters[c].querySelector('.rm-cho').innerHTML = html[c];
    });
  }

  // Two rows of one chapter with the same name - `getAll` in OwnerTest, PetTest and
  // VisitTest; "should create" in two component specs - read as one test listed twice,
  // since a row names its file by extension only. Where a name repeats among the rows on
  // show, the file link says the file instead: `OwnerTest.java`, `visit-edit….spec.ts` (the
  // name up to its first dot, then the extension), the whole path on its hover as ever.
  // Only there, so a card of distinct names keeps its narrow column.
  function shortFile(file) {
    var base = String(file || '').split('/').pop(), e = ext(base);
    var stem = base.slice(0, base.length - e.length), first = stem.split('.')[0];
    return first + (first.length < stem.length ? '\u2026' : '') + e;
  }
  function distinguish(sec, shown) {
    var by = {};
    shown.forEach(function (r) {
      var t = r.querySelector('.rm-tt'), k = t ? t.textContent.trim().toLowerCase() : '';
      (by[k] = by[k] || []).push(r);
    });
    Array.prototype.forEach.call(sec.querySelectorAll('.rm-t'), function (r) {
      var tw = r.querySelector('.rm-tw');
      if (!tw || !tw.textContent && !tw.dataset.face) return;
      if (tw.dataset.face === undefined) tw.dataset.face = tw.textContent;
      var t = r.querySelector('.rm-tt'), k = t ? t.textContent.trim().toLowerCase() : '';
      var clash = shown.indexOf(r) >= 0 && (by[k] || []).length > 1;
      var face = clash ? shortFile(r.dataset.file) : tw.dataset.face;
      if (tw.textContent !== face) tw.textContent = face;
      // A longer name fills the file column: the DB-fixture dot cannot slide into it.
      if (clash) tw.dataset.clash = 'yes'; else delete tw.dataset.clash;
    });
  }

  // Rows without a shield (see the header): listed only under "all". `data-fold` rows ran
  // a changed line, so "running" lists them too.
  function folded(row) {
    return row.dataset.own === 'yes' || row.dataset.del === 'yes';
  }
  // The fork: a wire button the renderer left enabled, so the test pins a sentence.
  function paired(row) {
    var b = row.querySelector('.rm-link');
    return !!b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
  }
  function listed(row, mode) {
    if (mode === 'all') return true;
    if (row.classList.contains('rm-other')) return false;
    return mode === 'cover' ? paired(row) : !folded(row);
  }
  // `−`, ✏️, `+` and the total, four slots always, an empty one kept (Victor, 14 Oct 2026:
  // "align in columns"), so the chapters' counts and the grand total stand in columns.
  // The `−N` is no tip but the popover under it (`goneHtml`): which tests went, each a way
  // into the IDE (Victor, 20 Oct 2026: "how could you render the tests gone?"). `cat` is
  // the chapter's kind, '' for the grand total's every kind.
  function led(st, n, K, what, cat) {
    var k = K ? ' ' + K : '';
    function tests(x) { return ' test' + (x === 1 ? '' : 's'); }
    return (st.deleted ? '<span class="removed" tabindex="0" data-gone="' + esc(cat || '')
        + '" aria-label="' + st.deleted + k + tests(st.deleted) + ' deleted">\u2212'
        + st.deleted + '</span>' : '<span></span>')
      + (st.edited ? '<span class="changed" data-tip="' + st.edited + k + tests(st.edited)
        + ' edited">\u270F\uFE0F' + st.edited + '</span>' : '<span></span>')
      + (st.new ? '<span class="added" data-tip="' + st.new + ' new' + k + tests(st.new) + '">+'
        + st.new + '</span>' : '<span></span>')
      + '<span class="rm-chn" data-tip="' + n + k + tests(n) + ' ' + what + '">' + n + '</span>';
  }
  function count() {
    var mode = list.dataset.mode || 'cover';
    var sum = {new: 0, deleted: 0, edited: 0}, total = 0;
    CH.forEach(function (c) {
      var sec = chapters[c[0]], n = 0, shown = [];
      Array.prototype.forEach.call(sec.querySelectorAll('.rm-t'), function (r) {
        var on = listed(r, mode);
        if (on) { n++; shown.push(r); delete r.dataset.off; } else r.dataset.off = 'yes';
      });
      distinguish(sec, shown);
      sec.hidden = !n;
      // What the branch did to this kind's tests: the card title's three counts, for one
      // chapter, off the stamps its rows already wear. Every row is counted, folded or not.
      var st = {new: 0, deleted: 0, edited: 0};
      Array.prototype.forEach.call(sec.querySelectorAll('.rm-t .rm-st[data-st]'), function (s) {
        if (s.dataset.st in st) st[s.dataset.st]++;
      });
      // The `+N` sits right in front of the total, as the Complexity tab writes `+1 12`.
      var K = esc(LABEL[c[0]] || c[0].toUpperCase());
      sec.querySelector('.rm-chled').innerHTML = led(st, n, K, 'listed', c[0]);
      Object.keys(st).forEach(function (k) { sum[k] += st[k]; });
      total += n;
    });
    if (grand) {
      grand.innerHTML = led(sum, total, '', 'listed, all kinds');
    }
    if (pick) pick.setAttribute('data-tip-html', modeTip(mode, total));
  }
  function setMode(mode) {
    if (mode === 'all') buildOthers();
    list.dataset.mode = mode;
    list.dataset.all = mode === 'all' ? 'yes' : 'no';
    // The card title's hover says what the list is, so it follows the choice.
    var tipEl = map.querySelector('.rm-code > .rm-tkhead .cov-head[data-tip]')
             || map.querySelector('.rm-code > .rm-tkhead .rm-who[data-tip]');
    MODES.forEach(function (m) {
      if (m[0] === mode && tipEl) tipEl.setAttribute('data-tip', m[2]);
    });
    // Rows are kept or dropped one by one (`data-off`, from `listed`), not by these.
    ['unfold', 'unown', 'undel'].forEach(function (k) { list.dataset[k] = 'yes'; });
    count();
    window.dispatchEvent(new Event('resize'));       // the wires follow the rows
  }
  if (pick) pick.addEventListener('change', function () { setMode(pick.value); });
  setMode(pick ? pick.value : 'run');

  list.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('.rm-link[aria-disabled=true]')) {
      e.preventDefault(); e.stopPropagation();
    }
  }, true);

  // ---- open / close ---------------------------------------------------------------------
  function openCh(sec, on) {
    sec.dataset.open = on ? 'yes' : 'no';
    sec.querySelector('.rm-chh').setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  list.addEventListener('click', function (e) {
    var h = e.target.closest('.rm-chh');
    // The `−N` answers with its popover, not by folding the chapter it sits on.
    if (!h || e.target.closest('.removed[data-gone]')) return;
    var sec = h.parentNode;
    openCh(sec, sec.dataset.open !== 'yes');
    window.dispatchEvent(new Event('resize'));
  });
  list.addEventListener('keydown', function (e) {
    var h = e.target.closest && e.target.closest('.rm-chh');
    if (!h || (e.key !== 'Enter' && e.key !== ' ') || e.target.closest('.removed[data-gone]')) return;
    e.preventDefault();
    openCh(h.parentNode, h.parentNode.dataset.open !== 'yes');
    window.dispatchEvent(new Event('resize'));
  });

  // A sentence picked on the left lights its tests on the right (reqmap.js `open`). A lit
  // row in a closed chapter would be lit where nobody can see it, so its chapter opens and
  // the first lit row is brought to the top of the card - under its chapter's heading,
  // which sticks to the card's top edge as the list scrolls. Runs after the renderer's own
  // handler: it is registered later on the same element.
  function reveal() {
    if (list.dataset.sel !== 'yes') return;
    var hits = Array.prototype.filter.call(list.querySelectorAll('.rm-t[data-hit=yes]'),
      function (r) { return r.dataset.off !== 'yes'; });
    if (!hits.length) return;
    hits.forEach(function (r) { var s = r.closest('.rm-ch'); if (s) openCh(s, true); });
    var sec = hits[0].closest('.rm-ch'), hh = sec ? sec.querySelector('.rm-chh').offsetHeight : 0;
    var top = hits[0].getBoundingClientRect().top - list.getBoundingClientRect().top - hh;
    if (top < 0 || top > list.clientHeight - hh - 30) list.scrollTop += top - 4;
    window.dispatchEvent(new Event('resize'));
  }
  // How many of each kind's tests cover the picked sentence, on the kind's own heading -
  // "none" too, so a kind with no test for it reads at a glance, open or closed, on screen
  // or scrolled away (Victor, 19 Oct 2026: "I'd want to know there are no unit tests
  // covering this"). Follows `data-sel` on the list, which reqmap.js sets on a pick and
  // takes off on every way out of one.
  function hitMarks() {
    var FAN = '<svg class="rm-fan" viewBox="0 0 12 14" aria-hidden="true">'
      + '<path d="M7 7H11.2"/><path d="M7 7H1.2"/><path d="M7 7C4.6 7 4.6 1.7 1.2 1.7"/>'
      + '<path d="M7 7C4.6 7 4.6 12.3 1.2 12.3"/></svg>';
    var on = list.dataset.sel === 'yes';
    CH.forEach(function (c) {
      var sec = chapters[c[0]], mark = sec.querySelector('.rm-chhit');
      if (!on) { delete sec.dataset.hits; mark.hidden = true; mark.textContent = ''; return; }
      var n = sec.querySelectorAll('.rm-t[data-hit=yes]:not([data-off=yes])').length;
      var K = LABEL[c[0]] || c[0].toUpperCase();
      sec.dataset.hits = String(n);
      mark.hidden = false;
      // The rows' own link icon, not a tick (Victor, 20 Oct 2026: "the same fork icon").
      mark.innerHTML = n ? FAN + n : 'none';
      mark.setAttribute('data-tip', n
        ? n + ' ' + K + ' test' + (n === 1 ? ' covers' : 's cover') + ' the picked sentence'
        : 'No ' + K + ' test covers the picked sentence');
    });
  }
  if (window.MutationObserver) {
    new MutationObserver(hitMarks).observe(list, {attributes: true, attributeFilter: ['data-sel']});
  }
  map.addEventListener('click', function (e) { if (e.target.closest('.rm-f')) reveal(); });

  // ---- the shield: which code this test really runs ---------------------------------------
  // The row's 🛡 used to say "Runs changed code: a.ts:13-15... +6 files", one line, gone
  // the moment the pointer left it. Its question is "what part of the code is this test
  // really covering?", and the answer is a list of files a reader wants to open. So it is
  // a popover that stays while the pointer travels into it: the changed files, with how
  // many changed lines the test ran in each; each name is the page's ordinary `vscode://`
  // link (`#rm-test-cover`, written by tests.py `test_cover_files`). The unchanged files it
  // also ran (router-stubs.ts…) went on 19 Oct 2026 (Victor): the shield answers "which
  // changed code", and the rest of the run was noise under it.
  var COVER = {};
  try { COVER = JSON.parse(document.getElementById('rm-test-cover').textContent) || {}; }
  catch (e) { COVER = {}; }
  var pop = null, popFor = null, showT = 0, hideT = 0;
  function coverHtml(c) {
    var ch = c.files.filter(function (f) { return f[2] > 0; });
    function li(f) {
      var name = f[0].split('/').pop();
      return '<li><a href="' + esc(f[3]) + '" data-path="' + esc(f[0]) + '">' + esc(name) + '</a>'
        + (f[2] ? ' <span class="rm-cpn">' + f[2] + ' changed line' + (f[2] === 1 ? '' : 's')
          + '</span>' : '') + '</li>';
    }
    return (ch.length ? '<p class="rm-cph">Changed code it runs</p><ul class="tiplist">'
        + ch.map(li).join('') + '</ul>' : '')
      + (c.more ? '<p class="rm-cpm">+' + c.more + ' more file' + (c.more === 1 ? '' : 's')
        + '</p>' : '');
  }
  function placePop(el) {
    var r = el.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    var left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
    var top = r.bottom + 4;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    pop.style.left = left + 'px'; pop.style.top = top + 'px';
  }
  // ---- the `−N`: which tests the branch deleted, and where they went -----------------------
  // Locally each name opens its file in the IDE as a diff, the base commit on the left, the
  // caret where the removal landed (semcov.py `deleted_ide`): the test is the red block in
  // front of the reader. Through editor.js, as every diff handle on the page, so VS Code,
  // IntelliJ, served or off disk all take the route they take for any other diff. Online,
  // or where the file itself is gone, the name opens the test as it was on GitHub.
  function online() { return document.documentElement.classList.contains('hr-online'); }
  function goneRows(cat) {
    var out = [];
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t .rm-st[data-st=deleted]'), function (s) {
      var row = s.closest('.rm-t');
      if (row && (!cat || row.dataset.cat === cat)) out.push(row);
    });
    return out;
  }
  function goneLink(t) {
    var face = esc(human(t.title));
    // Opened through `openIde` (the popover's click, below), not as a link of its own: the
    // face is the test's name, and editor.js copies a link's face where no IDE is connected.
    if (t.ide && !online()) return '<a href="' + esc(t.ide.href) + '" data-gone-id="' + esc(t.id) + '">' + face + '</a>';
    if (t.href) return '<a href="' + esc(t.href) + '" target="_blank" rel="noopener">' + face + '</a>';
    return '<span>' + face + '</span>';
  }
  function goneHtml(cat) {
    var rows = goneRows(cat);
    if (!rows.length) return '';
    var ide = !online() && rows.some(function (r) { var t = TESTS[r.dataset.id]; return t && t.ide; });
    var K = cat ? (LABEL[cat] || cat.toUpperCase()) + ' ' : '';
    return '<p class="rm-cph">' + rows.length + ' ' + esc(K) + 'test' + (rows.length === 1 ? '' : 's')
      + ' deleted by this branch' + (ide ? ' \u2014 click: the diff in your IDE' : '')
      + '</p><ul class="tiplist">' + rows.map(function (r) {
        var t = TESTS[r.dataset.id] || {title: r.dataset.testName || '?'};
        return '<li>' + goneLink(Object.assign({id: r.dataset.id}, t)) + ' <span class="rm-cpn">'
          + esc(t.where || (r.dataset.file || '').split('/').pop()) + '</span></li>';
      }).join('') + '</ul>';
  }
  // The deleted row's own links (its file, its excerpt's bar) go the same way: to the IDE
  // locally, to the base commit on GitHub online — the href they were drawn with. The
  // handle is a bare diff link with no face, so where no IDE is connected editor.js copies
  // its `path:line` rather than a test's name.
  function openIde(ide) {
    var a = document.createElement('a');
    a.href = ide.href;
    a.setAttribute('data-diff-base', ide.base);
    a.setAttribute('data-diff-path', ide.path);
    if (ide.uri) a.setAttribute('data-diff-uri', ide.uri);
    a.hidden = true;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  function goneAim(el) {
    var a = el.closest && el.closest('.rm-t[data-gone=yes] a.srcref');
    var row = a && a.closest('.rm-t'), t = row && TESTS[row.dataset.id];
    return t && t.ide ? {a: a, t: t} : null;
  }
  list.addEventListener('click', function (e) {
    var g = goneAim(e.target);
    if (!g || online() || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    openIde(g.t.ide);
  }, true);
  // Its tip says which of the two the click does, asked as the pointer arrives: a served
  // page is online until its probe answers. Before the page's tip reads it (document).
  list.addEventListener('pointerover', function (e) {
    var g = goneAim(e.target);
    if (!g) return;
    if (g.a.dataset.tipOnline === undefined) g.a.dataset.tipOnline = g.a.getAttribute('data-tip') || '';
    g.a.setAttribute('data-tip', online() ? g.a.dataset.tipOnline : g.t.ide.path
      + ' \u2014 deleted by this branch: opens as a diff in your IDE, the base commit on the left');
  });

  function showPop(el) {
    var gone = el.matches('.removed[data-gone]'), c = null;
    if (!gone) {
      var row = el.closest('.rm-t');
      c = row && COVER[(row.dataset.id || '').replace(/@base$/, '')];
      if (!c) return;
    }
    var body = gone ? goneHtml(el.dataset.gone) : coverHtml(c);
    if (!body) return;
    if (!pop) {
      pop = document.createElement('div');
      pop.className = 'rm-covpop';
      pop.setAttribute('role', 'tooltip');
      pop.addEventListener('pointerenter', function () { clearTimeout(hideT); });
      pop.addEventListener('pointerleave', function () { hideSoon(); });
      pop.addEventListener('click', function (e) {
        var a = e.target.closest && e.target.closest('a[data-gone-id]'), t = a && TESTS[a.dataset.goneId];
        if (!t || !t.ide || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        openIde(t.ide);
      });
      document.body.appendChild(pop);
    }
    pop.innerHTML = body;
    popFor = el;
    pop.classList.add('on');
    placePop(el);
  }
  function hidePop() { if (pop) pop.classList.remove('on'); popFor = null; }
  function hideSoon() { clearTimeout(showT); clearTimeout(hideT); hideT = setTimeout(hidePop, 250); }
  function arm(row) {
    var run = row.querySelector('.rm-run');
    if (!run || !COVER[(row.dataset.id || '').replace(/@base$/, '')]) return;
    // The page's one-line tip would open on top of the popover: the popover says more.
    run.removeAttribute('data-tip');
    run.dataset.cover = 'yes';
  }
  Array.prototype.forEach.call(list.querySelectorAll('.rm-t[data-id]'), arm);
  // On the card, not the list: the grand total's `−N` sits in the title strip above it.
  var POPS = '.rm-run[data-cover], .removed[data-gone]';
  map.addEventListener('pointerover', function (e) {
    var run = e.target.closest && e.target.closest(POPS);
    if (!run) return;
    clearTimeout(hideT);
    if (popFor === run) return;
    clearTimeout(showT);
    showT = setTimeout(function () { showPop(run); }, 150);
  });
  map.addEventListener('pointerout', function (e) {
    var run = e.target.closest && e.target.closest(POPS);
    if (run && !run.contains(e.relatedTarget)) hideSoon();
  });
  // By keyboard too: the `−N` takes a tab stop, and its list is the only way to its names.
  map.addEventListener('focusin', function (e) {
    var g = e.target.closest && e.target.closest('.removed[data-gone]');
    if (g) { clearTimeout(hideT); showPop(g); }
  });
  map.addEventListener('focusout', function (e) {
    var g = e.target.closest && e.target.closest('.removed[data-gone]');
    if (g && !(pop && pop.contains(e.relatedTarget))) hideSoon();
  });
  list.addEventListener('scroll', hidePop, {passive: true});
  function onEsc(e) { if (e.key === 'Escape') hidePop(); }
  document.addEventListener('keydown', onEsc);
  window.__hrChaptersOff = function () {
    hidePop();
    if (pop) pop.remove();
    document.removeEventListener('keydown', onEsc);
  };
  map.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.closest && e.target.closest('.rm-f')) reveal();
  });
})();
