#!/bin/zsh
# Hot-redeploys this plugin into the running IntelliJ, no restart. Adapted from
# ~/workspace/confluent-intellij/deploy-local.sh.
#
# Needs -Didea.auto.reload.plugins=true in idea.vmoptions (Help → Edit Custom VM Options…): the
# IDE then reloads a loaded plugin when one of its jars changes *content* (a new file name is
# ignored), so the jar keeps the file name the IDE already has and only its bytes change.
# The plugin must be installed once from disk first (Settings → Plugins → ⚙ → Install Plugin
# from Disk… → build/distributions/human-review-<version>.zip).
#
# IDE_CONFIG=… picks the IDE's config directory; by default the newest IntelliJIdea* that has
# this plugin installed.
#
# It then waits for the reload (RELOAD_WAIT seconds, 60 by default) and for IntelliJ's verdict
# on the classloader it replaced: since 2026.2 the old one is awaited *after* the new build is
# loaded, for about 20 s, and a leak shows only as "Plugins that were expected to unload but are
# still referenced" in idea.log (and a balloon asking for a restart) — "- load plugin" alone
# proves nothing (30 Oct 2026: 0.3.3 reloaded "✅" and leaked). `./deploy-local.sh --verdict`
# builds and copies nothing: it waits again (RELOAD_WAIT, 300 s by default) for the reload of
# the last copy — after IntelliJ was given focus — and prints the same two lines.
set -e
cd "${0:A:h}"

PLUGIN_DIR=human-review          # the zip's top folder: pluginName in gradle.properties
VERSION=$(sed -n 's/^pluginVersion *= *//p' gradle.properties)
BUILT=build/libs/human-review-$VERSION.jar

if [[ -z "$IDE_CONFIG" ]]; then
  for d in ~/Library/Application\ Support/JetBrains/IntelliJIdea*(N/On); do
    [[ -d "$d/plugins/$PLUGIN_DIR/lib" ]] && { IDE_CONFIG=$d; break; }
  done
fi
[[ -n "$IDE_CONFIG" && -d "$IDE_CONFIG/plugins/$PLUGIN_DIR/lib" ]] || {
  echo "Human Review is not installed in any IntelliJIdea* config — install the zip from disk once:"
  echo "  ./gradlew buildPlugin, then Settings → Plugins → ⚙ → Install Plugin from Disk… → build/distributions/human-review-$VERSION.zip"
  exit 1
}
LIB="$IDE_CONFIG/plugins/$PLUGIN_DIR/lib"
# The installed jar's own file name (not the searchable-options one): a different name is ignored.
JAR=$(cd "$LIB" && print -l human-review-*.jar(N) | grep -v searchableOptions | head -1)
[[ -n "$JAR" ]] || { echo "No human-review-*.jar in $LIB — install the zip from disk once"; exit 1; }
TARGET="$LIB/$JAR"
LOG=~/Library/Logs/JetBrains/${IDE_CONFIG:t}/idea.log
grep -q -- '-Didea.auto.reload.plugins=true' "$IDE_CONFIG/idea.vmoptions" 2>/dev/null ||
  echo "⚠ -Didea.auto.reload.plugins=true is not in $IDE_CONFIG/idea.vmoptions: IntelliJ will not notice the new jar"

MARK=build/last-deploy.logline
if [[ "$1" == "--verdict" ]]; then
  LOG_LINES=$(cat "$MARK" 2>/dev/null || echo 0)
  WAIT=${RELOAD_WAIT:-300}
else
  # buildSearchableOptions boots a headless IDE and is the slow step; a stale index is harmless.
  ./gradlew buildPlugin -x test -x buildSearchableOptions -q
  [[ -f "$BUILT" ]] || { echo "Built jar not found: $BUILT"; exit 1; }

  LOG_LINES=$( [[ -f "$LOG" ]] && wc -l < "$LOG" || echo 0 )
  echo $LOG_LINES > "$MARK"
  # Never overwrite in place: the loaded plugin's classloader still reads the old jar by offset,
  # and a rewritten file gives it ZipExceptions → ClassNotFound → a half-dead plugin.
  # A rename swaps the path to a new file while the old one stays intact for whoever has it open.
  STAGED="${TARGET:h:h}/$JAR.tmp"   # outside lib/ so the IDE never sees it, same volume so mv is a rename
  cp "$BUILT" "$STAGED"
  mv -f "$STAGED" "$TARGET"
  echo "Copied $BUILT over $JAR (${IDE_CONFIG:t}) — waiting for IntelliJ to reload it (click into IntelliJ if nothing happens)…"
  WAIT=${RELOAD_WAIT:-60}
fi

since() {  # what idea.log gained since the copy; all of it if the log rotated meanwhile
  local n=$(wc -l < "$LOG" 2>/dev/null || echo 0)
  (( n < LOG_LINES )) && cat "$LOG" || tail -n +$((LOG_LINES + 1)) "$LOG" 2>/dev/null
}
LEAK='expected to unload but are still referenced|is not unload-safe|failed to unload'

loaded=""
for i in {1..$WAIT}; do
  # `- load plugin …`, anchored: `- unload plugin 'Human Review'` comes first and must not match.
  if grep -q "^- load plugin 'Human Review'" <<< "$(since)"; then loaded=$i; break; fi
  sleep 1
done
if [[ -z "$loaded" ]]; then
  echo "No reload line in $LOG after ${WAIT}s — click into IntelliJ, then run: ${0:A} --verdict"
  exit 2
fi
echo "✅ Reloaded live in IntelliJ after ${loaded}s ($(since | grep "^- load plugin 'Human Review'" | tail -1 | sed 's/^- load plugin //'))"

# The verdict on the build it replaced: silent when its classloader went, a warning when not.
for i in {1..30}; do
  NEW_LOG=$(since)
  if grep -q -E "$LEAK" <<< "$NEW_LOG" && grep -q "ro.victorrentea.human-review" <<< "$NEW_LOG"; then
    echo "❌ The build it replaced did not unload: its classloader is still referenced. IntelliJ runs the new one but"
    echo "   asks for a restart. idea.log:"
    grep -E -A2 "$LEAK|Snapshot analysis|ROOT:" <<< "$NEW_LOG" | head -40 | sed 's/^/   /'
    exit 3
  fi
  sleep 1
done
echo "✅ Unloaded cleanly: idea.log has no 'still referenced' 30 s after the reload"
exit 0
