#!/usr/bin/env node
// Chromium's per-test JS coverage -> covered lines of the app's own sources.
//
//   node v8-join.js <suite dir> <source prefix> <node_modules dir>...
//
// <suite dir> is what petclinic-style fixtures leave under COVERAGE_DIR/<suite>: one JSON
// per test (`v8`: the precise coverage Playwright returned, per script URL) and
// `scripts/*.json` (each script once: url, source, source map). <source prefix> is where
// the bundle's sources live in the repository (the front end's project folder), so a map
// source `src/app/x.ts` becomes `<prefix>/src/app/x.ts`. The node_modules dirs are where
// `source-map` may be found — this skill ships no npm dependencies of its own.
//
// Prints {"executable": {path: [lines]}, "tests": {<json file>: {path: [lines]}}}.
//
// A line counts as run when the innermost V8 range enclosing one of its mapped bytes has a
// non-zero count. V8's ranges nest (a function, the blocks inside it), so a sweep over
// sorted offsets with a stack finds the innermost one without comparing every pair.
'use strict';
const fs = require('fs');
const path = require('path');

const [dir, prefix, ...modules] = process.argv.slice(2);
if (!dir) { console.error('usage: v8-join.js <suite dir> <prefix> <node_modules>...'); process.exit(2); }
const {SourceMapConsumer} = require(require.resolve('source-map', {paths: modules.length ? modules : [process.cwd()]}));

function normalise(src, sourceRoot) {
  let s = (sourceRoot && !/^webpack:/.test(src) ? sourceRoot.replace(/\/?$/, '/') : '') + src;
  s = s.replace(/^webpack:\/\/[^/]*\//, '').replace(/^\/+/, '').replace(/^\.\//, '');
  if (/node_modules|^webpack\/|^\(webpack\)|^ignored\b|^external /.test(s)) return null;
  return (prefix ? prefix.replace(/\/$/, '') + '/' : '') + s;
}

(async () => {
  const scripts = {};                 // url -> sorted [{off, file, line}]
  const executable = {};
  const sdir = path.join(dir, 'scripts');
  for (const f of fs.existsSync(sdir) ? fs.readdirSync(sdir) : []) {
    const s = JSON.parse(fs.readFileSync(path.join(sdir, f), 'utf8'));
    if (!s.map || !s.source) continue;
    const starts = [0];
    for (let i = 0; i < s.source.length; i++) if (s.source.charCodeAt(i) === 10) starts.push(i + 1);
    const c = await new SourceMapConsumer(s.map);
    const points = [];
    c.eachMapping(m => {
      if (!m.source || !m.originalLine) return;
      const file = normalise(m.source, s.map.sourceRoot);
      if (!file) return;
      points.push({off: starts[m.generatedLine - 1] + m.generatedColumn, file, line: m.originalLine});
      (executable[file] ||= new Set()).add(m.originalLine);
    });
    if (c.destroy) c.destroy();
    points.sort((a, b) => a.off - b.off);
    scripts[s.url] = points;
  }

  const tests = {};
  for (const f of fs.readdirSync(dir).filter(x => x.endsWith('.json') && x !== 'run.json')) {
    const t = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
    const hits = {};
    for (const e of t.v8 || []) {
      const points = scripts[e.url];
      if (!points || !points.length) continue;
      const ranges = e.functions.flatMap(fn => fn.ranges)
        .sort((a, b) => a.startOffset - b.startOffset || b.endOffset - a.endOffset);
      const stack = [];
      let r = 0;
      for (const p of points) {
        while (r < ranges.length && ranges[r].startOffset <= p.off) {
          while (stack.length && stack[stack.length - 1].endOffset <= ranges[r].startOffset) stack.pop();
          stack.push(ranges[r++]);
        }
        while (stack.length && stack[stack.length - 1].endOffset <= p.off) stack.pop();
        const inner = stack[stack.length - 1];
        if (inner && inner.count > 0) (hits[p.file] ||= new Set()).add(p.line);
      }
    }
    tests[f] = Object.fromEntries(Object.entries(hits).map(([k, v]) => [k, [...v].sort((a, b) => a - b)]));
  }
  process.stdout.write(JSON.stringify({
    executable: Object.fromEntries(Object.entries(executable).map(([k, v]) => [k, [...v].sort((a, b) => a - b)])),
    tests,
  }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
