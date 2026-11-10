package ro.victorrentea.humanreview

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationActivationListener
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.util.IconLoader
import com.intellij.openapi.vfs.VirtualFileManager
import com.intellij.openapi.vfs.newvfs.BulkFileListener
import com.intellij.openapi.vfs.newvfs.events.VFileEvent
import com.intellij.openapi.wm.CustomStatusBarWidget
import com.intellij.openapi.wm.IdeFrame
import com.intellij.openapi.wm.StatusBar
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory
import com.intellij.ui.AnimatedIcon
import com.intellij.ui.JBColor
import com.intellij.ui.components.JBLabel
import com.intellij.util.EnvironmentUtil
import com.intellij.util.concurrency.AppExecutorUtil
import com.intellij.util.ui.JBUI
import com.intellij.util.ui.UIUtil
import java.awt.Color
import java.awt.Cursor
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import java.nio.file.Path
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import javax.swing.JComponent
import javax.swing.SwingConstants

// The UI half of the "human-review" status-bar item (the decision itself is in ReviewProbe.kt).
//
// One timer per project window: 10 s while served, then 5 → 10 → 20 → 30 s while nothing is.
// A VFS event on `.server.json`/`review.html` and the IDE coming to the front refresh at once.
// (VS Code also repaints on the git extension's HEAD change; that needs Git4Idea here, which M1
// does without — a checkout or a commit shows on the next tick, or when the IDE is re-focused.)
//
// A click on grey starts the server (`serve-review.py … --no-open`, which reuses a live one on
// the same folder), then opens the page exactly like a click on green — in the Human Review
// tool window, or the OS browser when the setting says so or the IDE has no JCEF (Page.kt).

@Service(Service.Level.PROJECT)
class ReviewStatus(private val project: Project) : Disposable {
    private val memo = ProbeMemo()
    private val started = AtomicBoolean(false)
    private val running = AtomicBoolean(false)
    @Volatile private var again = false
    @Volatile private var disposed = false
    @Volatile private var idle = 0
    @Volatile private var next: ScheduledFuture<*>? = null
    private val painters = CopyOnWriteArrayList<() -> Unit>()

    @Volatile var current: Probe = Probe(ReviewState.NONE, "")
        private set
    @Volatile var starting = false
        private set

    private val scheduler get() = AppExecutorUtil.getAppScheduledExecutorService()

    fun start() {
        if (!started.compareAndSet(false, true)) return
        project.messageBus.connect(this).subscribe(VirtualFileManager.VFS_CHANGES, object : BulkFileListener {
            override fun after(events: List<VFileEvent>) {
                if (events.any { e ->
                        e.path.contains("/${ReviewProbe.DIR}/") &&
                            (e.path.endsWith(ReviewProbe.IDENTITY) || e.path.endsWith(ReviewProbe.PAGE))
                    }) soon()
            }
        })
        ApplicationManager.getApplication().messageBus.connect(this)
            .subscribe(ApplicationActivationListener.TOPIC, object : ApplicationActivationListener {
                override fun applicationActivated(ideFrame: IdeFrame) = soon()
            })
        scheduler.execute(::tick)
    }

    /** `paint` runs on the EDT after every probe, until `parent` is disposed. */
    fun onChange(parent: Disposable, paint: () -> Unit) {
        painters += paint
        Disposer.register(parent) { painters -= paint }
    }

    private fun repaint() = ApplicationManager.getApplication().invokeLater({ painters.forEach { it() } }) { disposed }

    private fun tick() {
        if (disposed) return
        if (!running.compareAndSet(false, true)) { again = true; return }
        next?.cancel(false)
        try {
            val folders = Folders.ofProject(project).map { it.path }
            val s = ReviewProbe.probeFolders(folders, memo)
            current = s
            idle = if (s.state == ReviewState.ON || s.state == ReviewState.NEAR) 0 else idle + 1
            repaint()
        } catch (_: Exception) {
            // the next tick tries again
        } finally {
            running.set(false)
        }
        if (again) { again = false; return tick() }
        // Served: 10 s, enough to notice a stopped server. Nothing: 5 → 10 → 20 → 30 s; a newly
        // started server is caught at once anyway by the VFS event on `.server.json`.
        val delay = if (idle == 0) 10_000L else minOf(30_000L, 5_000L shl minOf(idle - 1, 3))
        if (!disposed) next = scheduler.schedule(::tick, delay, TimeUnit.MILLISECONDS)
    }

    fun soon() {
        idle = minOf(idle, 1)
        if (!disposed) scheduler.schedule(::tick, 150, TimeUnit.MILLISECONDS)
    }

    /** The status-bar click, and the bridge's `/command?id=humanReview.status`. Any thread. */
    fun click() {
        val s = current
        if (s.state == ReviewState.ON || s.state == ReviewState.NEAR) {
            ReviewPage.open(project, s.url!!)
            return
        }
        if (s.state != ReviewState.OFF || starting) return
        starting = true
        repaint()
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                val script = ReviewProbe.serveScript(HumanReviewSettings.get().serveScript)
                // The login shell's PATH: an IDE started from the Dock has launchd's, without
                // Homebrew's python3.
                val url = ReviewProbe.startServer(s.folder, script, ReviewProbe.python(EnvironmentUtil.getValue("PATH")))
                starting = false
                idle = 0
                tick()  // marker verified now → green/amber without waiting for the timer
                ReviewPage.open(project, url)
            } catch (e: Exception) {
                starting = false
                repaint()
                NotificationGroupManager.getInstance().getNotificationGroup("Human Review")
                    .createNotification("Could not serve ${Path.of(s.folder).fileName}/${ReviewProbe.DIR}: ${e.message}",
                        NotificationType.ERROR)
                    .notify(project)
            }
        }
    }

    override fun dispose() {
        disposed = true
        next?.cancel(false)
    }

    companion object {
        fun of(project: Project): ReviewStatus = project.service()
    }
}

class ReviewStatusWidgetFactory : StatusBarWidgetFactory {
    override fun getId() = ReviewStatusWidget.ID
    override fun getDisplayName() = "Human Review"
    override fun isAvailable(project: Project) = true
    override fun canBeEnabledOn(statusBar: StatusBar) = true
    override fun createWidget(project: Project): StatusBarWidget = ReviewStatusWidget(project)
}

/** A label, not a `TextPresentation`: the state is an icon (and a colour), and a text
 *  presentation has neither. Hidden (not removed) while the project has no review on disk.
 *
 *  The icon is the review page's favicon, rendered from `hrbuild/shared/masthead.py` to PNG
 *  (an SVG of an emoji would draw in whatever font the IDE finds): the face at the laptop while
 *  served, the plain face while the page is only on disk — the same two the browser tab shows,
 *  so the IDE and the tab strip agree at a glance (Victor, 10 Nov 2026: the icon, no text). */
class ReviewStatusWidget(private val project: Project) : CustomStatusBarWidget {
    companion object {
        const val ID = "HumanReview.Status"
        private val GREEN = JBColor(Color(0x2E7D32), Color(0x5FB865))
        private val AMBER = JBColor(Color(0xB26B00), Color(0xE0A030))
        private val SERVED = IconLoader.getIcon("/icons/review-served.png", ReviewStatusWidget::class.java)
        private val STATIC = IconLoader.getIcon("/icons/review-static.png", ReviewStatusWidget::class.java)
    }

    private val label = JBLabel(STATIC).apply {
        border = JBUI.Borders.empty(0, 6)
        horizontalTextPosition = SwingConstants.RIGHT
        cursor = Cursor.getPredefinedCursor(Cursor.HAND_CURSOR)
        isVisible = false
        addMouseListener(object : MouseAdapter() {
            override fun mouseClicked(e: MouseEvent) {
                if (e.button == MouseEvent.BUTTON1) ReviewStatus.of(project).click()
            }
        })
    }

    init {
        // A plugin loaded into a running IDE (installed, or updated without a restart) gets a
        // widget per open frame but no project-opened startup activity: the bridge starts here
        // too. Idempotent — one listener per process however many widgets ask.
        if (!ApplicationManager.getApplication().isUnitTestMode) service<HumanReviewBridge>().start()
        val status = ReviewStatus.of(project)
        status.onChange(this, ::paint)
        status.start()
        paint()
    }

    override fun ID() = ID
    override fun getComponent(): JComponent = label
    override fun install(statusBar: StatusBar) {}
    override fun dispose() {}

    private fun esc(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    private fun short(sha: String?) = (sha ?: "").take(8)
    private fun where(b: String?, sha: String?) = "${if (!b.isNullOrEmpty()) "$b @ " else ""}${short(sha).ifEmpty { "?" }}"

    private fun paint() {
        val status = ReviewStatus.of(project)
        val s = status.current
        if (s.state == ReviewState.NONE) {
            label.isVisible = false
            return
        }
        val name = Path.of(s.folder).fileName?.toString() ?: s.folder
        val title = if (s.title.isNotEmpty()) "<b>${esc(s.title)}</b><br><br>" else ""
        val reviewed = esc(where(s.reviewed?.branch, s.reviewed?.sha))
        val tip: String
        if (s.state == ReviewState.OFF) {
            label.text = ""
            label.icon = if (status.starting) AnimatedIcon.Default.INSTANCE else STATIC
            label.foreground = UIUtil.getInactiveTextColor()
            tip = "Human Review — not served<br><br>$title" +
                "${esc(name)}/${ReviewProbe.DIR}/${ReviewProbe.PAGE} reviews $reviewed, but no review server is serving it.<br><br>" +
                (if (status.starting) "Starting the review server…" else "Click to serve it.")
        } else {
            // Green is the face alone; amber adds a dot, the one part of the item a colour reaches.
            label.text = if (s.state == ReviewState.ON) "" else "●"
            label.icon = SERVED
            val co = s.checkout?.let { esc(where(it.branch, it.head)) } ?: "unknown"
            tip = if (s.state == ReviewState.ON) {
                label.foreground = GREEN
                "Human Review — served on :${s.port}<br><br>$title" +
                    "Reviewing $reviewed, which is what this checkout has.<br><br>"
            } else {
                label.foreground = AMBER
                "Human Review — served on :${s.port}, other commit<br><br>$title" +
                    "Reviewing $reviewed, but this checkout is on $co: " +
                    "links in the page open only files unchanged since.<br><br>"
            } + "Click: open ${esc(s.url ?: "")} " +
                (if (ReviewPage.target() == PageTarget.TOOL_WINDOW) "in the Human Review tool window." else "in the browser.")
        }
        label.toolTipText = "<html>$tip</html>"
        label.isVisible = true
        label.parent?.let { it.revalidate(); it.repaint() }
    }
}
