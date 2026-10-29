package ro.victorrentea.humanreview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.nio.file.Path

/** A platform object as far as the test goes: it records the classes on the stack that called it. */
class StackProbe {
    var callers: List<Class<*>> = emptyList()
    var got: Any? = null

    fun capture(arg: String): Int {
        got = arg
        callers = StackWalker.getInstance(setOf(StackWalker.Option.RETAIN_CLASS_REFERENCE, StackWalker.Option.SHOW_HIDDEN_FRAMES))
            .walk { s -> s.map { it.declaringClass }.toList() }
        return 42
    }
}

// The unload leak of 30 Oct 2026 (OffStack.kt): a Throwable created while the platform builds
// UI keeps the class of every frame on the stack. A call handed over through OffStack must
// reach the platform with no class of this plugin under it.
class OffStackTest {
    private val ours = OffStack::class.java.classLoader

    @Test fun `the call reaches its method with no class of the plugin below it`() {
        val probe = StackProbe()
        val call = OffStack.runnable(probe, StackProbe::class.java, "capture", String::class.java to "hi")
        Thread(call).apply { start(); join() }
        assertEquals("hi", probe.got)
        val below = probe.callers.drop(1)  // the probe itself is the "platform" method
        assertTrue("no frame below: ${probe.callers}", below.isNotEmpty())
        val mine = below.filter { it.name.startsWith("ro.victorrentea.") || it.classLoader === ours }
        assertTrue("plugin frames under the call: $mine", mine.isEmpty())
    }

    @Test fun `the Runnable itself is not a class of the plugin`() {
        val call = OffStack.runnable(StackProbe(), StackProbe::class.java, "capture", String::class.java to "x")
        assertNotSame(ours, call.javaClass.classLoader)
        assertFalse(call.javaClass.name, call.javaClass.name.startsWith("ro.victorrentea."))
    }

    @Test fun `a static method, its result dropped — the shutdown hook's delete`() {
        val f = Files.createTempFile("hr-offstack", ".json")
        val call = OffStack.runnable(null, Files::class.java, "deleteIfExists", Path::class.java to f)
        assertNotSame(ours, call.javaClass.classLoader)
        call.run()
        assertFalse(Files.exists(f))
    }

    @Test fun `a primitive parameter takes its boxed value`() {
        val box = javax.swing.JCheckBox()
        OffStack.runnable(box, javax.swing.AbstractButton::class.java, "setSelected", java.lang.Boolean.TYPE to true).run()
        assertTrue(box.isSelected)
    }
}
