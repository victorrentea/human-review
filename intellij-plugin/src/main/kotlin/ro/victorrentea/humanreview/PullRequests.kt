package ro.victorrentea.humanreview

import com.intellij.openapi.Disposable
import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.ToggleAction
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.FileEditorManagerEvent
import com.intellij.openapi.fileEditor.FileEditorManagerListener
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.util.ui.JBUI
import kotlinx.coroutines.flow.StateFlow
import java.awt.AWTEvent
import java.awt.Component
import java.awt.Container
import java.awt.Dimension
import java.awt.Toolkit
import java.awt.event.AWTEventListener
import java.awt.event.ContainerEvent
import java.awt.event.MouseEvent
import java.lang.reflect.Method
import java.net.URI
import java.util.Collections
import java.util.WeakHashMap
import java.util.concurrent.atomic.AtomicBoolean
import javax.swing.JComponent
import javax.swing.JScrollPane
import javax.swing.JTree
import javax.swing.SwingUtilities
import javax.swing.tree.DefaultMutableTreeNode
import javax.swing.tree.TreePath

// IntelliJ's bundled GitHub plugin shows a pull request in its "Pull Requests" tool window: a tab
// per PR ("#49"), the PR's changes as a tree, and under the tree a status block (checks,
// reviewers) above the review buttons. Three things it does not do on its own, done here:
//
//   PrFollow      the file the editor switches to is selected in the open PR's changes tree;
//   PrStatusFold  a double-click on the status block folds it to one line, giving the tree the room;
//   PrOpen        a PR named by its URL opens there, its tab selected — the review page's
//                 "in IntelliJ" beside a review posted to the PR (`/open-pr`, `open-pr` links).
//
// The GitHub plugin has no public API for any of it: its view models are internal and its UI
// is a Swing tree it is free to rearrange. So nothing here links against it — no class of it in
// a signature, nothing on the compile classpath. The tool window is found by its id, the tree by
// being a JTree whose nodes hold a change, the status block by the name it gives itself
// ("Status check panel", there since 2023), and the PR is opened by reflection over the two
// shapes its view model has had since 2024.3. Whatever is not where it is looked for makes that
// feature a no-op — a plain sentence for the PR open — never an exception in the IDE.
//
// Nor may any of it keep the plugin from unloading (Lifecycle.kt): what makes the GitHub plugin
// build UI is called off this plugin's stack (OffStack); the editor listener is declared in
// a project service's message-bus connection, disposed with the plugin; the AWT listener
// belongs to a service that takes it out on dispose; and what is left on the GitHub plugin's components is a preferred size and a
// client property — JDK values, no class of ours.

private val LOG = logger<PrOpen>()

/** The GitHub plugin's tool window, found by its id and never built from here. */
object PrWindow {
    /** The id it has had since 2019. */
    const val ID = "Pull Requests"

    /** EDT. Its tabs' components — the selected one only, or all — empty when it was never built. */
    fun tabs(project: Project, selectedOnly: Boolean): List<Component> {
        if (project.isDisposed) return emptyList()
        val cm = ToolWindowManager.getInstance(project).getToolWindow(ID)?.contentManagerIfCreated ?: return emptyList()
        return if (selectedOnly) listOfNotNull(cm.selectedContent?.component) else cm.contents.mapNotNull { it.component }
    }

    /** Every component under `root`, itself included, that is a `T`. */
    fun <T : Component> under(root: Component, type: Class<T>): Sequence<T> = sequence {
        val stack = ArrayDeque<Component>().apply { add(root) }
        while (stack.isNotEmpty()) {
            val c = stack.removeLast()
            if (type.isInstance(c)) yield(type.cast(c))
            if (c is Container) stack.addAll(c.components)
        }
    }
}

/** A no-argument public method, called; null for anything that is not there or throws. */
internal fun call(o: Any?, name: String, vararg args: Pair<Class<*>, Any?>): Any? {
    o ?: return null
    val m: Method = runCatching { o.javaClass.getMethod(name, *args.map { it.first }.toTypedArray()) }.getOrNull() ?: return null
    runCatching { m.trySetAccessible() }
    return runCatching { m.invoke(o, *args.map { it.second }.toTypedArray()) }.getOrNull()
}

// ---------------------------------------------------------------------- follow the editor

object PrFollow {
    /**
     * The file a change-tree node stands for: a collaboration-tools `RefComparisonChange` (its
     * after side, else its before side — the GitHub plugin's), a VCS `Change` (its after
     * revision, else its before), or a `FilePath`/`VirtualFile` itself. Null for anything else.
     */
    fun pathOf(o: Any?): String? {
        if (o == null || o is CharSequence) return null
        for (side in listOf("getFilePathAfter", "getFilePathBefore")) call(o, side)?.let { pathOf(it)?.let { p -> return p } }
        for (side in listOf("getAfterRevision", "getBeforeRevision")) call(call(o, side), "getFile")?.let { pathOf(it)?.let { p -> return p } }
        return call(o, "getPath") as? String
    }

    /** The leaf, in any JTree under `root`, whose file is one of `paths`. */
    fun find(root: Component, paths: Set<String>): Pair<JTree, TreePath>? {
        for (tree in PrWindow.under(root, JTree::class.java)) {
            val top = tree.model?.root as? DefaultMutableTreeNode ?: continue
            val nodes = top.depthFirstEnumeration()
            while (nodes.hasMoreElements()) {
                val node = nodes.nextElement() as? DefaultMutableTreeNode ?: continue
                if (node.isLeaf && pathOf(node.userObject) in paths) return tree to TreePath(node.path)
            }
        }
        return null
    }

    /**
     * The editor switched to `file`: select it in the changes tree of the PR tab shown in the
     * Pull Requests tool window, when following is on and the file is one of the PR's. EDT.
     * True when a selection was asked for.
     */
    fun reveal(project: Project, file: VirtualFile): Boolean {
        if (!HumanReviewSettings.get().prFollowEditor || !file.isInLocalFileSystem) return false
        val paths = setOfNotNull(file.path, file.canonicalPath)
        val (tree, path) = PrWindow.tabs(project, selectedOnly = true).firstNotNullOfOrNull { find(it, paths) } ?: return false
        if (tree.selectionPaths?.singleOrNull() == path) return false
        // What the reader's click does: the GitHub plugin's selection listener updates its view
        // model, which may rebuild a diff preview — UI, so built off our stack (OffStack).
        OffStack.later(OffStack.runnable(tree, JTree::class.java, "setSelectionPath", TreePath::class.java to path))
        OffStack.later(OffStack.runnable(tree, JTree::class.java, "scrollPathToVisible", TreePath::class.java to path))
        return true
    }
}

/**
 * The editor's selection, followed: a subscription owned by a project service, which the
 * platform disposes with the plugin — so the connection goes with it. Not a `projectListeners`
 * entry in plugin.xml: a live update (deploy-local.sh) reloads the classes but keeps the
 * descriptor IntelliJ parsed at startup, so a listener declared there only after it would wait
 * for a restart. Started with the bridge (HumanReviewStartup, `pluginLoaded`).
 */
@Service(Service.Level.PROJECT)
class PrFollowService(private val project: Project) : Disposable {
    private val started = AtomicBoolean(false)

    fun start() {
        if (!started.compareAndSet(false, true)) return
        project.messageBus.connect(this).subscribe(FileEditorManagerListener.FILE_EDITOR_MANAGER, object : FileEditorManagerListener {
            override fun selectionChanged(event: FileEditorManagerEvent) {
                val file = event.newFile ?: return
                runCatching { PrFollow.reveal(project, file) }.onFailure { LOG.debug("human-review: follow the editor", it) }
            }
        })
    }

    override fun dispose() {}
}

/** "Follow the Editor", in the changes tree's right-click menu (human-review-github.xml). */
class PrFollowToggle : ToggleAction(), DumbAware {
    override fun getActionUpdateThread() = ActionUpdateThread.BGT
    override fun isSelected(e: AnActionEvent) = HumanReviewSettings.get().prFollowEditor
    override fun setSelected(e: AnActionEvent, state: Boolean) {
        HumanReviewSettings.get().prFollowEditor = state
        // Turned on: catch up with the file already in front.
        val project = e.project ?: return
        if (state) FileEditorManager.getInstance(project).selectedFiles.firstOrNull()?.let { PrFollow.reveal(project, it) }
    }
}

// ---------------------------------------------------------------------- fold the status block

/**
 * The status block, folded: as high as its first row, so "✓ All required checks have passed"
 * still reads and the changes tree above it gets the rest. A double-click anywhere on the block
 * folds or unfolds every one of them, and the choice is remembered (`prStatusFolded`).
 *
 * The block is a component the GitHub plugin names "Status check panel", in a MigLayout column
 * that sizes it by its preferred and minimum height. Folding sets both on it — nothing else is
 * touched, its rows and their bindings stay the plugin's — and unfolding puts back what it had.
 * A block is met as it is added to its parent (an AWT container event) or, after a hot reload,
 * where the tool window holds it.
 */
@Service(Service.Level.APP)
class PrStatusFold : Disposable {
    private val installed = AtomicBoolean(false)
    private val panels: MutableSet<JComponent> = Collections.newSetFromMap(WeakHashMap())
    private var lastToggle = 0L
    private val listener = AWTEventListener { e -> runCatching { onEvent(e) }.onFailure { LOG.debug("human-review: status fold", it) } }

    /** Any thread; once. */
    fun install() {
        if (!installed.compareAndSet(false, true)) return
        Toolkit.getDefaultToolkit().addAWTEventListener(listener, AWTEvent.MOUSE_EVENT_MASK or AWTEvent.CONTAINER_EVENT_MASK)
        ApplicationManager.getApplication().invokeLater { refresh(scan = true) }
    }

    /** EDT: unfold what was folded, and stop listening. Again is nothing. */
    override fun dispose() {
        if (installed.compareAndSet(true, false)) Toolkit.getDefaultToolkit().removeAWTEventListener(listener)
        val all = panels.toList()
        panels.clear()
        all.forEach { unfold(it) }
    }

    private fun onEvent(e: AWTEvent) {
        when (e) {
            is ContainerEvent -> if (e.id == ContainerEvent.COMPONENT_ADDED) {
                val child = e.child
                if (child is JComponent && child.name == NAME) adopt(child)
                // Its content swapped ("Loading status…" for the statuses): measured again.
                else (e.container as? JComponent)?.takeIf { it.name == NAME && it in panels }?.let { apply(it) }
            }
            is MouseEvent -> if (e.id == MouseEvent.MOUSE_CLICKED && e.clickCount == 2 && SwingUtilities.isLeftMouseButton(e)
                && e.`when` != lastToggle && statusPanel(e.component) != null) {
                lastToggle = e.`when`
                HumanReviewSettings.get().prStatusFolded = !HumanReviewSettings.get().prStatusFolded
                refresh(scan = true)
            }
        }
    }

    private fun adopt(panel: JComponent) {
        panels += panel
        apply(panel)
    }

    private fun apply(panel: JComponent) = if (HumanReviewSettings.get().prStatusFolded) fold(panel) else unfold(panel)

    /** EDT. Every block known, folded or not as the setting says; `scan` looks for blocks in every
     *  open project's tool window first. */
    fun refresh(scan: Boolean) {
        if (!installed.get()) return
        if (scan) for (p in Folders.openProjects()) for (tab in PrWindow.tabs(p, selectedOnly = false)) {
            PrWindow.under(tab, JComponent::class.java).filter { it.name == NAME }.forEach { panels += it }
        }
        panels.toList().forEach { apply(it) }
    }

    companion object {
        /** The name the GitHub plugin gives the block (GHPRStatusChecksComponentFactory). */
        const val NAME = "Status check panel"

        /** The block's own preferred and minimum size, kept while folded: `[pref, min]`, each a
         *  Dimension or "none" (computed). JDK values only — a class of ours would leak. */
        private const val SAVED = "human-review.status-fold.saved"
        private const val NONE = "none"

        /** EDT. Fold or unfold every block, and remember it. */
        fun setFolded(folded: Boolean) {
            HumanReviewSettings.get().prStatusFolded = folded
            ApplicationManager.getApplication().getServiceIfCreated(PrStatusFold::class.java)?.refresh(scan = true)
        }

        /** The status block `c` is in, itself included; null outside one. */
        fun statusPanel(c: Component?): JComponent? =
            generateSequence(c) { it.parent }.firstOrNull { it is JComponent && it.name == NAME } as JComponent?

        /** One row of the block: its first visible row (a status line, or "Loading status…"), plus
         *  the insets of the block, its scroll pane and its list. */
        fun rowHeight(panel: JComponent): Int {
            var pad = panel.insets.let { it.top + it.bottom }
            var c: Component? = panel.components.firstOrNull { it.isVisible }
            if (c is JScrollPane) {
                pad += c.insets.let { it.top + it.bottom }
                c = c.viewport?.view
            }
            if (c is JComponent) pad += c.insets.let { it.top + it.bottom }
            val row = (c as? Container)?.components?.firstOrNull { it.isVisible && it.preferredSize.height > 0 }
            return pad + (row?.preferredSize?.height ?: JBUI.scale(24))
        }

        fun fold(panel: JComponent) {
            val saved = panel.getClientProperty(SAVED) as? Array<*> ?: arrayOf<Any>(
                if (panel.isPreferredSizeSet) panel.preferredSize else NONE,
                if (panel.isMinimumSizeSet) panel.minimumSize else NONE,
            ).also { panel.putClientProperty(SAVED, it) }
            // Measured as the plugin left it, then held to one row.
            panel.preferredSize = saved[0] as? Dimension
            panel.minimumSize = saved[1] as? Dimension
            val natural = panel.preferredSize
            val h = minOf(natural.height, rowHeight(panel))
            panel.preferredSize = Dimension(natural.width, h)
            panel.minimumSize = Dimension(minOf(panel.minimumSize.width, natural.width), h)
            panel.revalidate()
            panel.repaint()
        }

        fun unfold(panel: JComponent) {
            val saved = panel.getClientProperty(SAVED) as? Array<*> ?: return
            panel.putClientProperty(SAVED, null)
            panel.preferredSize = saved[0] as? Dimension
            panel.minimumSize = saved[1] as? Dimension
            panel.revalidate()
            panel.repaint()
        }

        fun isFolded(panel: JComponent) = panel.getClientProperty(SAVED) != null
    }
}

/** "Fold the Status Block", in the tool window's right-click menus (human-review-github.xml). */
class PrStatusFoldToggle : ToggleAction(), DumbAware {
    override fun getActionUpdateThread() = ActionUpdateThread.BGT
    override fun isSelected(e: AnActionEvent) = HumanReviewSettings.get().prStatusFolded
    override fun setSelected(e: AnActionEvent, state: Boolean) = PrStatusFold.setFolded(state)
}

// ---------------------------------------------------------------------- open a PR by its URL

object PrOpen {
    data class Pr(val owner: String, val repo: String, val number: Long) {
        val repoUrl get() = "https://github.com/$owner/$repo"
    }

    private val URL = Regex("""^https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)(?:[/?#].*)?$""")

    const val NOT_A_PR = "not a GitHub pull request URL: expected https://github.com/<owner>/<repo>/pull/<number>"
    const val NO_GITHUB = "IntelliJ's GitHub plugin is not enabled here (Settings → Plugins → GitHub)"
    const val UNSUPPORTED = "this IntelliJ's GitHub plugin offers no way to open a pull request by its number — open it from the Pull Requests tool window"
    const val NOT_CONNECTED = "IntelliJ's Pull Requests tool window is not connected to a GitHub repository yet — pick the repository and account in it, then click again"

    fun parse(url: String?): Pr? {
        val m = URL.matchEntire(url?.trim() ?: return null) ?: return null
        return m.groupValues[3].toLongOrNull()?.let { Pr(m.groupValues[1], m.groupValues[2], it) }
    }

    /** The GitHub plugin's project view model, as a service class and the getter of the
     *  StateFlow that holds the view model of the repository it is connected to. */
    private val SHAPES = listOf(
        "org.jetbrains.plugins.github.pullrequest.ui.GHPRProjectViewModel" to "getConnectedProjectVm",            // 2025.2 →
        "org.jetbrains.plugins.github.pullrequest.ui.toolwindow.model.GHPRToolWindowViewModel" to "getProjectVm", // 2024.3 – 2025.1
    )

    /** Actions the GitHub plugin registers: any one of them gives its class loader. */
    private val GITHUB_ACTIONS = listOf("Github.View.Pull.Request", "Github.Create.Pull.Request", "Github.PullRequest.Show")

    fun githubLoader(): ClassLoader? = GITHUB_ACTIONS.firstNotNullOfOrNull { id ->
        runCatching { ActionManager.getInstance().getAction(id) }.getOrNull()?.javaClass?.classLoader
    }

    /**
     * Off the EDT. Show `pr` in `project`'s Pull Requests tool window — its tab selected and the
     * tool window active — the way the GitHub plugin opens a `#123` clicked in a PR description:
     * `openPullRequestInfoAndTimeline`, which finds the PR by number, selects its tab and opens
     * its timeline (the IntelliJ counterpart of VS Code's PR overview). Not connected yet, the
     * tool window is shown and given `waitMs` to connect. Null when asked for, else a sentence
     * for the reader.
     */
    fun open(project: Project, pr: Pr, waitMs: Long = 8_000): String? {
        check(!ApplicationManager.getApplication().isDispatchThread) { "PrOpen.open waits: not on the EDT" }
        val loader = githubLoader() ?: return NO_GITHUB
        val (vmService, getter) = SHAPES.firstNotNullOfOrNull { (name, getter) ->
            val type = runCatching { Class.forName(name, true, loader) }.getOrNull() ?: return@firstNotNullOfOrNull null
            runCatching { project.getService(type) }.getOrNull()?.let { it to getter }
        } ?: return UNSUPPORTED
        val connected = call(vmService, getter) as? StateFlow<*> ?: return UNSUPPORTED
        var vm = connected.value
        if (vm == null) {
            // Shown, the tool window connects by itself to a project's one GitHub repository
            // with one account — the usual case.
            call(vmService, "activate")
            val until = System.currentTimeMillis() + waitMs
            while (vm == null && System.currentTimeMillis() < until) {
                Thread.sleep(150)
                vm = connected.value
            }
            vm ?: return NOT_CONNECTED
        }
        val shows = (call(call(vm, "getRepository"), "getWebURI") as? URI)?.toString()?.trimEnd('/')
        if (shows != null && !shows.equals(pr.repoUrl, ignoreCase = true)) {
            val name = shows.substringAfter("://").substringAfter('/')
            return "IntelliJ's Pull Requests tool window here is on $name, not ${pr.owner}/${pr.repo} — " +
                "switch it (⚙ → Switch Remote), then click again"
        }
        val open = vm.javaClass.methods.firstOrNull {
            it.name == "openPullRequestInfoAndTimeline" && it.parameterTypes.contentEquals(arrayOf(java.lang.Long.TYPE))
        } ?: return UNSUPPORTED
        runCatching { open.trySetAccessible() }
        // From this pooled thread: it launches a coroutine of the plugin's own, which builds the
        // tab and the timeline — never on this plugin's stack.
        return runCatching { open.invoke(vm, pr.number); null }.getOrElse {
            LOG.warn("human-review: opening PR #${pr.number}", it)
            "IntelliJ's GitHub plugin could not open PR #${pr.number}: ${(it.cause ?: it).message ?: it.javaClass.simpleName}"
        }
    }
}
