const { execFile } = require('child_process');

// VS Code started from Finder inherits launchd's PATH, which has no Homebrew; the git from
// the Command Line Tools is always at its absolute path, though.
const BINARIES = ['git', '/usr/bin/git'];

async function git(cwd, args) {
  let lastError;
  for (const bin of BINARIES) {
    try {
      return await new Promise((resolve, reject) => {
        execFile(bin, args, { cwd }, (err, stdout) => {
          if (err) reject(err);
          else resolve(stdout.trim());
        });
      });
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
      lastError = err;
    }
  }
  throw lastError;
}

module.exports = { git };
