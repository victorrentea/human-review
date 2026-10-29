package ro.victorrentea.humanreview

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.intellij.diff.DiffDialogHints
import com.intellij.diff.DiffManager
import com.intellij.diff.DiffRequestPanel
import com.intellij.diff.chains.DiffRequestChain
import com.intellij.diff.contents.DocumentContent
import com.intellij.diff.merge.MergeRequest
import com.intellij.diff.requests.DiffRequest
import com.intellij.diff.requests.SimpleDiffRequest
import com.intellij.diff.util.DiffUserDataKeys
import com.intellij.diff.util.Side
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.JBProtocolCommand
import com.intellij.openapi.components.service
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.fileEditor.ex.FileEditorManagerEx
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.testFramework.HeavyPlatformTestCase
import com.intellij.testFramework.PlatformTestUtil
import com.intellij.testFramework.replaceService
import com.sun.net.httpserver.HttpServer
import kotlinx.coroutines.runBlocking
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.URI
import java.net.URLEncoder
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The IDE half, headless: a real project on disk with a git repository in it, the bridge
 * listening on loopback with its registry in a temporary directory, the diff viewer replaced by
 * a recorder, and no window raised (Ide.raise is a no-op in a test run). The test runs on the
 * EDT; what the bridge's callers do runs on a pooled thread (`offEdt`), as it does in the IDE,
 * while the EDT keeps dispatching the work they hand it.
 */
class HeadlessIdeTest : HeavyPlatformTestCase() {
    private fun <T> offEdt(block: () -> T): T =
        PlatformTestUtil.waitForFuture(ApplicationManager.getApplication().executeOnPooledThread<T> { block() }, 60_000)

    private lateinit var registry: Path
    private lateinit var repo: TestRepo
    private lateinit var base: String
    private val diffs = CopyOnWriteArrayList<DiffRequest>()

    /** What `DiffManager.showDiff` was asked, instead of a diff window. */
    private inner class Recorder : DiffManager() {
        override fun showDiff(project: Project?, request: DiffRequest) { diffs += request }
        override fun showDiff(project: Project?, request: DiffRequest, hints: DiffDialogHints) { diffs += request }
        override fun showDiff(project: Project?, chain: DiffRequestChain, hints: DiffDialogHints) = error("not used")
        override fun createRequestPanel(project: Project?, parent: Disposable, window: java.awt.Window?): DiffRequestPanel = error("not used")
        override fun showMerge(project: Project?, request: MergeRequest) = error("not used")
    }

    override fun setUp() {
        super.setUp()
        registry = Files.createTempDirectory("hr-registry")
        System.setProperty("human-review.registry.dir", registry.toString())
        ApplicationManager.getApplication().replaceService(DiffManager::class.java, Recorder(), testRootDisposable)
        repo = TestRepo(Files.createDirectories(Path.of(project.basePath!!)).toRealPath())
        repo.write("src/Owner.java", (1..40).joinToString("\n") { "// line $it" } + "\n")
        repo.write("src/Same.java", "class Same {}\n")
        base = repo.commit("one")
        LocalFileSystem.getInstance().refreshAndFindFileByNioFile(repo.dir)?.let { it.refresh(false, true) }
    }

    override fun tearDown() {
        try {
            service<HumanReviewBridge>().stop()
            RangeFocus.clear()
            EditedMark.clear()
            FileEditorManagerEx.getInstanceEx(project).closeAllFiles()
        } catch (e: Throwable) {
            addSuppressedException(e)
        } finally {
            super.tearDown()
        }
    }

    private fun bridge(): Pair<Int, String> {
        val b = service<HumanReviewBridge>()
        b.start(registry)  // once per test JVM; later calls find it running
        return b.address()!!
    }

    private fun post(route: String, body: Map<String, Any?>): Pair<Int, JsonObject> = offEdt { postNow(route, body) }

    private fun postNow(route: String, body: Map<String, Any?>): Pair<Int, JsonObject> {
        val (port, token) = bridge()
        val c = URI("http://127.0.0.1:$port$route").toURL().openConnection() as HttpURLConnection
        c.requestMethod = "POST"
        c.doOutput = true
        c.connectTimeout = 5000
        c.readTimeout = 20000
        c.setRequestProperty("x-relay-token", token)
        c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(Gson().toJson(body).toByteArray()) }
        val code = c.responseCode
        val text = (if (code < 400) c.inputStream else c.errorStream).use { String(it.readAllBytes()) }
        return code to JsonParser.parseString(text).asJsonObject
    }

    private fun path(rel: String) = repo.dir.resolve(rel).toString()

    private fun selectedFile(): String? = FileEditorManager.getInstance(project).selectedTextEditor?.virtualFile?.path

    // ------------------------------------------------------------------ /open-diff

    fun `test open-diff asks the diff viewer for the base on the left and the file on the right`() {
        repo.write("src/Owner.java", (1..40).joinToString("\n") { if (it == 30) "// changed" else "// line $it" } + "\n")
        val (code, body) = post("/open-diff", mapOf("file" to path("src/Owner.java"), "base" to base, "line" to 30, "focus" to true))
        assertEquals(body.toString(), 200, code)
        val req = diffs.single() as SimpleDiffRequest
        val short = base.take(8)
        assertEquals("Owner@$short.java ↔ Owner.java", req.title)
        assertEquals(listOf("Owner@$short.java", "Owner.java"), req.contentTitles)
        val (left, right) = req.contents.map { it as DocumentContent }
        assertTrue(left.document.text.contains("// line 30"))
        assertFalse("the base side is read-only", left.document.isWritable)
        assertTrue(right.document.text.contains("// changed"))
        assertTrue("the working tree side is editable", right.document.isWritable)
        assertEquals(path("src/Owner.java"), right.highlightFile?.path)
        val at = req.getUserData(DiffUserDataKeys.SCROLL_TO_LINE)!!
        assertEquals(Side.RIGHT, at.first)
        assertEquals(29, at.second)
    }

    fun `test open-diff refuses in diff-js words, as a 409`() {
        fun refusal(file: String, base: String) =
            post("/open-diff", mapOf("file" to file, "base" to base)).also { assertEquals(409, it.first) }.second["error"].asString
        assertEquals("Same.java is unchanged since ${base.take(8)}", refusal(path("src/Same.java"), base))
        assertEquals("not a usable git ref", refusal(path("src/Same.java"), "--upload-pack=x"))
        assertEquals("Same.java does not exist at nosuchre", refusal(path("src/Same.java"), "nosuchref"))
        assertEquals("that path is not in this window", refusal("/elsewhere/A.java", base))
        assertTrue(diffs.isEmpty())
    }

    // ------------------------------------------------------------------ /open-pr

    fun `test open-pr refuses what is not a PR, and says so when the GitHub plugin is not there`() {
        val (bad, said) = post("/open-pr", mapOf("url" to "https://github.com/o/r/issues/7"))
        assertEquals(400, bad)
        assertEquals(PrOpen.NOT_A_PR, said["error"].asString)
        // The test IDE loads no GitHub plugin: a sentence for the reader, never a 500.
        val (code, body) = post("/open-pr", mapOf("url" to "https://github.com/o/r/pull/7", "root" to repo.dir.toString()))
        assertEquals(body.toString(), 409, code)
        assertEquals(PrOpen.NO_GITHUB, body["error"].asString)
    }

    // ------------------------------------------------------------------ off this plugin's stack

    /** Every platform method OffStack is handed (Ide, Diff, Bridge) exists, with those parameter
     *  types, in the platform the plugin is built against: a typo would only show when clicked. */
    fun `test the platform calls made off this plugin's stack resolve against this platform`() {
        val vf = LocalFileSystem.getInstance().findFileByNioFile(Path.of(path("src/Owner.java")))!!
        val bool = java.lang.Boolean.TYPE
        OffStack.runnable(OpenFileDescriptor(project, vf, 0, 0), OpenFileDescriptor::class.java, "navigateInEditor",
            Project::class.java to project, bool to true)
        OffStack.runnable(OpenFileDescriptor(project, vf), OpenFileDescriptor::class.java, "navigate", bool to true)
        OffStack.runnable(DiffManager.getInstance(), DiffManager::class.java, "showDiff",
            Project::class.java to project, DiffRequest::class.java to null)
        OffStack.runnable(DiffManager.getInstance(), DiffManager::class.java, "showDiff",
            Project::class.java to project, DiffRequestChain::class.java to null, DiffDialogHints::class.java to DiffDialogHints.DEFAULT)
        val select = OffStack.runnable(com.intellij.ide.projectView.ProjectView.getInstance(project),
            com.intellij.ide.projectView.ProjectView::class.java, "select",
            Any::class.java to null, com.intellij.openapi.vfs.VirtualFile::class.java to vf, bool to true)
        val tw = java.lang.reflect.Proxy.newProxyInstance(javaClass.classLoader, arrayOf(com.intellij.openapi.wm.ToolWindow::class.java)) { _, _, _ -> null }
        OffStack.runnable(tw, com.intellij.openapi.wm.ToolWindow::class.java, "activate", Runnable::class.java to select)
        OffStack.runnable(tw, com.intellij.openapi.wm.ToolWindow::class.java, "activate", Runnable::class.java to null)
    }

    // ------------------------------------------------------------------ /review-open

    fun `test review-open opens the reviewed version here, as a range`() {
        val (code, body) = post("/review-open", mapOf("file" to path("src/Owner.java"), "line" to 10, "endLine" to 12,
            "sha" to base, "root" to repo.dir.toString(), "branch" to "feature"))
        assertEquals(body.toString(), 200, code)
        assertEquals(path("src/Owner.java"), body["path"].asString)
        assertFalse(body["edited"].asBoolean)
        assertEquals(path("src/Owner.java"), selectedFile())
        assertEquals(mapOf("path" to path("src/Owner.java"), "line" to 10, "endLine" to 12), RangeFocus.active())
    }

    fun `test review-open says edited when the working tree moved on`() {
        repo.write("src/Owner.java", "// edited\n" + Files.readString(Path.of(path("src/Owner.java"))))
        val (code, body) = post("/review-open", mapOf("file" to path("src/Owner.java"), "line" to 3,
            "sha" to base, "root" to repo.dir.toString()))
        assertEquals(body.toString(), 200, code)
        assertTrue(body["edited"].asBoolean)
    }

    fun `test review-open with no window on the blob is a 409 with a prompt, and a bad sha a 400`() {
        repo.write("src/Owner.java", "moved on\n")
        repo.commit("two")
        val (code, body) = post("/review-open", mapOf("file" to path("src/Owner.java"), "line" to 3,
            "sha" to base, "root" to repo.dir.toString(), "branch" to "feature"))
        assertEquals(409, code)
        assertEquals("no-window", body["error"].asString)
        assertEquals("No open IntelliJ window is on this commit (feature @ ${base.take(8)})", body["message"].asString)
        assertTrue(body["prompt"].asString.contains("Find a local checkout of this repository whose HEAD is $base"))
        assertEquals(400, post("/review-open", mapOf("file" to path("src/Owner.java"), "sha" to "", "root" to repo.dir.toString())).first)
    }

    fun `test review-open hands over to the bridge whose checkout has the reviewed blob`() {
        // Another checkout at the reviewed content, open in a "VS Code window" that is only a stub
        // on loopback, registered in the test's own registry.
        val other = TestRepo(Files.createTempDirectory("hr-other").toRealPath())
        other.write("src/Owner.java", Files.readString(Path.of(path("src/Owner.java"))))
        other.commit("same content")
        repo.write("src/Owner.java", "moved on\n")
        repo.commit("two")
        val got = CopyOnWriteArrayList<JsonObject>()
        val stub = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0).apply {
            createContext("/") { ex ->
                val reply = when (ex.requestURI.path) {
                    "/ping" -> Gson().toJson(mapOf("ok" to true, "app" to "vscode", "folder" to "other",
                        "folders" to listOf(mapOf("name" to "other", "path" to other.dir.toString(), "realPath" to other.dir.toString()))))
                    "/open-file" -> { got += JsonParser.parseString(String(ex.requestBody.readAllBytes())).asJsonObject; """{"ok":true}""" }
                    else -> """{"ok":false}"""
                }.toByteArray()
                ex.sendResponseHeaders(200, reply.size.toLong())
                ex.responseBody.use { it.write(reply) }
            }
            start()
        }
        try {
            Files.writeString(registry.resolve("vscode-999999.json"),
                """{"app":"vscode","port":${stub.address.port},"token":"t","pid":999999}""")
            val (code, body) = post("/review-open", mapOf("file" to path("src/Owner.java"), "line" to 5, "endLine" to 7,
                "sha" to base, "root" to repo.dir.toString()))
            assertEquals(body.toString(), 200, code)
            assertEquals("vscode", body["app"].asString)
            assertEquals("${other.dir}/src/Owner.java", got.single()["path"].asString)
            assertEquals(5, got.single()["line"].asInt)
            assertEquals(7, got.single()["endLine"].asInt)
        } finally {
            stub.stop(0)
            Files.deleteIfExists(registry.resolve("vscode-999999.json"))
        }
    }

    // ------------------------------------------------------------------ jetbrains://

    private fun q(s: String) = URLEncoder.encode(s, Charsets.UTF_8).replace("+", "%20")

    /** The platform's own entry for `jetbrains://<query>`: finds the command by the name
     *  registered in plugin.xml, parses target and parameters, runs it. */
    private fun link(query: String) = offEdt { runBlocking { JBProtocolCommand.execute(query) } }

    fun `test a jetbrains open link reaches the plugin through the protocol command EP`() {
        link("idea/human-review/open?file=${q(path("src/Owner.java"))}&line=20&endLine=23")
        assertEquals(path("src/Owner.java"), selectedFile())
        assertEquals(mapOf("path" to path("src/Owner.java"), "line" to 20, "endLine" to 23), RangeFocus.active())
    }

    fun `test a jetbrains diff link opens the diff, and a refused one the file`() {
        repo.write("src/Owner.java", "changed\n")
        link("idea/human-review/diff?file=${q(path("src/Owner.java"))}&base=$base&line=1")
        assertEquals("Owner@${base.take(8)}.java ↔ Owner.java", (diffs.single() as SimpleDiffRequest).title)
        link("idea/human-review/diff?file=${q(path("src/Same.java"))}&base=$base&line=1")
        assertEquals(1, diffs.size)
        assertEquals(path("src/Same.java"), selectedFile())
    }

    fun `test a jetbrains review-open link opens the reviewed version`() {
        link("idea/human-review/review-open?file=${q(path("src/Owner.java"))}&line=4&endLine=6&sha=$base" +
            "&root=${q(repo.dir.toString())}&branch=feature")
        assertEquals(mapOf("path" to path("src/Owner.java"), "line" to 4, "endLine" to 6), RangeFocus.active())
    }

    // ------------------------------------------------------------------ /open-url

    /** What `PageView.show` was asked, instead of a Chromium in a tool window. */
    private class PageRecorder(val jcef: Boolean) : PageView {
        val shown = CopyOnWriteArrayList<Triple<Project, String, Boolean>>()
        override fun supported() = jcef
        override fun show(project: Project, url: String, focus: Boolean) { shown += Triple(project, url, focus) }
    }

    private fun pages(jcef: Boolean = true) = PageRecorder(jcef).also {
        ApplicationManager.getApplication().replaceService(PageView::class.java, it, testRootDisposable)
    }

    fun `test open-url refuses anything but http in bridge-js words`() {
        val rec = pages()
        for (url in listOf("file:///tmp/review.html", "javascript:alert(1)", "", "ftp://x")) {
            val (code, body) = post("/open-url", mapOf("url" to url))
            assertEquals(url, 400, code)
            assertEquals(ReviewPage.HTTP_ONLY, body["error"].asString)
        }
        assertTrue(rec.shown.isEmpty())
    }

    fun `test open-url without JCEF is a 501, so the caller opens the OS browser`() {
        // The real view: a headless test JVM has no Chromium, which is exactly an IDE without JCEF.
        assertFalse("headless: no JCEF", PageView.get().supported())
        val (code, body) = post("/open-url", mapOf("url" to "http://127.0.0.1:8765/review.html"))
        assertEquals(body.toString(), 501, code)
        assertFalse(body["ok"].asBoolean)
    }

    fun `test open-url shows the page in the owning project, keeping the caret unless asked`() {
        val rec = pages()
        val (code, body) = post("/open-url", mapOf("url" to "http://127.0.0.1:8765/review.html",
            "root" to repo.dir.resolve(".human-review").toString(), "beside" to true))
        assertEquals(body.toString(), 200, code)
        assertEquals("tool-window", body["view"].asString)
        assertEquals(Triple(project, "http://127.0.0.1:8765/review.html", false), rec.shown.single())
        post("/open-url", mapOf("url" to "https://example.org/r", "preserveFocus" to false))
        assertEquals(Triple(project, "https://example.org/r", true), rec.shown.last())
    }

    // ------------------------------------------------------------------ PR comments

    private val prComment = "https://github.com/victorrentea/petclinic/pull/53#discussion_r1234"

    private fun lastComment(): JsonObject? = offEdt {
        val (port, token) = bridge()
        val c = URI("http://127.0.0.1:$port/editor-state").toURL().openConnection() as HttpURLConnection
        c.setRequestProperty("x-relay-token", token)
        JsonParser.parseString(c.inputStream.use { String(it.readAllBytes()) }).asJsonObject
    }["lastComment"]?.takeIf { it.isJsonObject }?.asJsonObject

    fun `test open-file on a PR comment lands on the lines and offers the thread on GitHub`() {
        val (code, body) = post("/open-file", mapOf("path" to path("src/Owner.java"), "line" to 10, "endLine" to 12,
            "comment" to true, "commentLine" to 12, "commentUrl" to prComment))
        assertEquals(body.toString(), 200, code)
        assertEquals("notified", body["comment"].asString)
        assertEquals(mapOf("path" to path("src/Owner.java"), "line" to 10, "endLine" to 12), RangeFocus.active())
        val last = lastComment()!!
        assertEquals("notified", last["comment"].asString)
        assertEquals(12, last["line"].asInt)
        assertEquals(prComment, last["url"].asString)
    }

    fun `test a comment with no GitHub URL, or another URL, shows nothing`() {
        for (url in listOf(null, "javascript:alert(1)", "https://evil.example/pull/1", "file:///etc/passwd")) {
            val (code, body) = post("/open-file", mapOf("path" to path("src/Owner.java"), "line" to 4,
                "comment" to true, "commentUrl" to url))
            assertEquals(200, code)
            assertEquals("$url", "none", body["comment"].asString)
            assertTrue(lastComment()!!["url"].isJsonNull)
        }
        val (_, plain) = post("/open-file", mapOf("path" to path("src/Owner.java"), "line" to 4))
        assertNull("no comment asked, none answered", plain["comment"])
    }

    fun `test review-open on a PR comment offers the thread too`() {
        val (code, body) = post("/review-open", mapOf("file" to path("src/Owner.java"), "line" to 3, "sha" to base,
            "root" to repo.dir.toString(), "comment" to true, "commentUrl" to prComment))
        assertEquals(body.toString(), 200, code)
        assertEquals("notified", body["comment"].asString)
        assertEquals(3, lastComment()!!["line"].asInt)
    }

    fun `test a jetbrains open link carries the comment`() {
        link("idea/human-review/open?file=${q(path("src/Owner.java"))}&line=7&comment=1&commentLine=8&commentUrl=${q(prComment)}")
        assertEquals(8, lastComment()!!["line"].asInt)
        assertEquals(prComment, lastComment()!!["url"].asString)
    }

    // ------------------------------------------------------------------ the range flake

    private fun pump(ms: Long) {
        val end = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < end) {
            PlatformTestUtil.dispatchAllEventsInIdeEventQueue()
            Thread.sleep(10)
        }
    }

    private fun editorOn(rel: String): Pair<com.intellij.openapi.editor.Editor, com.intellij.openapi.vfs.VirtualFile> {
        val vf = LocalFileSystem.getInstance().refreshAndFindFileByNioFile(Path.of(path(rel)))!!
        return FileEditorManager.getInstance(project).openTextEditor(OpenFileDescriptor(project, vf), true)!! to vf
    }

    // In the IDE a cold file's editor loads in the background, and the open's navigation is
    // deferred until it has (AsyncEditorLoader). In a test run the loader is synchronous, so the
    // late navigation is played here by hand: `navigateIn` is exactly what it runs, and it moves
    // the caret and removes the selection.
    fun `test a navigation landing past the guard drops a focus put on an editor already loaded`() {
        val (editor, vf) = editorOn("src/Owner.java")
        assertTrue(RangeFocus.focus(project, editor, 10, 12) { it.run() })
        pump(RangeSpan.GUARD_MS + 150)
        OpenFileDescriptor(project, vf, 9, 0).navigateIn(editor)
        assertNull("this is the M2 flake: the open's own late move read as the reader's", RangeFocus.active())
    }

    /** The loader's queue of delayed actions, for an editor that is "still loading". */
    private class Loader {
        val queued = mutableListOf<Runnable>()
        fun load() { queued.toList().forEach { it.run() }; queued.clear() }
    }

    fun `test a focus on an editor still loading waits for it, and survives the deferred navigation`() {
        val (editor, vf) = editorOn("src/Owner.java")
        val loader = Loader()
        // The open queued its navigation first; the focus queues behind it.
        loader.queued += Runnable { OpenFileDescriptor(project, vf, 9, 0).navigateIn(editor) }
        assertTrue("a range is promised at once", RangeFocus.focus(project, editor, 10, 12) { loader.queued += it })
        assertNull("nothing painted on an editor still loading", RangeFocus.active())
        pump(RangeSpan.GUARD_MS + 150)
        loader.load()
        pump(50)
        assertEquals(mapOf("path" to path("src/Owner.java"), "line" to 10, "endLine" to 12), RangeFocus.active())
        pump(RangeSpan.GUARD_MS + 150)
        assertNotNull("still there once the guard is long past", RangeFocus.active())
        val sel = editor.selectionModel
        assertEquals(9, editor.document.getLineNumber(sel.selectionStart))
        assertEquals(11, editor.document.getLineNumber(sel.selectionEnd))
    }

    fun `test the next open cancels a focus still waiting for its editor`() {
        val (editor, _) = editorOn("src/Owner.java")
        val loader = Loader()
        RangeFocus.focus(project, editor, 10, 12) { loader.queued += it }
        RangeFocus.clear()
        loader.load()
        assertNull(RangeFocus.active())
    }

    fun `test the real loader runs the focus on a loaded editor at once`() {
        val (editor, _) = editorOn("src/Owner.java")
        assertTrue(RangeFocus.focus(project, editor, 10, 12))
        assertEquals(mapOf("path" to path("src/Owner.java"), "line" to 10, "endLine" to 12), RangeFocus.active())
    }

    // ------------------------------------------------------------------ dynamic unload

    fun `test unloading the plugin closes the port, unpublishes, and takes the highlight off`() {
        val (port, _) = bridge()
        val entry = registry.resolve(Registry.fileName(ProcessHandle.current().pid()))
        assertTrue(Files.exists(entry))
        val (editor, _) = editorOn("src/Owner.java")
        val before = editor.markupModel.allHighlighters.size
        RangeFocus.focus(project, editor, 10, 12)
        EditedMark.mark(editor, 9, "edited")
        assertTrue(editor.markupModel.allHighlighters.size > before)

        val me = com.intellij.ide.plugins.PluginManagerCore.getPlugin(HumanReviewPlugin.ID)
        assertNotNull("the plugin under test is loaded from plugin.xml", me)
        HumanReviewPluginListener().beforePluginUnload(me!!, true)

        assertFalse("idea-<pid>.json is gone", Files.exists(entry))
        assertTrue("the port is closed", runCatching { java.net.Socket("127.0.0.1", port).close() }.isFailure)
        assertNull(RangeFocus.active())
        assertEquals("every highlighter of ours is off the editor", before, editor.markupModel.allHighlighters.size)
        assertNull(CommentNotice.lastResult())
    }
}
