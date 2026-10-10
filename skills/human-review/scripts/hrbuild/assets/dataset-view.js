// The Demo tab's dataset view: in the "DB Fixture" card, each fixture is a header — a
// caret, its dot, its name, its Seed — and opening the caret lays that fixture's tables out
// in a band under it, so a reviewer sees what "green" is without reading green.sql (Victor,
// 7 Oct 2026). One under the other, any number open at once, so the seed and a fixture can
// be compared top to bottom (9 Oct 2026). A fixture is a dataset of its own, loaded on an
// empty DB, never the seed with extras (10 Oct 2026). The rows were computed at build time from the
// project's own SQL (`dataset_view.py`) and sit in #dsv-data; nothing here talks to the
// app, so the view works with the app down too.
(function () {
  var src = document.getElementById('dsv-data');
  var card = document.querySelector('.dbfx');
  if (!src || !card) return;
  var data;
  try { data = JSON.parse(src.textContent); } catch (e) { return; }
  // A foreign key, said with a glyph on its column's header rather than a "→ owners, types"
  // trailing the row count: the key sits on the very column it describes. Drawn, not an
  // emoji, so it takes the header's colour and size in both themes.
  var KEY = '<svg class="dsv-key" viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">'
    + '<circle cx="4.6" cy="8" r="3" fill="none" stroke="currentColor" stroke-width="1.7"/>'
    + '<path d="M7.6 8H15M12.4 8v3M14.6 8v2.4" fill="none" stroke="currentColor"'
    + ' stroke-width="1.7" stroke-linecap="round"/></svg>';

  function label(name) { return name ? name : 'Default'; }
  function has(name) { return Object.prototype.hasOwnProperty.call(data.sets, name); }
  function rowsOf(table, name) {
    return (data.sets[name] && data.sets[name][table]) || [];
  }
  function countOf(table, name) {
    return (data.counts[name] && data.counts[name][table]) || 0;
  }
  var byName = {};
  data.tables.forEach(function (t) { byName[t.name] = t; });

  // The row a foreign key points at, in words: `owners #1 · Kevin McCallister`. A bare 6 in
  // `type_id` tells nobody it is a hamster. Looked up in the same dataset the cell is in.
  function target(table, value, name) {
    var t = byName[table];
    if (!t || value === null || value === undefined) return '';
    var pk = t.cols.indexOf('id');
    if (pk < 0) pk = 0;
    var hit = rowsOf(table, name).filter(function (r) { return r[pk] === value; })[0];
    if (!hit) return table + ' ' + value + ' (not in the data)';
    var words = [];
    t.cols.forEach(function (c, i) {
      if (i !== pk && words.length < 2 && typeof hit[i] === 'string' && !/(^|_)id$/.test(c)
          && hit[i].length < 40) words.push(hit[i]);
    });
    return table + ' #' + value + (words.length ? ' · ' + words.join(' ') : '');
  }

  function cell(v) {
    var td = document.createElement('td');
    if (v === null || v === undefined) { td.className = 'dsv-null'; td.textContent = 'null'; }
    else {
      var s = String(v);
      td.textContent = s;
      if (s.length > 28) td.dataset.tip = s;
      if (typeof v === 'number') td.className = 'dsv-num';
    }
    return td;
  }

  function grid(t, name) {
    var rows = rowsOf(t.name, name), count = countOf(t.name, name);
    var d = document.createElement('details');
    d.className = 'dsv-t';
    d.dataset.table = t.name;
    // Every table open: each one is capped at a few rows' height and scrolls inside, and
    // two fixtures open one above the other must fold the same, or they cannot be compared.
    d.open = true;
    var sum = document.createElement('summary');
    var caret = document.createElement('span');
    caret.className = 'disclose';
    var nm = document.createElement('b');
    nm.textContent = t.name;
    var n = document.createElement('span');
    n.className = 'dsv-n';
    n.textContent = count + (count === 1 ? ' row' : ' rows');
    sum.append(caret, nm, n);
    d.appendChild(sum);
    function fill() {
      if (d.dataset.drawn) return;
      d.dataset.drawn = '1';
      var box = document.createElement('div');
      box.className = 'dsv-grid';
      var tb = document.createElement('table');
      var hr = document.createElement('tr');
      t.cols.forEach(function (c, i) {
        var th = document.createElement('th');
        var vals = rows.map(function (r) { return r[i]; })
          .filter(function (v) { return v !== null && v !== undefined; });
        if (vals.length && vals.every(function (v) { return typeof v === 'number'; })) {
          th.classList.add('dsv-numcol');
        }
        if (t.fk && t.fk[c]) {
          th.classList.add('dsv-fkcol');
          th.innerHTML = KEY;
          th.dataset.tip = 'Foreign key to ' + t.fk[c] + ' — hover a value for its row';
        }
        // A column no row fills reads as data that is missing; say that it is absent on
        // purpose instead of leaving a column of "null" to be puzzled over.
        if (rows.length && !vals.length) {
          th.classList.add('dsv-allnull');
          th.dataset.tip = (th.dataset.tip ? th.dataset.tip + '. ' : '')
            + 'Null in every row of this dataset';
        }
        th.appendChild(document.createTextNode(c));
        hr.appendChild(th);
      });
      var head = document.createElement('thead');
      head.appendChild(hr);
      var body = document.createElement('tbody');
      rows.forEach(function (r) {
        var tr = document.createElement('tr');
        r.forEach(function (v, k) {
          var td = cell(v), fk = t.fk && t.fk[t.cols[k]];
          if (fk && v !== null && v !== undefined) {
            td.classList.add('dsv-fkv');
            td.dataset.tip = target(fk, v, name);
          }
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
      tb.append(head, body);
      box.appendChild(tb);
      if (count > rows.length) {
        var more = document.createElement('p');
        more.className = 'dsv-more';
        more.textContent = 'first ' + rows.length + ' of ' + count;
        box.appendChild(more);
      }
      d.appendChild(box);
    }
    if (d.open) fill();
    d.addEventListener('toggle', function () { if (d.open) fill(); });
    return d;
  }

  // What the header says while folded: how big the dataset is, so the seed and a fixture
  // compare at a glance before either is opened. "111 rows in 9 tables": the tables that
  // hold a row, not the schema's count, which is the same on every line.
  function summary(name) {
    var total = 0, filled = 0;
    data.tables.forEach(function (t) {
      var n = countOf(t.name, name);
      total += n;
      if (n) filled++;
    });
    return total + (total === 1 ? ' row' : ' rows') + ' in '
      + filled + (filled === 1 ? ' table' : ' tables');
  }

  function body(fx) {
    var b = fx.querySelector('.dbfx-body');
    if (b && !b.dataset.drawn) {
      b.dataset.drawn = '1';
      var name = fx.dataset.fixture || '';
      var row = document.createElement('div');
      row.className = 'dsv-tables';
      data.tables.forEach(function (t) { row.appendChild(grid(t, name)); });
      b.appendChild(row);
    }
    return b;
  }

  function fxOf(name) {
    return [].filter.call(card.querySelectorAll('.appenv-fx[data-fixture]'), function (f) {
      return (f.dataset.fixture || '') === name;
    })[0];
  }

  function toggle(fx, open) {
    var tog = fx.querySelector('.dbfx-tog'), b = fx.querySelector('.dbfx-body');
    if (!tog || !b || !has(fx.dataset.fixture || '')) return;
    if (open === undefined) open = tog.getAttribute('aria-expanded') !== 'true';
    if (open) body(fx);
    b.hidden = !open;
    tog.setAttribute('aria-expanded', open ? 'true' : 'false');
    fx.classList.toggle('open', open);
  }

  // Wire each header the build drew. A fixture the data does not know keeps its name and
  // its Seed, and its caret says there is nothing to show: a caret that opens nothing,
  // silently, is worse than none.
  [].forEach.call(card.querySelectorAll('.appenv-fx[data-fixture]'), function (fx) {
    var name = fx.dataset.fixture || '', tog = fx.querySelector('.dbfx-tog');
    if (!tog) return;
    var sum = fx.querySelector('.dbfx-sum');
    if (!has(name)) {
      tog.setAttribute('aria-disabled', 'true');
      tog.dataset.tip = 'No rows computed for ' + label(name) + ' at build time';
      return;
    }
    tog.dataset.tip = name ? 'Show the rows the “' + name + '” fixture loads into an empty DB'
                           : 'Show the seed rows';
    if (sum) sum.textContent = summary(name);
  });

  card.addEventListener('click', function (ev) {
    var tog = ev.target.closest('.dbfx-tog');
    if (!tog || tog.getAttribute('aria-disabled') === 'true') return;
    toggle(tog.closest('.appenv-fx'));
  });

  // For other parts of the page (the Tests tab's fixture dots): bring up one fixture's
  // data — the Demo tab, its header open, scrolled into view.
  window.hrOpenDataset = function (name) {
    name = name || '';
    var fx = fxOf(name);
    if (!has(name) || !fx) return false;
    var tab = document.querySelector('[role="tab"][aria-controls="behaviour"]');
    if (tab && tab.getAttribute('aria-selected') !== 'true') tab.click();
    toggle(fx, true);
    requestAnimationFrame(function () {
      fx.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    return true;
  };
})();
