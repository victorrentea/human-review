// Browser half of /human-review's per-spec Karma coverage: which Istanbul statement
// counters each spec moved. Istanbul keeps one global set of counters for the whole run,
// so a spec's own coverage is the difference between the counters after it and before it.
(function () {
  var karma = window.__karma__;
  var env = window.jasmine && window.jasmine.getEnv && window.jasmine.getEnv();
  if (!karma || !env) return;
  var before = null;
  function snapshot() {
    var cov = window.__coverage__ || {}, out = {};
    for (var k in cov) {
      var s = cov[k].s, copy = {};
      for (var id in s) copy[id] = s[id];
      out[k] = copy;
    }
    return out;
  }
  env.addReporter({
    specStarted: function () { before = snapshot(); },
    specDone: function (r) {
      var cov = window.__coverage__ || {}, hits = {};
      for (var k in cov) {
        var s = cov[k].s, b = (before && before[k]) || {}, ids = [];
        for (var id in s) if (s[id] > (b[id] || 0)) ids.push(id);
        if (ids.length) hits[k] = ids;
      }
      karma.info({hrTestcov: {id: r.fullName, description: r.description,
                              status: r.status, hits: hits}});
    }
  });
})();
