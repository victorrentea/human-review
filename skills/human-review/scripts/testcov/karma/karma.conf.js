// The project's own Karma config, plus per-spec coverage for /human-review's `testcov` step.
//
//   HR_TESTCOV_KARMA_BASE=<abs path of the project's karma.conf.js> \
//   HR_TESTCOV_OUT=<abs path of the json to write> \
//   ng test --watch=false --code-coverage --karma-config=<this file>
//
// Nothing in the project changes: this file loads the project's config first and then
// adds one framework (the browser half, which snapshots Istanbul's counters around each
// spec) and one reporter (the Node half, which maps the counters back to TypeScript lines).
// `--code-coverage` is what makes Istanbul instrument the sources at all.
'use strict';
const path = require('path');

module.exports = function (config) {
  const base = process.env.HR_TESTCOV_KARMA_BASE;
  if (!base) throw new Error('[hr-testcov] HR_TESTCOV_KARMA_BASE is not set');
  require(path.resolve(base))(config);
  config.set({
    plugins: [...(config.plugins || ['karma-*']), require('./plugin.js')],
    frameworks: [...(config.frameworks || []), 'hr-testcov'],
    // The browser's HTML runner is for a human watching; this run has none.
    reporters: [...(config.reporters || ['progress']).filter(r => r !== 'kjhtml'), 'hr-testcov'],
  });
};
