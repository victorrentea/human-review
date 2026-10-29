package ro.victorrentea.humanreview

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import java.lang.invoke.MethodHandleProxies
import java.lang.invoke.MethodHandles
import java.lang.reflect.Modifier
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

// Why an editor, a diff window, a tool window's content or a GitHub pull-request tab must never be
// *created* on a stack that runs through this plugin.
//
// Every ActionToolbarImpl keeps `Throwable("toolbar creation trace")` for its whole life, and a
// Throwable keeps the Class of every frame it was created through (HotSpot's backtrace holds the
// mirrors, to keep the methods alive). An editor opened from our own lambda has an inspections
// toolbar whose trace holds that lambda's class, so the editor — a platform object that rightly
// outlives the plugin — holds the plugin's classloader for as long as it is open, or, on macOS,
// for as long as the input method remembers it as the last focused component. The next hot
// reload then waits for a classloader that never goes, and IntelliJ answers "not unload-safe", a
// memory snapshot and a Restart button. 30 Oct 2026, 0.3.3 reloaded over itself, idea.log:
//
//   ROOT: Global JNI → sun.lwawt.macosx.CInputMethod → fAwtFocussedComponent: EditorComponentImpl
//   → EditorImpl → EditorMarkupModelImpl → statusToolbar: EditorInspectionsActionToolbar
//   → myCreationTrace: Throwable → backtrace → Class → PluginClassLoader (0.3.3, the old one)
//
// (The same report's second root — a shutdown-hook Thread whose task is a HumanReviewBridge
// lambda — was the *new* 0.3.3's loader: the analyzer prints a path to every PluginClassLoader of
// the plugin id, and the one just loaded is alive by design. The hook is JDK-only now anyway.)
//
// So the call that creates such UI is handed to the EDT as a Runnable that belongs to the JDK: a
// method handle on the platform method, bound to its arguments, wrapped by `MethodHandleProxies`.
// Its proxy class and the lambda forms it runs through are defined by the JDK, never by this
// plugin's loader, so when the platform builds the editor there is no frame of ours on the stack.
// (JDK ≤ 21 defines the proxy in the thread's context class loader when the interface is a
// bootstrap one, hence the swap below; JDK 22+ defines a hidden class of its own.)
object OffStack {
    /**
     * `receiver.name(args)` — a public method of `owner`, static when `receiver` is null — as a
     * Runnable with no class of this plugin in it. Each argument is its parameter type and its
     * value. A return value is dropped; an exception escapes from `run()`.
     */
    fun runnable(receiver: Any?, owner: Class<*>, name: String, vararg args: Pair<Class<*>, Any?>): Runnable {
        val method = owner.getMethod(name, *args.map { it.first }.toTypedArray())
        var mh = MethodHandles.publicLookup().unreflect(method)
        if (!Modifier.isStatic(method.modifiers)) mh = mh.bindTo(requireNotNull(receiver) { "$name needs a receiver" })
        if (args.isNotEmpty()) mh = MethodHandles.insertArguments(mh, 0, *args.map { it.second }.toTypedArray())
        if (mh.type().returnType() != Void.TYPE) mh = MethodHandles.dropReturn(mh)
        val thread = Thread.currentThread()
        val saved = thread.contextClassLoader
        thread.contextClassLoader = ClassLoader.getPlatformClassLoader()
        try {
            return MethodHandleProxies.asInterfaceInstance(Runnable::class.java, mh)
        } finally {
            thread.contextClassLoader = saved
        }
    }

    /** Dispatch `call` on the EDT, later, and return — the platform runs it on its own. */
    fun later(call: Runnable, modality: ModalityState = ModalityState.defaultModalityState()) =
        ApplicationManager.getApplication().invokeLater(call, modality)

    /**
     * Run `call` on the EDT and wait for it, like `Ide.onEdt`, but with `call` dispatched by the
     * platform on its own: a second event, queued behind it in the same modality, says it is
     * done. Not from the EDT. A `call` that throws is reported by the EDT, and this returns.
     * A busy EDT (a modal dialog) is `IdeBusy` after 10 s.
     */
    fun runOnEdt(call: Runnable, modality: ModalityState = ModalityState.defaultModalityState()) {
        val app = ApplicationManager.getApplication()
        check(!app.isDispatchThread) { "OffStack.runOnEdt waits for the EDT: call it from another thread" }
        val done = CompletableFuture<Unit>()
        app.invokeLater(call, modality)
        app.invokeLater({ done.complete(Unit) }, modality)
        try {
            done.get(10, TimeUnit.SECONDS)
        } catch (_: TimeoutException) {
            throw IdeBusy()
        } catch (e: ExecutionException) {
            throw e.cause ?: e
        }
    }
}
