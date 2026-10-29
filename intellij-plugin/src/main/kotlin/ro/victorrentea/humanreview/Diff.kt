package ro.victorrentea.humanreview

import com.intellij.diff.DiffContentFactory
import com.intellij.diff.DiffDialogHints
import com.intellij.diff.DiffManager
import com.intellij.diff.chains.DiffRequestChain
import com.intellij.diff.chains.SimpleDiffRequestChain
import com.intellij.diff.requests.DiffRequest
import com.intellij.diff.requests.SimpleDiffRequest
import com.intellij.diff.util.DiffUserDataKeys
import com.intellij.diff.util.Side
import com.intellij.openapi.fileTypes.FileTypeManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Pair
import com.intellij.openapi.vfs.VirtualFile
import java.io.File
import java.nio.file.Files
import java.nio.file.Path

// Open a file as a *diff* — a committed revision on the left, the working tree on the right —
// in the project window that owns it. The port of vscode-extension/diff.js.
//
// The Human Review guide's Auto-fixed tab links every applied fix this way. A snippet shows the
// code that is there now, which answers "what does it say" and not "what did you change":
// judging whether a fix was a real finding or the agent overfitting means seeing the delta.
//
// Unlike VS Code, which can only diff two URIs and so writes the before-image to a file under
// `.human-review/.diffbase/`, IntelliJ's diff takes the base side as content: `git show` is
// materialised in memory, and nothing is left behind in the checkout.

/** What `/open-diff` decided before anything is shown: a refusal in words, or the two sides. */
sealed interface DiffPlan {
    data class Refused(val error: String) : DiffPlan

    /** `before` is the file at `base`; `title` the left side's name, `<stem>@<short><ext>`. */
    data class Show(val file: String, val before: ByteArray, val title: String, val short: String) : DiffPlan
}

object DiffOpen {
    // A ref, and nothing that could be read as a flag or a second argument. `git` is spawned
    // without a shell, so this is not about quoting — it is about `--upload-pack=…` and
    // friends arriving from a query string that anything on this machine can compose. Hence
    // the first character: a ref may not open with `-`, which is the whole of the attack.
    //
    // It used to be `[0-9a-fA-F]{7,40}` — a raw sha and nothing else. But every source bar in
    // a Human Review guide is written against `origin/main`, so every one of those handles
    // arrived here and was turned away with "that is not a git ref". A name is not less safe
    // than a sha — it is resolved through git before it reaches anything, and a name git
    // cannot resolve is refused exactly as before. diff.js and serve-review.py carry the same
    // regex; the three are a set.
    val REF_RE = Regex("^[0-9A-Za-z][0-9A-Za-z._/+^~{}-]{0,100}$")

    fun usableRef(base: String?) = base != null && REF_RE.matches(base)

    /**
     * Refuses rather than improvises. If the ref does not resolve, or the file did not exist
     * in it, or the two sides are identical, there is no diff to show — and a diff with an
     * invented left half is worse than no diff, because it looks exactly like evidence.
     *
     * The same checks, in the same order and the same words, as diff.js `openDiff`. `owned`
     * is "a project in this IDE has the file"; `git` and `read` are the outside world, so a
     * test can drive every refusal.
     */
    fun plan(
        file: String,
        base: String,
        owned: (String) -> Boolean,
        git: (cwd: String, args: List<String>) -> ByteArray? = { cwd, a -> Git.bytes(cwd, *a.toTypedArray()) },
        read: (String) -> ByteArray? = { runCatching { Files.readAllBytes(Path.of(it)) }.getOrNull() },
    ): DiffPlan {
        if (file.isEmpty() || !File(file).isAbsolute) return DiffPlan.Refused("absolute path required")
        if (!usableRef(base)) return DiffPlan.Refused("not a usable git ref")
        val real = runCatching { Path.of(file).toRealPath().toString() }.getOrDefault(file)
        if (!owned(real) && !owned(file)) return DiffPlan.Refused("that path is not in this window")
        val name = File(file).name
        val after = read(file) ?: return DiffPlan.Refused("$name is no longer in the working tree")

        val root = git(File(file).parent, listOf("rev-parse", "--show-toplevel"))
            ?.toString(Charsets.UTF_8)?.trim().orEmpty()
        if (root.isEmpty()) return DiffPlan.Refused("that file is not in a git repository")
        val rel = relative(root, file) ?: relative(root, real)
            ?: return DiffPlan.Refused("that file is outside the repository")

        val short = base.take(8)
        val shown = git(root, listOf("show", "$base:$rel"))
            ?: return DiffPlan.Refused("$name does not exist at $short")
        if (shown.contentEquals(after)) return DiffPlan.Refused("$name is unchanged since $short")
        return DiffPlan.Show(file, shown, beforeName(rel, short), short)
    }

    /** `rel` of `file` under `root`, `/`-separated as git wants it, or null when outside. */
    fun relative(root: String, file: String): String? {
        val r = runCatching { Path.of(root).relativize(Path.of(file)) }.getOrNull() ?: return null
        val s = r.toString()
        if (s.isEmpty() || s == ".." || s.startsWith("../") || r.isAbsolute) return null
        return s.replace(File.separatorChar, '/')
    }

    // Named `<stem>@<short><ext>` rather than `<name>@<short>`, so the extension survives and
    // the left pane keeps its syntax highlighting — and so the tab reads
    // `Owner@cb0988f5.java ↔ Owner.java`, which says what is being compared.
    fun beforeName(rel: String, short: String): String {
        val name = rel.substringAfterLast('/')
        val dot = name.lastIndexOf('.')
        return if (dot <= 0) "$name@$short" else "${name.substring(0, dot)}@$short${name.substring(dot)}"
    }

    // ------------------------------------------------------------------ the IDE half

    /**
     * Plan, then show the diff in the project that owns the file and raise its window. Off the
     * EDT. `null` when it is open, else the refusal — a sentence a caller relays verbatim.
     */
    fun open(file: String, base: String, line: Int, focus: Boolean): String? {
        val plan = plan(file, base, owned = { Folders.ownerProject(it) != null })
        if (plan is DiffPlan.Refused) return plan.error
        plan as DiffPlan.Show
        val real = runCatching { Path.of(file).toRealPath().toString() }.getOrDefault(file)
        val project = Folders.ownerProject(file) ?: Folders.ownerProject(real) ?: return "that path is not in this window"
        val vf = Ide.findFile(file) ?: return "${File(file).name} is no longer in the working tree"
        Ide.onEdt {
            show(project, plan, vf, line)
            if (focus) runCatching { Ide.raise(project) }  // the diff is open regardless
        }
        Ide.remember(project)
        return null
    }

    /**
     * EDT only. The base on the left, read-only; the working-tree document on the right,
     * editable — a fix spotted wrong can be corrected right there.
     *
     * `line` lands the diff on the line the click was about instead of at the top of the
     * file. Without it a fix five hundred lines down opens with the reader looking at the
     * package declaration, hunting for the change — which is the hunt the handle they clicked
     * exists to end. It is a line of the right side, the one a caller could have known.
     */
    fun show(project: Project, plan: DiffPlan.Show, vf: VirtualFile, line: Int) {
        val factory = DiffContentFactory.getInstance()
        val left = factory.createFromBytes(project, plan.before, vf)
        val right = factory.create(project, vf)
        val request = SimpleDiffRequest("${plan.title} ↔ ${vf.name}", left, right, plan.title, vf.name)
        if (line > 0) request.putUserData(DiffUserDataKeys.SCROLL_TO_LINE, Pair.create(Side.RIGHT, line - 1))
        // The diff window's editors and toolbars are built off this plugin's stack (OffStack).
        OffStack.later(OffStack.runnable(DiffManager.getInstance(), DiffManager::class.java, "showDiff",
            Project::class.java to project, DiffRequest::class.java to request))
    }
}

// ---------------------------------------------------------------------------------- a commit

/** One file of a commit: its two sides out of git, null where the commit added or deleted it. */
class CommitFile(val path: String, val before: ByteArray?, val after: ByteArray?)

/** What `/open-diff` with a `head` decided: a refusal in words, or the commit's files. */
sealed interface CommitPlan {
    data class Refused(val error: String) : CommitPlan

    /** `index`: the clicked file among `files`; `base`/`short` the parent's and the commit's. */
    class Show(val files: List<CommitFile>, val index: Int, val base: String, val short: String,
               val subject: String) : CommitPlan
}

// A commit's own diff — `base` its parent, `head` the commit — as every file it changed, in one
// diff window that steps between them, opened on the clicked file at the clicked line. A Human
// Review page's Fixed card opens its fix commit this way (Victor, 30 Oct 2026): the fix is a
// commit, so the click shows the commit, not the file against whatever the tree holds since.
// Both sides come out of git; the working tree is never read. The port of diff.js `openCommit`.
object CommitOpen {
    fun plan(
        file: String,
        base: String,
        head: String,
        owned: (String) -> Boolean,
        git: (cwd: String, args: List<String>) -> ByteArray? = { cwd, a -> Git.bytes(cwd, *a.toTypedArray()) },
    ): CommitPlan {
        if (file.isEmpty() || !File(file).isAbsolute) return CommitPlan.Refused("absolute path required")
        if (!DiffOpen.usableRef(base) || !DiffOpen.usableRef(head)) return CommitPlan.Refused("not a usable git ref")
        val real = runCatching { Path.of(file).toRealPath().toString() }.getOrDefault(file)
        if (!owned(real) && !owned(file)) return CommitPlan.Refused("that path is not in this window")
        // The file may be gone from the tree since the commit: git is asked from the nearest
        // folder that is still there.
        var dir: File? = File(file).parentFile
        while (dir != null && !dir.isDirectory) dir = dir.parentFile
        fun text(cwd: String, vararg a: String) = git(cwd, a.toList())?.toString(Charsets.UTF_8)?.trim().orEmpty()
        val root = dir?.let { text(it.path, "rev-parse", "--show-toplevel") }.orEmpty()
        if (root.isEmpty()) return CommitPlan.Refused("that file is not in a git repository")
        val rel = DiffOpen.relative(root, file) ?: DiffOpen.relative(root, real)
            ?: return CommitPlan.Refused("that file is outside the repository")
        val from = text(root, "rev-parse", "--verify", "--quiet", "$base^{commit}")
        val to = text(root, "rev-parse", "--verify", "--quiet", "$head^{commit}")
        if (from.isEmpty() || to.isEmpty()) return CommitPlan.Refused("${(if (from.isEmpty()) base else head).take(8)} is not a commit here")
        val listed = text(root, "diff", "--name-status", "--no-renames", from, to).lines()
            .mapNotNull { l -> l.split('\t').takeIf { it.size >= 2 }?.let { it[0] to it[1] } }
        if (listed.isEmpty()) return CommitPlan.Refused("${to.take(8)} changes nothing")
        val files = listed.map { (st, p) ->
            CommitFile(p, if (st == "A") null else git(root, listOf("show", "$from:$p")),
                       if (st == "D") null else git(root, listOf("show", "$to:$p")))
        }
        return CommitPlan.Show(files, maxOf(0, files.indexOfFirst { it.path == rel }), from.take(8), to.take(8),
                               text(root, "log", "-1", "--format=%s", to))
    }

    /** Plan, then show the commit in the project that owns the file and raise its window. Off
     *  the EDT. `null` when it is open, else the refusal — a sentence a caller relays verbatim. */
    fun open(file: String, base: String, head: String, line: Int, focus: Boolean): String? {
        val plan = plan(file, base, head, owned = { Folders.ownerProject(it) != null })
        if (plan is CommitPlan.Refused) return plan.error
        plan as CommitPlan.Show
        val real = runCatching { Path.of(file).toRealPath().toString() }.getOrDefault(file)
        val project = Folders.ownerProject(file) ?: Folders.ownerProject(real) ?: return "that path is not in this window"
        Ide.onEdt {
            show(project, plan, line)
            if (focus) runCatching { Ide.raise(project) }  // the diff is open regardless
        }
        Ide.remember(project)
        return null
    }

    /** EDT only. One request per file, read-only on both sides, the clicked one selected and
     *  scrolled to `line` (a line of the commit's side). */
    fun show(project: Project, plan: CommitPlan.Show, line: Int) {
        val factory = DiffContentFactory.getInstance()
        val types = FileTypeManager.getInstance()
        val requests = plan.files.mapIndexed { i, f ->
            val name = f.path.substringAfterLast('/')
            val type = types.getFileTypeByFileName(name)
            fun side(bytes: ByteArray?) = if (bytes == null) factory.createEmpty() else factory.createFromBytes(project, bytes, type, name)
            SimpleDiffRequest("${plan.short} ${plan.subject} — ${f.path}", side(f.before), side(f.after),
                              DiffOpen.beforeName(f.path, plan.base), DiffOpen.beforeName(f.path, plan.short)).also {
                if (i == plan.index && line > 0) it.putUserData(DiffUserDataKeys.SCROLL_TO_LINE, Pair.create(Side.RIGHT, line - 1))
            }
        }
        // Off this plugin's stack, like DiffOpen.show.
        OffStack.later(OffStack.runnable(DiffManager.getInstance(), DiffManager::class.java, "showDiff",
            Project::class.java to project, DiffRequestChain::class.java to SimpleDiffRequestChain(requests, plan.index),
            DiffDialogHints::class.java to DiffDialogHints.DEFAULT))
    }
}
