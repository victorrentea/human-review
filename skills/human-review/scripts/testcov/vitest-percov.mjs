// Per-test V8 coverage for a Vitest suite — testcov.py's `vitest` runner adds this file to
// the project's own setupFiles through a wrapper config, so nothing in the project changes.
//
// Every worker opens an inspector session on itself and starts V8's precise coverage. Around
// each test it takes the coverage (which also resets the counters), so what a test's
// afterEach takes is what that test ran. It writes, into HR_TESTCOV_VITEST_OUT:
//
//   <worker>-<n>.json   {id, title, file, line, status, v8: [{url, functions}]}
//   scripts/<hash>.json {url, source, map}  — each project script once, with the inline
//                                            source map Vite put on it
//
// which is the shape v8-join.js reads for a browser suite. Only the project's own modules
// are kept: a script whose URL is under the project root and outside node_modules.
import {Session} from 'node:inspector/promises';
import {afterAll, afterEach, beforeAll, beforeEach} from 'vitest';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const OUT = process.env.HR_TESTCOV_VITEST_OUT;
const ROOT = process.env.HR_TESTCOV_VITEST_ROOT || process.cwd();
const own = url => {
  const p = url.startsWith('file://') ? new URL(url).pathname : url;
  return p.startsWith(ROOT) && !p.includes('/node_modules/') && !p.includes('/.vite');
};
const hash = s => crypto.createHash('sha1').update(s).digest('hex').slice(0, 16);

let session = null;
const scripts = new Map();          // scriptId -> url
let n = 0;

async function take() {
  const {result} = await session.post('Profiler.takePreciseCoverage');
  return result;
}

if (OUT) {
  beforeAll(async () => {
    fs.mkdirSync(path.join(OUT, 'scripts'), {recursive: true});
    session = new Session();
    session.connect();
    session.on('Debugger.scriptParsed', ({params}) => {
      if (params.url && own(params.url)) scripts.set(params.scriptId, params.url);
    });
    await session.post('Debugger.enable');      // replays scriptParsed for what is loaded
    await session.post('Profiler.enable');
    await session.post('Profiler.startPreciseCoverage', {callCount: true, detailed: true});
  });

  beforeEach(async () => { await take(); });

  afterEach(async ctx => {
    const result = await take();
    const task = ctx.task;
    const file = task.file && task.file.filepath ? path.relative(ROOT, task.file.filepath) : null;
    // `describe > describe > it`, the file itself left out (it is a suite with a filepath).
    const names = [task.name];
    for (let s = task.suite; s && !s.filepath && s.name; s = s.suite) names.unshift(s.name);
    const failed = task.result && (task.result.state === 'fail' || (task.result.errors || []).length);
    const v8 = result.filter(r => own(r.url)).map(r => ({url: r.url, functions: r.functions}));
    fs.writeFileSync(path.join(OUT, `${process.pid}-${++n}.json`), JSON.stringify({
      id: `${file}::${names.join(' > ')}`, title: task.name, file,
      line: task.location ? task.location.line : null,
      status: failed ? 'failed' : 'passed', v8,
    }));
  });

  afterAll(async () => {
    for (const [id, url] of scripts) {
      const target = path.join(OUT, 'scripts', hash(url) + '.json');
      if (fs.existsSync(target)) continue;
      try {
        const {scriptSource} = await session.post('Debugger.getScriptSource', {scriptId: id});
        const m = /\/\/# sourceMappingURL=data:application\/json[^,]*;base64,([A-Za-z0-9+/=]+)\s*$/m
          .exec(scriptSource);
        if (!m) continue;
        const map = JSON.parse(Buffer.from(m[1], 'base64').toString('utf8'));
        // Sources relative to the project root, which is what v8-join.js prefixes with the
        // project's folder in the repository.
        const base = map.sourceRoot ? path.resolve(path.dirname(new URL(url, 'file://').pathname), map.sourceRoot)
          : path.dirname(url.startsWith('file://') ? new URL(url).pathname : url);
        map.sources = (map.sources || []).map(src => path.relative(ROOT, path.isAbsolute(src) ? src : path.resolve(base, src)));
        delete map.sourceRoot;
        fs.writeFileSync(target, JSON.stringify({url, source: scriptSource, map}));
      } catch (e) { /* a script gone with its context: nothing to map */ }
    }
    await session.post('Profiler.stopPreciseCoverage');
    session.disconnect();
  });
}
