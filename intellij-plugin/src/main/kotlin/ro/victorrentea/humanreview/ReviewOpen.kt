package ro.victorrentea.humanreview

import com.intellij.openapi.project.Project
import java.io.File
import java.nio.file.Path

// Open a Human Review reference in the window that has *the reviewed version* of the file — or
// say plainly that no window does. The port of vscode-extension/review-open.js.
//
// A reference on the guide quotes a file as it was at the commit the review was built from.
// Handing its absolute path to whichever window the IDE picks is how the reader ended up in
// another checkout's copy, or in a copy three commits later, with nothing saying so. So a
// window qualifies only if the file at its HEAD is the same blob as the file at the reviewed
// commit. Per file, not per commit: commits that do not touch it change nothing the reader
// sees; one that does moves the lines, and then the link would lie.
//
// No qualifying window is an error carrying a prompt for an agent, never a fallback. And
// nothing here checks anything out: a branch switched under a session working in that
// checkout is how someone else's work gets swept away.
//
// The windows are this IDE's project windows *and* every other registered bridge — VS Code's
// windows, another IntelliJ — because the reviewed version may be open in any of them. This
// IDE's own windows win a tie: the reader asked IntelliJ.

/** A window that may hold the reviewed version: its folders, which editor it is, and `key` —
 *  how to reach it (a project here, a peer elsewhere). */
data class Window<K>(val key: K, val app: String, val folders: List<Folder>)

data class Candidate<K>(val window: Window<K>, val folder: String, val top: String, val target: String, val score: Int)

sealed interface Located<out K> {
    data object BadRequest : Located<Nothing>
    data class NoWindow(val message: String, val prompt: String) : Located<Nothing>
    data class Found<K>(val hit: Candidate<K>, val edited: Boolean, val short: String) : Located<K>
}

object ReviewLocate {
    val SHA_RE = Regex("^[0-9a-f]{7,40}$", RegexOption.IGNORE_CASE)

    /** Which editor's windows were looked at, as the refusal names them. */
    private val NAMES = mapOf("idea" to "IntelliJ", "vscode" to "VS Code")

    fun editedText(short: String) = "⚠ Edited since the reviewed commit $short: lines may have moved"

    /** Instead of the refusal when the search could not run: it found nothing because it saw nothing. */
    const val CANNOT_RUN_GIT = "IntelliJ cannot start git, so it cannot look for this commit. " +
        "Updated on disk while it was running? Restart IntelliJ"

    private fun inside(p: String, dir: String) = Folders.inside(p, dir)

    /**
     * `file` is the absolute path the guide was built with, `root` the checkout it was built
     * in, `sha` that checkout's HEAD at build time. `git` runs in a directory and answers
     * trimmed stdout, or null on failure.
     */
    fun <K> locate(
        file: String, sha: String, root: String, branch: String?,
        windows: List<Window<K>>,
        git: (cwd: String, args: List<String>) -> String? = { cwd, a -> Git.run(cwd, *a.toTypedArray()) },
        preferred: String = "idea",
    ): Located<K> {
        if (!File(file).isAbsolute || !File(root).isAbsolute || !SHA_RE.matches(sha)) return Located.BadRequest
        val rel = DiffOpen.relative(root, file) ?: return Located.BadRequest
        val relGit = rel

        // Every repository a window shows that could hold this file: the one each folder sits
        // in, and — for a window opened on a directory above several checkouts — the one the
        // file's own path sits in.
        val candidates = mutableListOf<Candidate<K>>()
        for (w in windows) {
            for (f in w.folders) {
                val tops = LinkedHashSet<String>()
                git(f.realPath.ifEmpty { f.path }, listOf("rev-parse", "--show-toplevel"))?.takeIf { it.isNotEmpty() }?.let { tops += it }
                val owns = inside(file, f.path) || inside(file, f.realPath)
                if (owns) git(File(file).parent, listOf("rev-parse", "--show-toplevel"))?.takeIf { it.isNotEmpty() }?.let { tops += it }
                for (t in tops) {
                    val target = Path.of(t, rel).toString()
                    if (!inside(target, f.realPath) && !inside(target, f.path)) continue
                    // The window owning the very path the guide names is the best claim; the
                    // deeper its folder, the more specific.
                    candidates += Candidate(w, f.name, t, target, if (owns) f.path.length else 0)
                }
            }
        }

        // The blob the guide quoted. The checkout it was built in knows the commit while it is
        // still on this machine; a window's own clone may know it too, once fetched.
        var want = git(root, listOf("rev-parse", "--verify", "--quiet", "$sha:$relGit"))?.takeIf { it.isNotEmpty() }
        for (c in candidates) {
            if (want != null) break
            want = git(c.top, listOf("rev-parse", "--verify", "--quiet", "$sha:$relGit"))?.takeIf { it.isNotEmpty() }
        }

        val seen = LinkedHashSet<String>()
        val hits = mutableListOf<Pair<Candidate<K>, Boolean>>()
        for (c in candidates) {
            val head = git(c.top, listOf("rev-parse", "--short=8", "HEAD"))
            val on = git(c.top, listOf("branch", "--show-current"))
            seen += "${c.folder} (${on?.ifEmpty { null } ?: "detached"} @ ${head ?: "?"})"
            if (want == null) continue
            val have = git(c.top, listOf("rev-parse", "--verify", "--quiet", "HEAD:$relGit"))
            if (have != want) continue
            val status = git(c.top, listOf("status", "--porcelain", "--", relGit))
            hits += c to (status != null && status != "")
        }
        val short = sha.take(8)
        if (hits.isNotEmpty()) {
            val best = hits.sortedWith(compareBy<Pair<Candidate<K>, Boolean>> { if (it.first.window.app == preferred) 0 else 1 }
                .thenByDescending { it.first.score }).first()
            return Located.Found(best.first, best.second, short)
        }

        val on = branch?.ifEmpty { null } ?: git(root, listOf("branch", "--show-current"))?.ifEmpty { null } ?: "detached"
        val remote = git(root, listOf("remote", "get-url", "origin"))?.ifEmpty { null }
        val apps = windows.map { it.app }.toSet()
        return noWindow(relGit, sha, on, remote, root, seen.toList(), editorsNamed(apps, preferred), launchHint(preferred))
    }

    /** "IntelliJ", "VS Code", or both when windows of both were looked at. */
    fun editorsNamed(apps: Set<String>, preferred: String): String {
        val order = listOf(preferred) + NAMES.keys.filter { it != preferred }
        val named = order.filter { it in apps }.map { NAMES.getValue(it) }
        return named.ifEmpty { listOf(NAMES.getValue(preferred)) }.joinToString(" or ")
    }

    fun launchHint(app: String) = if (app == "vscode") "VS Code (`code <dir>`)" else "IntelliJ (`open -a \"IntelliJ IDEA\" <dir>`)"

    /** The refusal, worded as review-open.js words it — the editor named for what was searched. */
    fun noWindow(relGit: String, sha: String, on: String, remote: String?, root: String,
                 open: List<String>, editors: String, launch: String): Located.NoWindow {
        val short = sha.take(8)
        return Located.NoWindow(
            message = "No open $editors window is on this commit ($on @ $short)",
            prompt = listOf(
                "I'm reviewing $relGit at commit $sha (branch $on${remote?.let { ", repo $it" } ?: ""}), " +
                    "but no open $editors window has that version of the file.",
                "Open $editors windows: ${if (open.isNotEmpty()) open.joinToString(", ") else "none"}.",
                "Find a local checkout of this repository whose HEAD is $sha — the review was built in $root — " +
                    "and open it in $launch.",
                "Do not check out, switch, reset or stash in any existing checkout: other sessions may be working in them. " +
                    "If no checkout is at that commit, stop and tell me which one you would use.",
            ).joinToString("\n"),
        )
    }
}

/** The request `/review-open` and `jetbrains://…/review-open` both carry. */
data class ReviewRef(
    val file: String, val line: Any?, val endLine: Any?, val sha: String, val root: String, val branch: String,
    val comment: Boolean = false, val commentLine: Any? = null, val commentUrl: String? = null,
) {
    /** The PR comment this click is about, or null: its line, else the reference's own. */
    fun commentAim(line: Int): CommentAim? = if (!comment) null else CommentAim(
        RangeSpan.num(commentLine)?.takeIf { it >= 1 && it % 1.0 == 0.0 }?.toInt() ?: line,
        commentUrl?.ifEmpty { null })
}

/** The IDE half: which windows exist, and the open itself. */
object ReviewOpen {
    /** How a window is reached: a project of this process, or another bridge. */
    sealed interface Where {
        data class Here(val project: Project) : Where
        data class There(val peer: LivePeer) : Where
    }

    private fun windows(registry: Path): List<Window<Where>> =
        Folders.openProjects().map { Window(Where.Here(it) as Where, "idea", Folders.ofProject(it)) } +
            Peers.live(registry).map { Window(Where.There(it) as Where, it.peer.app, it.folders) }

    /**
     * Locate, then open in the window that qualified. Never throws; the answer is
     * `(status, body)` with the words a caller can show as they are — 200, 400 `bad-request`,
     * 409 `no-window` (with `message` and `prompt`) or `open-failed`, 503 `cannot-run-git`.
     */
    fun open(ref: ReviewRef, registry: Path = Registry.DIR,
             gitFailure: () -> String? = Git::startFailure): Pair<Int, Map<String, Any?>> {
        val line = maxOf(1, (RangeSpan.num(ref.line) ?: 1.0).toInt())
        // Passed on as given: RangeFocus decides whether it is a range worth focusing.
        val endLine = ref.endLine?.takeIf { it != "" }
        val found = ReviewLocate.locate(ref.file, ref.sha, ref.root, ref.branch, windows(registry))
        when (found) {
            is Located.BadRequest -> return 400 to mapOf("ok" to false, "error" to "bad-request")
            is Located.NoWindow -> return gitFailure()?.let {
                503 to linkedMapOf("ok" to false, "error" to "cannot-run-git", "message" to ReviewLocate.CANNOT_RUN_GIT, "detail" to it)
            } ?: (409 to linkedMapOf(
                "ok" to false, "error" to "no-window", "message" to found.message, "prompt" to found.prompt))
            is Located.Found -> Unit
        }
        val hit = found.hit
        val warn = if (found.edited) ReviewLocate.editedText(found.short) else ""
        val ok = linkedMapOf<String, Any?>("ok" to true, "folder" to hit.folder, "path" to hit.target, "edited" to found.edited)
        val failed = 409 to mapOf("ok" to false, "error" to "open-failed",
            "message" to "${hit.folder} did not open ${File(hit.target).name}")
        return when (val w = hit.window.key) {
            is Where.Here -> {
                val vf = Ide.findFile(hit.target) ?: return failed
                val opened = try {
                    Ide.open(w.project, vf, line, endLine, warn, true)
                } catch (e: Exception) {
                    return 409 to mapOf("ok" to false, "error" to "open-failed", "message" to (e.message ?: "$e"))
                }
                if (opened == null) return failed
                ref.commentAim(line)?.let { ok["comment"] = CommentNotice.offer(w.project, opened.path, it.line, it.url) }
                200 to ok
            }
            is Where.There -> {
                val body = linkedMapOf<String, Any?>("path" to hit.target, "line" to line, "endLine" to endLine,
                    "focus" to true, "warn" to warn)
                if (ref.comment) {
                    body["comment"] = true
                    RangeSpan.num(ref.commentLine)?.takeIf { it >= 1 && it % 1.0 == 0.0 }?.let { body["commentLine"] = it.toInt() }
                    ref.commentUrl?.takeIf { it.isNotEmpty() }?.let { body["commentUrl"] = it }
                }
                val res = Peers.call(w.peer.peer, "POST", "/open-file", body, timeoutMs = 8000)
                val said = res?.second
                if (res?.first == 200 && said?.get("ok")?.let { it.isJsonPrimitive && it.asBoolean } == true) {
                    ok["app"] = w.peer.peer.app
                    said.get("comment")?.let { ok["comment"] = it }
                    200 to ok
                } else failed
            }
        }
    }
}
