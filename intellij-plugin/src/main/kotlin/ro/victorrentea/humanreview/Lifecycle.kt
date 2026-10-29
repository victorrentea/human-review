package ro.victorrentea.humanreview

import com.intellij.ide.plugins.DynamicPluginListener
import com.intellij.ide.plugins.IdeaPluginDescriptor
import com.intellij.ide.plugins.cl.PluginAwareClassLoader
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.service
import com.intellij.openapi.extensions.PluginId

// Installing or updating the plugin needs no restart: every extension point it uses is dynamic
// (postStartupActivity, statusBarWidgetFactory, applicationConfigurable, notificationGroup,
// jbProtocolCommand, toolWindow, applicationService), it has no components, and this is the
// rest of a clean unload and load.
//
// **Unload.** The platform disposes the plugin's services and unregisters its extensions. What
// it cannot know about is state held in Kotlin objects — the range highlight and the edited mark
// sit in some editor's markup model, a notification's action points at a class about to go — so
// `beforePluginUnload` takes them off. The bridge is a service and would be disposed anyway; it
// is stopped here explicitly, so the port closes and `idea-<pid>.json` is gone before the
// classloader is, whatever order the platform disposes services in. Anything left referencing a
// class of ours keeps the classloader alive, and IntelliJ then answers "failed to unload" and
// wants a restart — the one thing this file exists to avoid.
//
// **Load.** The bridge starts from a `postStartupActivity`, which runs when a project opens. A
// plugin loaded into a running IDE sees no project open, so `pluginLoaded` starts it for the
// projects already there.

object HumanReviewPlugin {
    val ID: PluginId = PluginId.getId("ro.victorrentea.human-review")

    /** The installed build, from the classloader that loaded this class — PluginManagerCore's
     *  lookup by id is internal API from 2026.2. Null when not loaded as a plugin (unit tests). */
    fun version(): String? =
        (HumanReviewPlugin::class.java.classLoader as? PluginAwareClassLoader)?.pluginDescriptor?.version

    /** Take everything this plugin put into the IDE back out. EDT. Leaves the bridge
     *  restartable, so a test can call it and go on. */
    fun shutdown() {
        ApplicationManager.getApplication().getServiceIfCreated(HumanReviewBridge::class.java)?.stop()
        // The Pull Requests tool window's status blocks unfolded, and the AWT listener out.
        ApplicationManager.getApplication().getServiceIfCreated(PrStatusFold::class.java)?.dispose()
        RangeFocus.clear()
        EditedMark.clear()
        CommentNotice.forget()
    }
}

class HumanReviewPluginListener : DynamicPluginListener {
    override fun beforePluginUnload(pluginDescriptor: IdeaPluginDescriptor, isUpdate: Boolean) {
        if (pluginDescriptor.pluginId == HumanReviewPlugin.ID) HumanReviewPlugin.shutdown()
    }

    override fun pluginLoaded(pluginDescriptor: IdeaPluginDescriptor) {
        if (pluginDescriptor.pluginId != HumanReviewPlugin.ID) return
        val app = ApplicationManager.getApplication()
        if (app.isUnitTestMode || Folders.openProjects().isEmpty()) return
        service<HumanReviewBridge>().start()
        service<PrStatusFold>().install()
    }
}
