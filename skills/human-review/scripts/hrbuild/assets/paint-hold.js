(function () {
  var h = document.documentElement;
  h.classList.add('tabs-pending');
  // Insurance, and the reason the hold is safe to take at all: whatever happens to the
  // scripts at the foot of the page -- a throw in one of them, a truncated file, a build
  // with no tab layout at all -- `load` still fires, and the panels come back. The page
  // can lose its tab strip; it must never lose its content.
  addEventListener('load', function () { h.classList.remove('tabs-pending'); });
})();
