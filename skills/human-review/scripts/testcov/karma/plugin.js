// Node half of /human-review's per-spec Karma coverage.
//
// The browser (client.js) reports, per spec, which Istanbul statements it executed. At the
// end of the run the full coverage object arrives with the browser's completion, carrying
// every file's statement map and the source map of the transpiled JS it was taken from;
// this maps statements -> JS lines -> TypeScript lines and writes one JSON file:
//   {"executable": {<abs ts path>: [lines]}, "tests": [{id, description, status, hits}]}
'use strict';
const fs = require('fs');
const path = require('path');

function loadSourceMap() {
  const from = [path.dirname(path.resolve(process.env.HR_TESTCOV_KARMA_BASE || '.')), process.cwd()];
  return require(require.resolve('source-map', {paths: from}));
}

function initFramework(files) {
  files.push({pattern: path.join(__dirname, 'client.js'), included: true, served: true,
              watched: false, nocache: false});
}
initFramework.$inject = ['config.files'];

function Reporter(logger) {
  const log = logger.create('hr-testcov');
  const specs = [];
  let coverage = null;
  this.adapters = [];

  this.onBrowserInfo = function (browser, info) {
    if (info && info.hrTestcov) specs.push(info.hrTestcov);
  };
  this.onBrowserComplete = function (browser, result) {
    if (result && result.coverage) coverage = result.coverage;
  };
  this.onExit = function (done) {
    write().then(done, err => { log.error(String(err && err.stack || err)); done(); });
  };

  async function write() {
    const out = process.env.HR_TESTCOV_OUT;
    if (!out) return;
    if (!coverage) {
      log.warn('no coverage object came back from the browser — was --code-coverage on?');
      return;
    }
    const {SourceMapConsumer} = loadSourceMap();
    const lines = {};          // file -> statement id -> [ts lines]
    const executable = {};
    for (const [file, fc] of Object.entries(coverage)) {
      const key = fc.path || file;
      let consumer = null;
      if (fc.inputSourceMap) {
        try { consumer = await new SourceMapConsumer(fc.inputSourceMap); } catch (e) { consumer = null; }
      }
      const byId = {};
      const all = new Set();
      for (const [id, loc] of Object.entries(fc.statementMap || {})) {
        const got = new Set();
        for (let l = loc.start.line; l <= loc.end.line; l++) {
          if (!consumer) { got.add(l); continue; }
          const p = consumer.originalPositionFor({line: l, column: l === loc.start.line ? loc.start.column : 0,
                                                  bias: SourceMapConsumer.LEAST_UPPER_BOUND});
          if (p && p.line) got.add(p.line);
        }
        byId[id] = [...got];
        got.forEach(x => all.add(x));
      }
      if (consumer && consumer.destroy) consumer.destroy();
      lines[key] = byId;
      executable[key] = [...all].sort((a, b) => a - b);
    }
    const tests = specs.map(s => {
      const hits = {};
      for (const [file, ids] of Object.entries(s.hits || {})) {
        const key = (coverage[file] && coverage[file].path) || file;
        const got = new Set();
        for (const id of ids) (lines[key] && lines[key][id] || []).forEach(x => got.add(x));
        if (got.size) hits[key] = [...got].sort((a, b) => a - b);
      }
      return {id: s.id, description: s.description, status: s.status, hits};
    });
    fs.mkdirSync(path.dirname(out), {recursive: true});
    fs.writeFileSync(out, JSON.stringify({executable, tests}));
    log.info(`per-spec coverage of ${tests.length} spec(s) -> ${out}`);
  }
}
Reporter.$inject = ['logger'];

module.exports = {
  'framework:hr-testcov': ['factory', initFramework],
  'reporter:hr-testcov': ['type', Reporter],
};
