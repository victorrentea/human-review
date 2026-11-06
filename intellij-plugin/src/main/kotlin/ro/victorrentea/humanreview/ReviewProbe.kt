package ro.victorrentea.humanreview

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

// "human-review" in the status bar: is a Human Review of this project's checkout being served?
//
//   grey   — `.human-review/review.html` exists, but no serve-review.py is serving it
//   green  — a live server serves `<folder>/.human-review`, and the checkout's HEAD is the
//            reviewed commit (`data-hr-head` on the <html> of review.html)
//   amber  — served, but the checkout is on another commit: links in the page open only the
//            files unchanged since
//   hidden — the project has no review on disk (a grey item in every window is noise)
//
// How it is detected, and why exactly so:
// - `.server.json` (written by serve-review.py beside review.html) gives the port and pid.
//   Only a hint — it survives a `kill -9`, and the port may belong to someone else by now.
// - The proof is the marker (`GET /__human_review__`): key `humanReview`, `served` = exactly
//   our directory, `pid` = the one in the file. Asked ONCE per (port, pid): any GET other than
//   /__watch__ and /__editor__ resets the server's `--idle-minutes` clock, so polling the
//   marker every 10 s would keep it alive forever.
// - After that, each tick is only a pid-liveness check + reading the file: no network. A
//   server that stops normally deletes its `.server.json`; a killed one leaves the file but
//   its pid dies — grey either way.
//
// This file is the IDE-free half (the port of review-status.js's probe), so JUnit can run it
// against a temp directory with the network and the process table stubbed out.

enum class ReviewState(val rank: Int) { NONE(0), OFF(1), NEAR(2), ON(3) }

data class Stamp(val sha: String, val branch: String, val title: String)

data class Checkout(val head: String, val branch: String)

data class Probe(
    val state: ReviewState,
    val folder: String,
    val title: String = "",
    val reviewed: Stamp? = null,
    val port: Int? = null,
    val pid: Long? = null,
    val url: String? = null,
    val checkout: Checkout? = null,
)

/** Marker checks kept between ticks: `verified` = confirmed (dir|port|pid) keys, `rejected` =
 *  those someone else answered (not asked again — no point resetting their idle clock). */
class ProbeMemo {
    val verified: MutableSet<String> = ConcurrentHashMap.newKeySet()
    val rejected: MutableSet<String> = ConcurrentHashMap.newKeySet()
}

/** `ok` = a 200 with JSON; `answered` = something on the port replied at all. */
data class MarkerAnswer(val ok: Boolean, val info: JsonObject? = null, val answered: Boolean = false)

object ReviewProbe {
    const val DIR = ".human-review"
    const val PAGE = "review.html"
    const val IDENTITY = ".server.json"
    const val MARKER = "/__human_review__"
    const val MARKER_KEY = "humanReview"

    fun readJson(file: Path): JsonObject? = runCatching {
        JsonParser.parseString(Files.readString(file)).asJsonObject
    }.getOrNull()

    // What the build stamped on <html> and in <title>; it sits in the first ~1 KB of a file of
    // a few MB, so only the head is read, and only when its mtime changed.
    private val stampCache = ConcurrentHashMap<Path, Pair<Long, Stamp>>()

    fun readStamp(page: Path): Stamp? {
        val mtime = runCatching { Files.getLastModifiedTime(page).toMillis() }.getOrNull() ?: return null
        stampCache[page]?.let { (m, s) -> if (m == mtime) return s }
        val head = runCatching {
            Files.newInputStream(page).use { String(it.readNBytes(8192), Charsets.UTF_8) }
        }.getOrNull() ?: return null
        fun attr(name: String) = Regex("""\b$name="([^"]*)"""").find(head)?.groupValues?.get(1) ?: ""
        val title = (Regex("""<title>([^<]*)</title>""", RegexOption.IGNORE_CASE).find(head)?.groupValues?.get(1) ?: "")
            .replace(Regex("&(amp|lt|gt|quot|#39);")) {
                mapOf("amp" to "&", "lt" to "<", "gt" to ">", "quot" to "\"", "#39" to "'")[it.groupValues[1]]!!
            }
        val stamp = Stamp(attr("data-hr-head"), attr("data-hr-branch"), title.trim())
        stampCache[page] = mtime to stamp
        return stamp
    }

    /** JS truthiness of a JSON value: the server answers `"humanReview": 1`. */
    fun truthy(v: com.google.gson.JsonElement?): Boolean = when {
        v == null || v.isJsonNull -> false
        !v.isJsonPrimitive -> true
        v.asJsonPrimitive.isBoolean -> v.asBoolean
        v.asJsonPrimitive.isNumber -> v.asDouble != 0.0
        else -> v.asString.isNotEmpty()
    }

    fun pidAlive(pid: Long?): Boolean =
        pid != null && pid > 0 && ProcessHandle.of(pid).map { it.isAlive }.orElse(false)

    fun getMarker(port: Int, timeoutMs: Int = 1500): MarkerAnswer {
        val conn = runCatching {
            URI("http://127.0.0.1:$port$MARKER").toURL().openConnection() as HttpURLConnection
        }.getOrNull() ?: return MarkerAnswer(false)
        return try {
            conn.connectTimeout = timeoutMs
            conn.readTimeout = timeoutMs
            val code = conn.responseCode
            if (code != 200) return MarkerAnswer(false, answered = true)
            val body = conn.inputStream.use { String(it.readNBytes(1 shl 20), Charsets.UTF_8) }
            runCatching { MarkerAnswer(true, JsonParser.parseString(body).asJsonObject, true) }
                .getOrDefault(MarkerAnswer(false, answered = true))
        } catch (_: Exception) {
            MarkerAnswer(false, answered = false)
        } finally {
            conn.disconnect()
        }
    }

    fun checkoutHead(folder: String): Checkout? = runCatching {
        val (head, ref) = Git.run(folder, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")!!.lines()
        Checkout(head, if (ref == "HEAD") "detached" else ref)
    }.getOrNull()

    /**
     * The review state of one project folder. The three outside effects come in as parameters,
     * defaulting to the real ones, so a test can drive every branch of the decision.
     */
    fun probeFolder(
        folder: String,
        memo: ProbeMemo = ProbeMemo(),
        alive: (Long?) -> Boolean = ::pidAlive,
        marker: (Int) -> MarkerAnswer = { getMarker(it) },
        head: (String) -> Checkout? = ::checkoutHead,
    ): Probe {
        val dir = Path.of(folder, DIR)
        val stamp = readStamp(dir.resolve(PAGE)) ?: return Probe(ReviewState.NONE, folder)
        val base = Probe(ReviewState.OFF, folder, stamp.title, stamp)

        val rec = readJson(dir.resolve(IDENTITY))
        val port = rec?.get("port")?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asDouble
            ?.takeIf { it % 1.0 == 0.0 }?.toInt()
        val pid = rec?.get("pid")?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asLong
        if (port == null || !alive(pid)) return base
        val real = runCatching { dir.toRealPath().toString() }.getOrDefault(dir.toString())
        val key = "$real|$port|$pid"
        if (key in memo.rejected) return base
        if (key !in memo.verified) {
            val m = marker(port)
            val info = if (m.ok) m.info else null
            val mine = info != null &&
                truthy(info.get(MARKER_KEY)) &&
                info.get("served")?.takeIf { it.isJsonPrimitive }?.asString == real &&
                info.get("pid")?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asLong == pid
            if (mine) {
                memo.verified += key
            } else {
                if (m.answered || info != null) memo.rejected += key  // someone else on the port: stop asking
                return base
            }
        }

        val co = head(folder)
        val at = stamp.sha.isEmpty() || (co != null && co.head.startsWith(stamp.sha))
        return base.copy(
            state = if (at) ReviewState.ON else ReviewState.NEAR,
            port = port, pid = pid, url = "http://127.0.0.1:$port/$PAGE", checkout = co,
        )
    }

    /** The most "lit" of a project's folders (base path and content roots: a served one beats a
     *  grey one). */
    fun probeFolders(folders: List<String>, memo: ProbeMemo, probe: (String, ProbeMemo) -> Probe = { f, m -> probeFolder(f, m) }): Probe {
        var best = Probe(ReviewState.NONE, folders.firstOrNull() ?: "")
        for (f in folders) {
            val s = probe(f, memo)
            if (s.state.rank > best.state.rank) best = s
        }
        return best
    }

    // ------------------------------------------------------------------ serving

    /**
     * serve-review.py: the plugin setting if set, then the installed Claude Code plugin's copy
     * (the one the skill runs), then the newest in the plugin cache, then a checkout in
     * ~/workspace. No hand-written hash — every plugin update changes it.
     */
    fun serveScript(configured: String?, home: String = System.getProperty("user.home")): String? {
        if (!configured.isNullOrBlank() && File(configured).exists()) return configured
        val rel = Path.of("skills", "human-review", "scripts", "serve-review.py")
        val cands = mutableListOf<Path>()
        readJson(Path.of(home, ".claude/plugins/installed_plugins.json"))?.let { installed ->
            val plugins = installed.get("plugins")?.takeIf { it.isJsonObject }?.asJsonObject ?: installed
            val rec = plugins.get("human-review@human-review")
            if (rec != null && rec.isJsonArray) rec.asJsonArray.forEach { r ->
                val p = r.takeIf { it.isJsonObject }?.asJsonObject?.get("installPath")?.takeIf { it.isJsonPrimitive }?.asString
                if (p != null) cands.add(Path.of(p).resolve(rel))
            }
        }
        val cache = Path.of(home, ".claude/plugins/cache/human-review/human-review")
        runCatching {
            Files.list(cache).use { s -> s.map { it.resolve(rel) }.filter { Files.exists(it) }.toList() }
                .sortedByDescending { Files.getLastModifiedTime(it).toMillis() }
        }.getOrNull()?.let { cands.addAll(it) }  // no plugin installed
        cands.add(Path.of(home, "workspace/human-review").resolve(rel))
        return cands.firstOrNull { Files.exists(it) }?.toString()
    }

    /** python3 from `path` (the IDE passes the login shell's PATH, which a Dock-started IDE
     *  would otherwise not have), with fallbacks for a thin one. */
    fun python(path: String?): String {
        val dirs = (path ?: "").split(File.pathSeparator).filter { it.isNotEmpty() } +
            listOf("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
        return dirs.map { File(it, "python3") }.firstOrNull { it.canExecute() }?.path ?: "python3"
    }

    /**
     * Start (or reuse) the server for `<folder>/.human-review` and return the page's URL. The
     * script daemonizes itself (`--_child`, new session) and exits once the port answers, so
     * this waits only for the parent, ~1 s. `--no-open`: the caller opens the page, the same as
     * a click on the served state. Blocking — call it off the EDT.
     */
    fun startServer(folder: String, script: String?, python: String): String {
        script ?: throw IllegalStateException("serve-review.py not found — is the human-review plugin installed?")
        val proc = ProcessBuilder(python, script, Path.of(folder, DIR).toString(), "--idle-minutes", "240", "--no-open")
            .redirectInput(ProcessBuilder.Redirect.from(File("/dev/null")))
            .start()
        val out = StringBuilder()
        val err = StringBuilder()
        val readers = listOf(
            Thread { out.append(proc.inputStream.bufferedReader().readText()) },
            Thread { err.append(proc.errorStream.bufferedReader().readText()) },
        ).onEach { it.isDaemon = true; it.start() }
        if (!proc.waitFor(30, TimeUnit.SECONDS)) {
            proc.destroy()
            throw IllegalStateException("serve-review.py did not answer within 30 s")
        }
        readers.forEach { it.join(1000) }
        val url = out.toString().trim().lines().last().trim()
        if (proc.exitValue() == 0 && Regex("""^http://127\.0\.0\.1:\d+/""").containsMatchIn(url)) return url
        val tail = err.toString().trim().lines().takeLast(5).joinToString("\n").ifBlank { "unexpected output: $url" }
        throw IllegalStateException(tail)
    }
}

object Git {
    // An IDE started from Finder inherits launchd's PATH, which has no Homebrew; the git from
    // the Command Line Tools is always at its absolute path, though.
    private val BINARIES = listOf("git", "/usr/bin/git")

    /**
     * Why this process cannot start git at all, or null when it can. The other two calls answer
     * null for that too, which reads as "not a repository"; so an IDE that Toolbox updated on
     * disk while it ran, whose every process start now fails jspawnhelper's version check, said
     * no window was on the commit (6 Nov 2026). Asked only once a refusal is about to go out.
     */
    fun startFailure(): String? = try {
        val proc = ProcessBuilder(BINARIES.last(), "--version").redirectErrorStream(true).start()
        proc.inputStream.use { it.readAllBytes() }
        proc.waitFor(10, TimeUnit.SECONDS)
        null
    } catch (e: java.io.IOException) {
        e.message ?: "$e"
    }

    /** stdout, trimmed, or null when git failed in that directory. */
    fun run(cwd: String, vararg args: String): String? = bytes(cwd, *args)?.toString(Charsets.UTF_8)?.trim()

    /**
     * stdout as it came, byte for byte, or null when git failed — for `git show <ref>:<path>`,
     * whose answer is compared with the file on disk and must not lose a trailing newline or
     * a byte that is not UTF-8. stderr goes nowhere: a pipe nobody reads fills up, and one
     * closed early kills git with SIGPIPE on its first warning.
     */
    fun bytes(cwd: String, vararg args: String): ByteArray? {
        for (bin in BINARIES) {
            val proc = try {
                ProcessBuilder(listOf(bin) + args).directory(File(cwd))
                    .redirectError(ProcessBuilder.Redirect.DISCARD).start()
            } catch (_: java.io.IOException) {
                continue  // not on this PATH (or no such directory): the next binary
            }
            val out = proc.inputStream.use { it.readAllBytes() }
            if (!proc.waitFor(10, TimeUnit.SECONDS)) { proc.destroy(); return null }
            return if (proc.exitValue() == 0) out else null
        }
        return null
    }
}
