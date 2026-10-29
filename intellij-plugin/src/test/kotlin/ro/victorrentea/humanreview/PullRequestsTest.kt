package ro.victorrentea.humanreview

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.wm.RegisterToolWindowTask
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.testFramework.PlatformTestUtil
import com.intellij.testFramework.fixtures.BasePlatformTestCase
import com.intellij.ui.content.ContentFactory
import java.awt.BorderLayout
import java.awt.Dimension
import java.awt.Toolkit
import java.awt.event.MouseEvent
import javax.swing.BoxLayout
import javax.swing.JLabel
import javax.swing.JPanel
import javax.swing.JScrollPane
import javax.swing.JTree
import javax.swing.tree.DefaultMutableTreeNode
import javax.swing.tree.DefaultTreeModel

// The GitHub plugin's own classes are not loaded here: these stand in for what its Pull Requests
// tool window holds, by the same shapes the code reads — a change with `getFilePathAfter` and
// `getFilePathBefore`, a file path with `getPath`, a block named "Status check panel".
class FakePath(private val p: String) { fun getPath() = p }
class FakeChange(private val before: FakePath?, private val after: FakePath?) {
    fun getFilePathBefore() = before
    fun getFilePathAfter() = after
}
class FakeRevision(private val f: FakePath) { fun getFile() = f }
class FakeVcsChange(private val before: FakeRevision?, private val after: FakeRevision?) {
    fun getBeforeRevision() = before
    fun getAfterRevision() = after
}

class PullRequestsTest : BasePlatformTestCase() {
    private val settings get() = HumanReviewSettings.get()

    override fun tearDown() {
        try {
            settings.prFollowEditor = true
            settings.prStatusFolded = false
        } catch (e: Throwable) {
            addSuppressedException(e)
        } finally {
            super.tearDown()
        }
    }

    // ------------------------------------------------------------------ PrOpen.parse

    fun `test a PR URL is parsed, a page of it too, anything else is not`() {
        assertEquals(PrOpen.Pr("victorrentea", "petclinic", 49), PrOpen.parse("https://github.com/victorrentea/petclinic/pull/49"))
        assertEquals(PrOpen.Pr("o", "r.js", 7), PrOpen.parse("https://github.com/o/r.js/pull/7/files#diff-1"))
        assertEquals("https://github.com/o/r/pull/7?x=1".let { PrOpen.parse(it)?.number }, 7L)
        assertEquals("https://github.com/o/r", PrOpen.parse("https://github.com/o/r/pull/7")!!.repoUrl)
        for (bad in listOf(null, "", "http://github.com/o/r/pull/7", "https://gitlab.com/o/r/pull/7",
                           "https://github.com/o/r/issues/7", "https://github.com/o/r/pull/", "https://github.com/o/r/pull/7x",
                           "https://github.com/o/r/pull/7\nhttps://evil")) {
            assertNull(bad, PrOpen.parse(bad))
        }
    }

    fun `test without the GitHub plugin the open says so, and throws nothing`() {
        val pr = PrOpen.parse("https://github.com/o/r/pull/7")!!
        val said = PlatformTestUtil.waitForFuture(ApplicationManager.getApplication().executeOnPooledThread<String?> {
            PrOpen.open(project, pr, waitMs = 0)
        }, 10_000)
        assertEquals(PrOpen.NO_GITHUB, said)
    }

    // ------------------------------------------------------------------ follow the editor

    fun `test a change node names its file, after side first`() {
        assertEquals("/r/New.java", PrFollow.pathOf(FakeChange(FakePath("/r/Old.java"), FakePath("/r/New.java"))))
        assertEquals("/r/Gone.java", PrFollow.pathOf(FakeChange(FakePath("/r/Gone.java"), null)))
        assertEquals("/r/A.java", PrFollow.pathOf(FakeVcsChange(FakeRevision(FakePath("/r/B.java")), FakeRevision(FakePath("/r/A.java")))))
        assertEquals("/r/A.java", PrFollow.pathOf(FakePath("/r/A.java")))
        assertNull(PrFollow.pathOf("a directory, by name"))
        assertNull(PrFollow.pathOf(Any()))
        assertNull(PrFollow.pathOf(null))
    }

    private fun changesTree(vararg files: String): JTree {
        val root = DefaultMutableTreeNode("root")
        val dir = DefaultMutableTreeNode(FakePath("/r/src")).also { root.add(it) }
        for (f in files) dir.add(DefaultMutableTreeNode(FakeChange(FakePath(f), FakePath(f))))
        return JTree(DefaultTreeModel(root)).apply { collapseRow(1) }
    }

    fun `test the file is found among the leaves of a tree, however deep, and a folder never is`() {
        val tree = changesTree("/r/src/A.java", "/r/src/B.java")
        val tab = JPanel(BorderLayout()).apply { add(JLabel("#49"), BorderLayout.NORTH); add(JScrollPane(tree)) }
        val (found, path) = PrFollow.find(tab, setOf("/r/src/B.java"))!!
        assertSame(tree, found)
        assertEquals("/r/src/B.java", PrFollow.pathOf((path.lastPathComponent as DefaultMutableTreeNode).userObject))
        assertNull(PrFollow.find(tab, setOf("/r/src/C.java")))
        assertNull("the folder node is not a leaf", PrFollow.find(tab, setOf("/r/src")))
    }

    /** A tool window called "Pull Requests" whose one tab holds `component`. */
    private fun pullRequestsWindow(component: javax.swing.JComponent) {
        val tw = ToolWindowManager.getInstance(project).registerToolWindow(RegisterToolWindowTask.notClosable(PrWindow.ID))
        onTearDown { ToolWindowManager.getInstance(project).unregisterToolWindow(PrWindow.ID) }
        val content = ContentFactory.getInstance().createContent(component, "#49", false)
        tw.contentManager.addContent(content)
        tw.contentManager.setSelectedContent(content)
    }

    private fun onTearDown(undo: () -> Unit) = com.intellij.openapi.util.Disposer.register(testRootDisposable) { undo() }

    fun `test switching the editor to a file of the PR selects it in the changes tree, and scrolls there`() {
        val file = myFixture.addFileToProject("src/B.java", "class B {}").virtualFile
        val other = myFixture.addFileToProject("src/Other.java", "class Other {}").virtualFile
        val tree = changesTree("/no/where/A.java", file.path)
        pullRequestsWindow(JPanel(BorderLayout()).apply { add(JScrollPane(tree)) })

        assertTrue(PrFollow.reveal(project, file))
        PlatformTestUtil.dispatchAllEventsInIdeEventQueue()
        assertEquals(file.path, PrFollow.pathOf((tree.selectionPath!!.lastPathComponent as DefaultMutableTreeNode).userObject))
        assertTrue("its folder is expanded to show it", tree.isVisible(tree.selectionPath))
        assertFalse("already selected: nothing to do", PrFollow.reveal(project, file))
        assertFalse("not one of the PR's files", PrFollow.reveal(project, other))

        settings.prFollowEditor = false
        tree.clearSelection()
        assertFalse("following is off", PrFollow.reveal(project, file))
        PlatformTestUtil.dispatchAllEventsInIdeEventQueue()
        assertNull(tree.selectionPath)
    }

    fun `test the editor listener follows, through the platform's own event`() {
        val file = myFixture.addFileToProject("src/C.java", "class C {}").virtualFile
        val tree = changesTree(file.path)
        pullRequestsWindow(JPanel(BorderLayout()).apply { add(JScrollPane(tree)) })
        myFixture.openFileInEditor(file)
        PlatformTestUtil.dispatchAllEventsInIdeEventQueue()
        assertEquals(file.path, tree.selectionPath?.let { PrFollow.pathOf((it.lastPathComponent as DefaultMutableTreeNode).userObject) })
    }

    // ------------------------------------------------------------------ fold the status block

    /** The GitHub plugin's block, as built by GHPRStatusChecksComponentFactory: a named wrapper
     *  around a scroll pane around a vertical list of status rows. */
    private fun statusBlock(): Pair<JPanel, List<JLabel>> {
        val rows = (1..4).map { JLabel("status $it").apply { preferredSize = Dimension(120, 26) } }
        val list = JPanel().apply { layout = BoxLayout(this, BoxLayout.Y_AXIS); rows.forEach { add(it) } }
        val block = JPanel(BorderLayout()).apply { name = PrStatusFold.NAME; add(JScrollPane(list).apply { border = null }) }
        return block to rows
    }

    fun `test folded, the block is one row high, and unfolded it is the plugin's again`() {
        val (block, rows) = statusBlock()
        val natural = block.preferredSize
        assertTrue(natural.height >= 4 * 26)
        PrStatusFold.fold(block)
        assertEquals(26, block.preferredSize.height)
        assertEquals(26, block.minimumSize.height)
        assertEquals(natural.width, block.preferredSize.width)
        assertSame(block, PrStatusFold.statusPanel(rows[2]))
        PrStatusFold.fold(block)  // again: measured from the plugin's size, not from the fold
        assertEquals(26, block.preferredSize.height)
        PrStatusFold.unfold(block)
        assertFalse(block.isPreferredSizeSet)
        assertFalse(block.isMinimumSizeSet)
        assertEquals(natural, block.preferredSize)
        assertFalse(PrStatusFold.isFolded(block))
    }

    fun `test a size the plugin set itself is put back as it was`() {
        val (block, _) = statusBlock()
        block.preferredSize = Dimension(300, 90)
        PrStatusFold.fold(block)
        assertEquals(Dimension(300, 26), block.preferredSize)
        PrStatusFold.unfold(block)
        assertEquals(Dimension(300, 90), block.preferredSize)
        assertNull("nothing outside a block", PrStatusFold.statusPanel(JLabel()))
    }

    fun `test a block added while folded is folded, a double-click unfolds it, and dispose leaves nothing behind`() {
        val fold = PrStatusFold()
        try {
            fold.install()
            settings.prStatusFolded = true
            val (block, rows) = statusBlock()
            JPanel().add(block)  // the details panel adding it: an AWT container event
            assertTrue(PrStatusFold.isFolded(block))

            val click = MouseEvent(rows[1], MouseEvent.MOUSE_CLICKED, System.currentTimeMillis(), 0, 5, 5, 2, false, MouseEvent.BUTTON1)
            rows[1].dispatchEvent(click)
            assertFalse(settings.prStatusFolded)
            assertFalse(PrStatusFold.isFolded(block))
            rows[1].dispatchEvent(MouseEvent(rows[1], MouseEvent.MOUSE_CLICKED, click.`when` + 500, 0, 5, 5, 1, false, MouseEvent.BUTTON1))
            assertFalse("a single click is not a toggle", settings.prStatusFolded)
            rows[1].dispatchEvent(MouseEvent(rows[1], MouseEvent.MOUSE_CLICKED, click.`when` + 900, 0, 5, 5, 2, false, MouseEvent.BUTTON1))
            assertTrue(settings.prStatusFolded)
            assertTrue(PrStatusFold.isFolded(block))

            fold.dispose()
            assertFalse("unfolded on unload", PrStatusFold.isFolded(block))
            assertFalse(block.isPreferredSizeSet)
            val ours = Toolkit.getDefaultToolkit().awtEventListeners
                .map { (it as? java.awt.event.AWTEventListenerProxy)?.listener ?: it }
                .filter { it.javaClass.name.startsWith("ro.victorrentea.") }
            assertTrue("no AWT listener of ours is left: $ours", ours.isEmpty())
            // And nothing reacts any more.
            rows[1].dispatchEvent(MouseEvent(rows[1], MouseEvent.MOUSE_CLICKED, click.`when` + 2000, 0, 5, 5, 2, false, MouseEvent.BUTTON1))
            assertTrue(settings.prStatusFolded)
        } finally {
            fold.dispose()
        }
    }
}
