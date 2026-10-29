package ro.victorrentea.humanreview

import com.google.gson.GsonBuilder
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.intellij.ide.projectView.ProjectView
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ApplicationNamesInfo
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.thisLogger
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.impl.EditorTabPresentationUtil
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowId
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.util.concurrency.AppExecutorUtil
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.awt.KeyboardFocusManager
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.URI
import java.net.URLDecoder
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.atomic.AtomicBoolean

// The loopback listener a Human Review page reaches IntelliJ through.
//
// A review page is full of `path:line` references, and a reviewer reading one wants to land
// in the class — in the project window that has *that* checkout open, on the reviewed commit.
// The page cannot do it itself: an `idea://` link goes to whichever project the IDE picks, and
// inside an embedded browser a sandboxed iframe cannot hand a custom scheme to the OS at all.
// What the page *can* do is fetch its own origin, so the server it was loaded from
// (serve-review.py) calls this.
//
// Unlike VS Code — one extension host, so one listener, per window — IntelliJ runs every
// project window in one JVM. So there is ONE listener per IDE process (port 0: the OS picks a
// free one), published as `{app:"idea", port, token, pid}` under ~/.human-review/ide/
// (Registry.kt). `/ping` answers with the folders of *every* open project, so the caller's
// longest-prefix routing works across windows, and the bridge then picks the project inside
// the process by the same rule (Folders.kt).
//
// The same wire protocol as vscode-extension/bridge.js, route for route. `/open-url` shows the
// page in the Human Review tool window (Page.kt) and answers 501 where this IDE has no JCEF, so
// the caller falls back to the OS browser.

@Service(Service.Level.APP)
class HumanReviewBridge : Disposable {
    private val log = thisLogger()
    private val started = AtomicBoolean(false)
    private val gson = GsonBuilder().serializeNulls().disableHtmlEscaping().create()

    @Volatile private var server: HttpServer? = null
    @Volatile private var registryFile: Path? = null
    @Volatile private var token: String? = null
    private var shutdownHook: Thread? = null

    /** The port and token, once listening — for a test that calls its routes. */
    fun address(): Pair<Int, String>? = server?.let { s -> token?.let { s.address.port to it } }

    fun start(registry: Path = Registry.DIR) {
        // One listener per IDE process, however many projects are open.
        if (!started.compareAndSet(false, true)) return
        try {
            val http = HttpServer.create(InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0)
            val secret = Token.mint()
            token = secret
            http.createContext("/") { ex -> handle(ex) }
            // A pool, not the server's single dispatcher thread: an `/open-file` waiting on an
            // EDT busy behind a modal dialog must not hold up `/ping`.
            http.executor = AppExecutorUtil.getAppExecutorService()
            http.start()
            server = http
            val pid = ProcessHandle.current().pid()
            registryFile = Registry.publish(registry, http.address.port, secret, pid)
            // `dispose()` runs on a normal exit; a `kill <pid>` skips services but still runs
            // JVM shutdown hooks, and a stale entry with a dead port is one more thing every
            // caller has to time out on. The hook's task is the JDK's own `Files.deleteIfExists`
            // (OffStack), not a lambda of ours: the JVM's hook table is a static map, and a class
            // of this plugin in it would pin the classloader if the hook were ever left behind.
            registryFile?.let { file ->
                shutdownHook = Thread(OffStack.runnable(null, Files::class.java, "deleteIfExists",
                    Path::class.java to file))
                    .also { Runtime.getRuntime().addShutdownHook(it) }
            }
            log.info("human-review bridge listening on 127.0.0.1:${http.address.port}")
        } catch (e: Exception) {
            log.warn("human-review: listener failed", e)
        }
    }

    /** Stop listening and unpublish, so a later `start()` listens afresh — for tests. */
    fun stop() {
        dispose()
        started.set(false)
    }

    override fun dispose() {
        server?.stop(0)
        server = null
        registryFile?.let { runCatching { Files.deleteIfExists(it) } }
        registryFile = null
        shutdownHook?.let { runCatching { Runtime.getRuntime().removeShutdownHook(it) } }
        shutdownHook = null
    }

    // ------------------------------------------------------------------ plumbing

    private class HttpError(val code: Int, val body: Map<String, Any?>) : Exception()

    private fun send(ex: HttpExchange, code: Int, body: Any) {
        val data = gson.toJson(body).toByteArray(Charsets.UTF_8)
        ex.responseHeaders.add("Content-Type", "application/json")
        ex.sendResponseHeaders(code, data.size.toLong())
        ex.responseBody.use { it.write(data) }
    }

    /** A JSON body, or a 400 for a malformed one. */
    private fun json(ex: HttpExchange): JsonObject {
        val raw = ex.requestBody.use { it.readNBytes(100_001) }
        if (raw.size > 100_000) throw HttpError(413, mapOf("ok" to false, "error" to "body too large"))
        val text = String(raw, Charsets.UTF_8).ifBlank { "{}" }
        return runCatching { JsonParser.parseString(text).asJsonObject }
            .getOrElse { throw HttpError(400, mapOf("ok" to false, "error" to "expected JSON")) }
    }

    private fun query(ex: HttpExchange): Map<String, String> =
        (ex.requestURI.rawQuery ?: "").split('&').filter { it.isNotEmpty() }.associate {
            val k = it.substringBefore('=')
            val v = it.substringAfter('=', "")
            URLDecoder.decode(k, Charsets.UTF_8) to URLDecoder.decode(v, Charsets.UTF_8)
        }

    /** `Ide.onEdt`, a busy EDT answered as a 503. */
    private fun <T> onEdt(modality: ModalityState = ModalityState.defaultModalityState(), block: () -> T): T =
        try { Ide.onEdt(modality, block) } catch (_: IdeBusy) {
            throw HttpError(503, mapOf("ok" to false, "error" to "the IDE is busy (a modal dialog?)"))
        }

    private fun JsonObject.str(k: String): String? = get(k)?.takeIf { it.isJsonPrimitive }?.asString
    private fun JsonObject.raw(k: String): Any? = get(k)?.takeIf { it.isJsonPrimitive }?.asJsonPrimitive?.let {
        when { it.isNumber -> it.asDouble; it.isBoolean -> it.asBoolean; else -> it.asString }
    }
    private fun JsonObject.isFalse(k: String) = get(k)?.let { it.isJsonPrimitive && it.asJsonPrimitive.isBoolean && !it.asBoolean } == true

    // ------------------------------------------------------------------ projects

    private fun lastFocusedProject(): Project? = Ide.lastFocusedProject()
    private fun remember(project: Project) = Ide.remember(project)
    private fun raise(project: Project) = Ide.raise(project)
    private fun firstFolder(project: Project?): String? = Ide.firstFolder(project)

    // ------------------------------------------------------------------ routes

    private fun handle(ex: HttpExchange) {
        try {
            if (!Token.ok(token, ex.requestHeaders.getFirst("x-relay-token"))) {
                return send(ex, 403, mapOf("ok" to false, "error" to "bad token"))
            }
            val route = ex.requestMethod to ex.requestURI.path
            when (route) {
                "GET" to "/ping" -> send(ex, 200, ping())
                "GET" to "/editor-state" -> send(ex, 200, editorState())
                "POST" to "/command" -> command(ex)
                "POST" to "/open-file" -> openFile(ex)
                "POST" to "/open-diff" -> openDiff(ex)
                "POST" to "/review-open" -> reviewOpen(ex)
                "POST" to "/open-url" -> openUrl(ex)
                "POST" to "/open-pr" -> openPr(ex)
                else -> send(ex, 404, mapOf("ok" to false))
            }
        } catch (e: HttpError) {
            runCatching { send(ex, e.code, e.body) }
        } catch (e: Throwable) {
            log.warn("human-review: request failed", e)
            runCatching { send(ex, 500, mapOf("ok" to false, "error" to (e.message ?: e.javaClass.simpleName))) }
        } finally {
            ex.close()
        }
    }

    // Who this IDE is, and which paths it owns. Callers route on `folders` — every folder of
    // every open project, each as its literal path and its realpath. `folder` is a *name*, kept
    // for older callers; a name is not an address.
    private fun ping(): Map<String, Any?> {
        val folders = Folders.dedupe(Folders.openProjects().flatMap { Folders.ofProject(it) })
        val (focused, folder, active) = onEdt(ModalityState.any()) {
            val p = lastFocusedProject()
            Triple(appFocused(), firstFolder(p),
                p?.let { FileEditorManager.getInstance(it).selectedTextEditor?.virtualFile?.path })
        }
        return linkedMapOf(
            "ok" to true,
            "app" to "idea",
            "bridge" to "human-review",
            "ide" to ApplicationNamesInfo.getInstance().fullProductName,
            // Which build of the plugin answers: a hot reload (deploy-local.sh) is proven by it.
            "version" to HumanReviewPlugin.version(),
            "focused" to focused,
            "folder" to folder,
            "folders" to folders,
            "activeFile" to active,
        )
    }

    /** AWT's active window is null in every process that does not hold the keyboard. */
    private fun appFocused() = KeyboardFocusManager.getCurrentKeyboardFocusManager().activeWindow != null

    // What the focused project's editor is showing, for a caller checking that an open landed —
    // read-only. The file, the selection, and the range still highlighted with the rest muted
    // (RangeFocus.kt). `activeTab` is the selected tab's name, a text editor or not: a diff
    // (`/open-diff`) is a tab with no text editor of its own, named `Owner@cb0988f5.java ↔
    // Owner.java`. `lastComment` is what the last PR-comment open did (Comment.kt).
    private fun editorState(): Map<String, Any?> = onEdt(ModalityState.any()) {
        val p = lastFocusedProject()
        val ed = p?.let { FileEditorManager.getInstance(it).selectedTextEditor }
        val tab = p?.let { pr -> FileEditorManager.getInstance(pr).selectedEditor?.file
            ?.let { runCatching { EditorTabPresentationUtil.getEditorTabTitle(pr, it) }.getOrDefault(it.name) } }
        val selection = ed?.let {
            val sel = it.selectionModel
            fun pos(offset: Int) = it.offsetToLogicalPosition(offset)
                .let { lp -> linkedMapOf("line" to lp.line + 1, "character" to lp.column) }
            linkedMapOf("start" to pos(sel.selectionStart), "end" to pos(sel.selectionEnd))
        }
        linkedMapOf(
            "ok" to true,
            "folder" to firstFolder(p),
            "focused" to appFocused(),
            "activeFile" to ed?.virtualFile?.path,
            "activeTab" to tab,
            "selection" to selection,
            "lastComment" to CommentNotice.lastResult(),
            "rangeFocus" to RangeFocus.active(),
        )
    }

    /** What `/command` will run. An allowlist, not any action id: the token gates the caller,
     *  the list gates what a caller can do. */
    private val COMMANDS = setOf(
        // Raise a project window. The page's editor badge asks the window that has the reviewed
        // checkout to come forward. With `&uri=`, the project owning it; without, the project
        // the bridge just acted on, else the last-focused one.
        "workbench.action.focusWindow",
        // The status-bar "human-review" click: serves this project's review when nothing does,
        // then opens the page.
        "humanReview.status",
    )

    /** What `/command` will run on a folder or file, named by `&uri=` — never without one. Kept
     *  apart from COMMANDS so a URI-taking command cannot be called bare (it would act on
     *  whatever happens to be selected), and so the URI is checked before anything runs: a
     *  `file:` URI inside one of the open projects' folders, nothing else. */
    private val URI_COMMANDS = setOf(
        // The Structure tab: a click on a package or a Maven module box shows that folder
        // selected in the owning project's Project view.
        "revealInExplorer",
        // The Demo tab's Running DB link: serve-review.py has just written the running app's
        // database into the checkout's `.idea/dataSources.xml` (the uri). Refreshed here so the
        // Database plugin reloads it, then its tool window is shown in that project.
        "humanReview.openDatabase",
    )

    private fun uriPath(raw: String?): String? = runCatching {
        val u = URI(raw ?: return null)
        if (u.scheme != "file") null else Path.of(u).toString()
    }.getOrNull()

    private fun command(ex: HttpExchange) {
        val q = query(ex)
        val id = q["id"] ?: ""
        val path = uriPath(q["uri"])
        val owner = path?.let { Folders.ownerProject(it) }
        if (id in URI_COMMANDS) {
            if (path == null || owner == null) {
                return send(ex, 400, mapOf("ok" to false, "error" to "$id needs a file: uri inside this IDE's project folders"))
            }
        } else if (id !in COMMANDS) {
            return send(ex, 400, mapOf("ok" to false, "error" to "not allowed: $id"))
        }
        val project: Project? = when (id) {
            "revealInExplorer", "humanReview.openDatabase" -> owner
            else -> owner ?: recentTarget() ?: onEdt(ModalityState.any()) { lastFocusedProject() }
        }
        project ?: return send(ex, 409, mapOf("ok" to false, "error" to "no project is open"))

        when (id) {
            "workbench.action.focusWindow" -> onEdt { raise(project) }
            "humanReview.status" -> ReviewStatus.of(project).click()
            "revealInExplorer" -> {
                val vf = LocalFileSystem.getInstance().refreshAndFindFileByNioFile(Path.of(path!!))
                    ?: return send(ex, 404, mapOf("ok" to false, "error" to "no such file or folder: $path"))
                // The Project view's pane may be built by this activation: off our stack
                // (OffStack), the select handed to `activate` as a JDK-owned Runnable too.
                val tw = onEdt { ToolWindowManager.getInstance(project).getToolWindow(ToolWindowId.PROJECT_VIEW) }
                val select = OffStack.runnable(ProjectView.getInstance(project), ProjectView::class.java, "select",
                    Any::class.java to null, VirtualFile::class.java to vf, java.lang.Boolean.TYPE to true)
                onEdtBusy { OffStack.runOnEdt(if (tw == null) select else OffStack.runnable(tw, ToolWindow::class.java, "activate", Runnable::class.java to select)) }
                onEdt { raise(project) }
                remember(project)
            }
            "humanReview.openDatabase" -> {
                // A storage file changed on disk is reloaded once the VFS has seen the change
                // and the frame is active; both happen here, the raise first.
                val vf = LocalFileSystem.getInstance().refreshAndFindFileByNioFile(Path.of(path!!))
                    ?: return send(ex, 404, mapOf("ok" to false, "error" to "no such file: $path"))
                vf.refresh(false, false)
                val tw = onEdt {
                    raise(project)
                    ToolWindowManager.getInstance(project).getToolWindow(DATABASE_TOOL_WINDOW)
                }
                // Its content (a tree, toolbars) is built on first show: off our stack (OffStack).
                if (tw != null) onEdtBusy { OffStack.runOnEdt(OffStack.runnable(tw, ToolWindow::class.java, "activate", Runnable::class.java to null)) }
                val shown = tw != null
                remember(project)
                return send(ex, 200, mapOf("ok" to true, "folder" to firstFolder(project),
                                           "toolWindow" to shown))
            }
        }
        send(ex, 200, mapOf("ok" to true, "folder" to firstFolder(project)))
    }

    private fun recentTarget(): Project? = Ide.recentTarget()

    /** The Database Tools plugin's tool window (IntelliJ Ultimate, DataGrip); absent in IC. */
    private val DATABASE_TOOL_WINDOW = "Database"

    private fun isTrue(body: JsonObject, k: String) =
        body.get(k)?.let { it.isJsonPrimitive && it.asJsonPrimitive.isBoolean && it.asBoolean } == true

    /** `comment: true` with its line (`commentLine`, else the reference's own) and the comment's
     *  GitHub URL (`commentUrl`), or null when the click is not about a PR comment. */
    private fun commentAim(body: JsonObject, line: Int): CommentAim? {
        if (!isTrue(body, "comment")) return null
        val at = RangeSpan.num(body.raw("commentLine"))?.takeIf { it >= 1 && it % 1.0 == 0.0 }?.toInt() ?: line
        return CommentAim(at, body.str("commentUrl")?.ifEmpty { null })
    }

    // Show a URL in the Human Review tool window, beside the code (Page.kt): the review page,
    // when the run that built it is in this IDE's terminal. http(s) only, in bridge.js's words.
    // `root` (optional, a path in the checkout) picks the project window, the way `/command`'s
    // `uri` does; without it, the project just acted on, else the last-focused one.
    // `preserveFocus` (default true, as in VS Code): the caller is a script in a terminal of
    // this IDE, and stealing the caret would land the next thing typed in the browser. `beside`
    // is accepted for the wire format's sake: a tool window is beside the code by construction.
    // No JCEF here: 501, and the caller opens the OS browser instead.
    private fun openUrl(ex: HttpExchange) {
        val body = json(ex)
        val target = body.str("url") ?: ""
        if (!ReviewPage.isHttp(target)) return send(ex, 400, mapOf("ok" to false, "error" to ReviewPage.HTTP_ONLY))
        val view = PageView.get()
        if (!view.supported()) {
            return send(ex, 501, mapOf("ok" to false, "error" to "no embedded browser in this IDE (JCEF is not available)"))
        }
        val root = body.str("root")?.takeIf { it.isNotEmpty() && Path.of(it).isAbsolute }
        val project = root?.let { Folders.ownerProject(it) } ?: recentTarget() ?: onEdt(ModalityState.any()) { lastFocusedProject() }
            ?: return send(ex, 409, mapOf("ok" to false, "error" to "no project is open"))
        val focus = body.isFalse("preserveFocus")
        onEdt {
            view.show(project, target, focus)
            if (focus) raise(project)
        }
        send(ex, 200, mapOf("ok" to true, "url" to target, "folder" to firstFolder(project), "view" to "tool-window"))
    }

    // Show a pull request in IntelliJ's GitHub "Pull Requests" tool window, its tab selected
    // (PullRequests.kt): the page's "in IntelliJ" beside a review posted to the PR. `url` is the
    // PR's GitHub URL; `root` (optional, a path in the checkout) picks the project window, the
    // way `/open-url`'s does. A refusal is a 409 with a sentence the caller shows the reader.
    private fun openPr(ex: HttpExchange) {
        val body = json(ex)
        val pr = PrOpen.parse(body.str("url"))
            ?: return send(ex, 400, mapOf("ok" to false, "error" to PrOpen.NOT_A_PR))
        val root = body.str("root")?.takeIf { it.isNotEmpty() && Path.of(it).isAbsolute }
        val project = root?.let { Folders.ownerProject(it) } ?: recentTarget() ?: onEdt(ModalityState.any()) { lastFocusedProject() }
            ?: return send(ex, 404, mapOf("ok" to false, "error" to "no project is open"))
        val refused = PrOpen.open(project, pr)
        if (refused != null) return send(ex, 409, mapOf("ok" to false, "error" to refused, "folder" to firstFolder(project)))
        onEdt { raise(project) }
        remember(project)
        send(ex, 200, mapOf("ok" to true, "number" to pr.number, "folder" to firstFolder(project)))
    }

    // Open a file at a line (or a range, `endLine`) in the project that owns it, and raise that
    // project's window (Ide.open).
    private fun openFile(ex: HttpExchange) {
        val body = json(ex)
        val file = body.str("path") ?: ""
        if (!Path.of(file).isAbsolute) return send(ex, 400, mapOf("ok" to false, "error" to "absolute path required"))
        val line = maxOf(1, (RangeSpan.num(body.raw("line")) ?: 1.0).toInt())
        val endLine = body.raw("endLine")
        val warn = body.str("warn")?.takeIf { it.isNotEmpty() }
        val focus = !body.isFalse("focus")
        val comment = commentAim(body, line)

        // The owner by longest prefix; a file no project owns still opens, in the window the
        // reader was last in — the same as VS Code opening any absolute path it is handed.
        val project = Folders.ownerProject(file) ?: onEdt(ModalityState.any()) { lastFocusedProject() }
            ?: return send(ex, 404, mapOf("ok" to false, "error" to "no project is open", "path" to file))
        val vf = Ide.findFile(file)
            ?: return send(ex, 404, mapOf("ok" to false, "error" to "cannot open $file", "path" to file))

        val opened = onEdtBusy { Ide.open(project, vf, line, endLine, warn, focus) }
            ?: return send(ex, 404, mapOf("ok" to false, "error" to "cannot open $file in an editor", "path" to file))
        val out = linkedMapOf<String, Any?>("ok" to true, "path" to opened.path, "line" to opened.line)
        if (opened.ranged) out["endLine"] = RangeSpan.num(endLine)?.toInt()
        if (comment != null) out["comment"] = CommentNotice.offer(project, opened.path, comment.line, comment.url)
        send(ex, 200, out)
    }

    /** A call that waits on the EDT itself (Ide.open, DiffOpen.open), a busy EDT as a 503. */
    private fun <T> onEdtBusy(block: () -> T): T =
        try { block() } catch (_: IdeBusy) {
            throw HttpError(503, mapOf("ok" to false, "error" to "the IDE is busy (a modal dialog?)"))
        }

    // Open a file as a diff in the project that owns it: a committed revision on the left, the
    // working tree on the right (Diff.kt). A refusal is a 409 with a sentence the caller relays
    // verbatim.
    private fun openDiff(ex: HttpExchange) {
        val body = json(ex)
        val file = body.str("file")?.ifEmpty { null } ?: body.str("path") ?: ""
        val line = (RangeSpan.num(body.raw("line")) ?: 0.0).toInt()
        // `head`: a commit's own diff, `base` its parent — a Fixed card's fix commit (Diff.kt).
        val head = body.str("head")?.ifEmpty { null }
        val refused = onEdtBusy {
            if (head != null) CommitOpen.open(file, body.str("base") ?: "", head, line, !body.isFalse("focus"))
            else DiffOpen.open(file, body.str("base") ?: "", line, !body.isFalse("focus"))
        }
        if (refused != null) return send(ex, 409, mapOf("ok" to false, "error" to refused))
        send(ex, 200, mapOf("ok" to true))
    }

    // Open a reference in the window holding the *reviewed* version of the file (ReviewOpen.kt).
    // Any bridge can be asked: it finds the right window itself, here or in another editor.
    private fun reviewOpen(ex: HttpExchange) {
        val body = json(ex)
        val ref = ReviewRef(
            file = body.str("file") ?: "", line = body.raw("line"), endLine = body.raw("endLine"),
            sha = body.str("sha") ?: "", root = body.str("root") ?: "", branch = body.str("branch") ?: "",
            comment = isTrue(body, "comment"),
            commentLine = body.raw("commentLine"),
            commentUrl = body.str("commentUrl"),
        )
        val (status, out) = onEdtBusy { ReviewOpen.open(ref) }
        send(ex, status, out)
    }
}

/** Starts the bridge when the first project opens; later projects find it running. */
class HumanReviewStartup : ProjectActivity {
    override suspend fun execute(project: Project) {
        // Not in a test run: a test starts its own bridge on its own registry, when it wants one.
        if (ApplicationManager.getApplication().isUnitTestMode) return
        service<HumanReviewBridge>().start()
        service<PrStatusFold>().install()
    }
}
