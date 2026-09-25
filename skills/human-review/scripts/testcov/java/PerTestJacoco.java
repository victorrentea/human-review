package hr.testcov;

import java.io.FileWriter;
import java.io.PrintWriter;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.engine.TestSource;
import org.junit.platform.engine.support.descriptor.ClasspathResourceSource;
import org.junit.platform.engine.support.descriptor.FilePosition;
import org.junit.platform.engine.support.descriptor.FileSource;
import org.junit.platform.engine.support.descriptor.MethodSource;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.TestPlan;

/**
 * Per-test JaCoCo for /human-review's `testcov` step: the agent's in-memory counters are
 * dumped AND reset around every test, so each test's .exec holds only what that test ran.
 *
 * Put on the test classpath by the step itself (Maven: -Dmaven.test.additionalClasspath),
 * found by the JUnit Platform through META-INF/services, and inert when there is no agent
 * in the JVM or no `hr.testcov.dir` property — a project needs no change of its own.
 *
 * What runs between two tests (a Spring context booting, a @BeforeAll) is reset and thrown
 * away rather than charged to either neighbour. Writes `index.jsonl`, one line per test:
 * n, engine, uid, name, status, and where the test lives (class + method, or a resource +
 * line for Cucumber scenarios).
 */
public class PerTestJacoco implements TestExecutionListener {
    private Object agent;
    private Method getData;
    private Path dir;
    private final AtomicInteger n = new AtomicInteger();
    private PrintWriter index;

    @Override
    public void testPlanExecutionStarted(TestPlan plan) {
        String out = System.getProperty("hr.testcov.dir");
        if (out == null || out.isEmpty()) return;
        try {
            dir = Paths.get(out);
            Files.createDirectories(dir);
            Class<?> rt = Class.forName("org.jacoco.agent.rt.RT");
            agent = rt.getMethod("getAgent").invoke(null);
            getData = agent.getClass().getMethod("getExecutionData", boolean.class);
            getData.setAccessible(true);
            index = new PrintWriter(new FileWriter(dir.resolve("index.jsonl").toFile(), true), true);
            reset();
        } catch (Throwable t) {
            System.err.println("[hr-testcov] per-test coverage off: " + t);
            agent = null;
        }
    }

    private synchronized byte[] take() throws Exception {
        return (byte[]) getData.invoke(agent, true);
    }

    private void reset() {
        if (agent == null) return;
        try { take(); } catch (Throwable t) { System.err.println("[hr-testcov] reset failed: " + t); }
    }

    @Override
    public void executionStarted(TestIdentifier id) {
        if (id.isTest()) reset();
    }

    @Override
    public void executionFinished(TestIdentifier id, TestExecutionResult r) {
        if (!id.isTest() || agent == null) return;
        try {
            byte[] data = take();
            int i = n.incrementAndGet();
            Files.write(dir.resolve(String.format("%05d.exec", i)), data);
            StringBuilder b = new StringBuilder("{");
            field(b, "n", String.valueOf(i));
            field(b, "uid", id.getUniqueId());
            field(b, "name", id.getDisplayName());
            field(b, "legacy", id.getLegacyReportingName());
            field(b, "status", r.getStatus().name());
            TestSource s = id.getSource().orElse(null);
            if (s instanceof MethodSource) {
                MethodSource m = (MethodSource) s;
                field(b, "class", m.getClassName());
                field(b, "method", m.getMethodName());
            } else if (s instanceof ClasspathResourceSource) {
                ClasspathResourceSource c = (ClasspathResourceSource) s;
                field(b, "resource", c.getClasspathResourceName());
                c.getPosition().map(FilePosition::getLine).ifPresent(l -> field(b, "line", String.valueOf(l)));
            } else if (s instanceof FileSource) {
                FileSource f = (FileSource) s;
                field(b, "file", f.getFile().getPath());
                f.getPosition().map(FilePosition::getLine).ifPresent(l -> field(b, "line", String.valueOf(l)));
            } else if (s != null) {
                field(b, "source", s.toString());
            }
            b.setLength(b.length() - 1);
            index.println(b.append('}'));
        } catch (Throwable t) {
            System.err.println("[hr-testcov] dump failed for " + id.getDisplayName() + ": " + t);
        }
    }

    private static void field(StringBuilder b, String k, String v) {
        b.append('"').append(k).append("\":\"");
        for (char c : v.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        b.append("\",");
    }

    @Override
    public void testPlanExecutionFinished(TestPlan plan) {
        if (index != null) index.close();
    }
}
