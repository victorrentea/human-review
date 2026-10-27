package ro.victorrentea.humanreview

import com.intellij.ide.impl.ProjectUtil
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.fileEditor.TextEditor
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.openapi.wm.IdeFocusManager
import java.lang.ref.WeakReference
import java.nio.file.Path
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/** The EDT did not get to a request within the wait: a modal dialog is up, most likely. */
class IdeBusy : Exception("the IDE is busy (a modal dialog?)")

/** What every entry point — the bridge's routes, `jetbrains://` links — does to the IDE. */
object Ide {
    /**
     * Run on the EDT and wait for the answer. **Everything that reads or moves an editor or a
     * window goes through here**: the callers have threads of their own, and the platform
     * answers those questions only on the Event Dispatch Thread. `any` modality for read-only
     * answers (a Settings dialog open must not make `/ping` hang); the default for anything
     * that opens or moves, which then waits for the dialog — bounded, so a caller gets an
     * answer instead of a socket held forever.
     */
    fun <T> onEdt(modality: ModalityState = ModalityState.defaultModalityState(), block: () -> T): T {
        val f = CompletableFuture<T>()
        ApplicationManager.getApplication().invokeLater({
            try { f.complete(block()) } catch (e: Throwable) { f.completeExceptionally(e) }
        }, modality)
        return try {
            f.get(10, TimeUnit.SECONDS)
        } catch (_: TimeoutException) {
            throw IdeBusy()
        } catch (e: ExecutionException) {
            throw e.cause ?: e
        }
    }

    /** The project whose frame was focused last — the one a reader is looking at. EDT. */
    fun lastFocusedProject(): Project? =
        IdeFocusManager.getGlobalInstance().lastFocusedFrame?.project?.takeIf { !it.isDisposed && !it.isDefault }
            ?: Folders.openProjects().firstOrNull()

    /** `ProjectUtil.focusProjectWindow(…, stealFocusIfAppInactive = true)` is the native raise
     *  the IDE's own "open from the command line" runs: on macOS it activates the application
     *  and brings that project's frame in front of the IDE's other frames. */
    fun raise(project: Project) {
        // Never from a test: a headless run has no frame to raise, and a test run on a desktop
        // must not pull a window in front of whoever is working at it.
        val app = ApplicationManager.getApplication()
        if (app.isUnitTestMode || app.isHeadlessEnvironment) return
        ProjectUtil.focusProjectWindow(project, true)
    }

    fun firstFolder(project: Project?): String? = project?.let { Folders.ofProject(it).firstOrNull()?.name }

    /** The project the last open acted on, for a bare `focusWindow` right after it:
     *  serve-review.py reveals, then raises "the window" with no uri — and in a process hosting
     *  several projects, the window is the one just acted on. */
    @Volatile private var lastTarget: Pair<WeakReference<Project>, Long>? = null

    fun remember(project: Project) {
        lastTarget = WeakReference(project) to System.currentTimeMillis()
    }

    fun recentTarget(): Project? {
        val (ref, at) = lastTarget ?: return null
        return ref.get()?.takeIf { !it.isDisposed && System.currentTimeMillis() - at < 15_000 }
    }

    /** Off the EDT: a refresh that finds a file created outside the IDE must not run inside a
     *  write-intent EDT event. Null for a missing file or a directory. */
    fun findFile(path: String): VirtualFile? =
        LocalFileSystem.getInstance().refreshAndFindFileByNioFile(Path.of(path))?.takeIf { !it.isDirectory }

    /** The answer of an open: where the caret went, and whether a range was focused. */
    data class Opened(val path: String, val line: Int, val ranged: Boolean)

    /**
     * Open `vf` at `line` (1-based; a range with `endLine`) in `project`, and raise that
     * project's window. Opening a file focuses the editor *within* its frame but does not bring
     * the frame (or the IDE) to the front, so a click from a browser in front "did nothing" as
     * far as the reader could see; `raise()` is the native raise. A file no text editor shows
     * (a `.drawio.png`) opens in whichever editor claims it, with no line to land on. Null when
     * nothing opened it. Off the EDT; waits for it.
     */
    fun open(project: Project, vf: VirtualFile, line: Int, endLine: Any?, warn: String?, focus: Boolean): Opened? {
        val line0 = maxOf(1, line) - 1
        val ranged = onEdt {
            val editors = FileEditorManager.getInstance(project)
            // Navigate, then take the text editor it selected: every synchronous FileEditorManager
            // open is deprecated in 2026.3, for a suspend replacement 2024.3 does not have.
            if (!OpenFileDescriptor(project, vf, line0, 0).navigateInEditor(project, true)) {
                OpenFileDescriptor(project, vf).navigate(true)
                if (!editors.isFileOpen(vf)) return@onEdt null
            }
            val editor = (editors.getSelectedEditor(vf) as? TextEditor)?.editor
            // A caller that found the file edited since the commit it is quoting says so, on the
            // line the reader lands on rather than in a toast elsewhere.
            if (editor != null && !warn.isNullOrEmpty()) EditedMark.mark(editor, line0, warn) else EditedMark.clear()
            val ranged = editor != null && RangeFocus.focus(project, editor, line0 + 1, endLine)
            if (focus) runCatching { raise(project) }  // the file is open, which is the part that matters
            ranged
        } ?: return null
        remember(project)
        return Opened(vf.path, line0 + 1, ranged)
    }
}
