package ro.victorrentea.humanreview

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import com.intellij.openapi.options.Configurable
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.UIUtil
import com.intellij.util.ui.FormBuilder
import javax.swing.JComponent

/** `serveScript` — absolute path to `serve-review.py`, for the status-bar item to start a server
 *  with. Empty: the copy installed with the `human-review` Claude Code plugin, else
 *  `~/workspace/human-review`. The twin of VS Code's `humanReview.serveScript`.
 *
 *  `pageInIde` — where a click on the status-bar item opens the page: the Human Review tool
 *  window (true, the default, as VS Code opens it in its Simple Browser beside the code) or the
 *  OS browser. An IDE without JCEF uses the browser whatever this says (`ReviewPage.target`).
 *
 *  `prFollowEditor` — the GitHub plugin's Pull Requests tool window selects, in an open PR's
 *  changes tree, the file the editor switches to (PullRequests.kt). On by default.
 *
 *  `prStatusFolded` — that tool window's status block (checks, reviewers) shows one line, and a
 *  double-click on it unfolds it (PullRequests.kt). Off by default; remembered. */
@Service(Service.Level.APP)
@State(name = "HumanReviewSettings", storages = [Storage("human-review.xml")])
class HumanReviewSettings : PersistentStateComponent<HumanReviewSettings.Data> {
    class Data {
        var serveScript: String = ""
        var pageInIde: Boolean = true
        var prFollowEditor: Boolean = true
        var prStatusFolded: Boolean = false
    }

    private var data = Data()

    override fun getState(): Data = data
    override fun loadState(state: Data) {
        data = state
    }

    var serveScript: String
        get() = data.serveScript
        set(v) { data.serveScript = v.trim() }

    var pageInIde: Boolean
        get() = data.pageInIde
        set(v) { data.pageInIde = v }

    var prFollowEditor: Boolean
        get() = data.prFollowEditor
        set(v) { data.prFollowEditor = v }

    var prStatusFolded: Boolean
        get() = data.prStatusFolded
        set(v) { data.prStatusFolded = v }

    companion object {
        fun get(): HumanReviewSettings = service()
    }
}

/** Settings → Tools → Human Review. */
class HumanReviewConfigurable : Configurable {
    private var field: JBTextField? = null
    private var inIde: JBCheckBox? = null
    private var follow: JBCheckBox? = null
    private var fold: JBCheckBox? = null

    override fun getDisplayName() = "Human Review"

    override fun createComponent(): JComponent {
        val f = JBTextField(HumanReviewSettings.get().serveScript).also { field = it }
        f.emptyText.text = "found automatically: the Claude Code plugin's copy, else ~/workspace/human-review"
        val jcef = PageView.get().supported()
        val box = JBCheckBox("Open the review page in the Human Review tool window, beside the code",
            HumanReviewSettings.get().pageInIde).also { inIde = it }
        return FormBuilder.createFormBuilder()
            .addLabeledComponent("serve-review.py:", f)
            .addComponent(box)
            .addComponentToRightColumn(JBLabel(if (jcef) "Off: the status-bar item opens it in the OS browser."
                else "This IDE has no embedded browser (JCEF): the page opens in the OS browser.").apply {
                componentStyle = UIUtil.ComponentStyle.SMALL
                foreground = UIUtil.getContextHelpForeground()
            })
            .addComponent(JBCheckBox("Pull Requests tool window: follow the editor — select the editor's file in the PR's changes tree",
                HumanReviewSettings.get().prFollowEditor).also { follow = it })
            .addComponent(JBCheckBox("Pull Requests tool window: fold the status block (checks, reviewers) to one line",
                HumanReviewSettings.get().prStatusFolded).also { fold = it })
            .addComponentToRightColumn(JBLabel("Both also in the tool window: right-click the changes tree. " +
                "A double-click on the status block folds or unfolds it.").apply {
                componentStyle = UIUtil.ComponentStyle.SMALL
                foreground = UIUtil.getContextHelpForeground()
            })
            .addComponentFillVertically(javax.swing.JPanel(), 0)
            .panel
    }

    override fun isModified() = field?.text?.trim() != HumanReviewSettings.get().serveScript ||
        inIde?.isSelected != HumanReviewSettings.get().pageInIde ||
        follow?.isSelected != HumanReviewSettings.get().prFollowEditor ||
        fold?.isSelected != HumanReviewSettings.get().prStatusFolded

    override fun apply() {
        HumanReviewSettings.get().serveScript = field?.text ?: ""
        inIde?.let { HumanReviewSettings.get().pageInIde = it.isSelected }
        follow?.let { HumanReviewSettings.get().prFollowEditor = it.isSelected }
        fold?.let { PrStatusFold.setFolded(it.isSelected) }
    }

    override fun reset() {
        field?.text = HumanReviewSettings.get().serveScript
        inIde?.isSelected = HumanReviewSettings.get().pageInIde
        follow?.isSelected = HumanReviewSettings.get().prFollowEditor
        fold?.isSelected = HumanReviewSettings.get().prStatusFolded
    }

    override fun disposeUIResources() {
        field = null
        inIde = null
        follow = null
        fold = null
    }
}
