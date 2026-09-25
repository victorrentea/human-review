package hr.testcov;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;
import org.jacoco.core.analysis.Analyzer;
import org.jacoco.core.analysis.CoverageBuilder;
import org.jacoco.core.analysis.IClassCoverage;
import org.jacoco.core.analysis.ICounter;
import org.jacoco.core.analysis.IMethodCoverage;
import org.jacoco.core.data.ExecutionDataStore;
import org.jacoco.core.tools.ExecFileLoader;

/**
 * JaCoCo execution data -> covered source lines, one .exec per test.
 *
 *   TestcovAnalyze --classes <dir|jar>... --include <path/prefix>... <exec>...
 *
 * `--classes` is a class directory or a jar; a Spring Boot jar is read from its
 * BOOT-INF/classes, never its BOOT-INF/lib. The classes must be the very bytes the agent
 * measured — JaCoCo matches execution data to a class by a checksum of the class file.
 *
 * Prints one JSON object: `executable` (every line holding bytecode, per source, as
 * "package/File.java"), `init` (lines only a constructor or static initialiser holds) and
 * `execs` (per exec file name, the covered lines). Constructors are left out of `execs`
 * and of `executable`: whatever a Spring context builds is built once, by whichever test
 * happens to boot it first, and charging it to that test says nothing about that test.
 * Lines are written as ranges, "3-7,12".
 */
public class TestcovAnalyze {
    public static void main(String[] args) throws Exception {
        List<Path> roots = new ArrayList<>();
        List<String> include = new ArrayList<>();
        List<Path> execs = new ArrayList<>();
        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("--classes")) roots.add(Paths.get(args[++i]));
            else if (args[i].equals("--include")) include.add(args[++i]);
            else execs.add(Paths.get(args[i]));
        }
        List<String[]> names = new ArrayList<>();          // [location, internal path]
        List<byte[]> bytes = new ArrayList<>();
        for (Path r : roots) load(r, include, names, bytes);

        Map<String, TreeSet<Integer>> exe = new TreeMap<>(), init = new TreeMap<>();
        CoverageBuilder empty = analyze(new ExecutionDataStore(), names, bytes);
        for (IClassCoverage cc : empty.getClasses()) {
            String src = source(cc);
            if (src == null) continue;
            for (IMethodCoverage m : cc.getMethods()) {
                boolean ctor = isInit(m);
                for (int ln = m.getFirstLine(); ln > 0 && ln <= m.getLastLine(); ln++) {
                    if (m.getLine(ln).getStatus() == ICounter.EMPTY) continue;
                    (ctor ? init : exe).computeIfAbsent(src, k -> new TreeSet<>()).add(ln);
                }
            }
        }
        for (Map.Entry<String, TreeSet<Integer>> e : init.entrySet()) {
            TreeSet<Integer> real = exe.get(e.getKey());
            if (real != null) e.getValue().removeAll(real);
        }

        try (PrintWriter out = new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8))) {
            out.print("{\"classes\":" + bytes.size() + ",\"executable\":");
            writeMap(out, exe);
            out.print(",\"init\":");
            writeMap(out, init);
            out.print(",\"execs\":{");
            boolean first = true;
            for (Path exec : execs) {
                ExecFileLoader l = new ExecFileLoader();
                l.load(exec.toFile());
                CoverageBuilder cb = analyze(l.getExecutionDataStore(), names, bytes);
                Map<String, TreeSet<Integer>> hit = new TreeMap<>();
                for (IClassCoverage cc : cb.getClasses()) {
                    String src = source(cc);
                    if (src == null) continue;
                    for (IMethodCoverage m : cc.getMethods()) {
                        if (isInit(m) || m.getInstructionCounter().getCoveredCount() == 0) continue;
                        for (int ln = m.getFirstLine(); ln > 0 && ln <= m.getLastLine(); ln++) {
                            int st = m.getLine(ln).getStatus();
                            if (st == ICounter.FULLY_COVERED || st == ICounter.PARTLY_COVERED)
                                hit.computeIfAbsent(src, k -> new TreeSet<>()).add(ln);
                        }
                    }
                }
                if (!first) out.print(',');
                first = false;
                out.print(quote(exec.getFileName().toString()) + ":");
                writeMap(out, hit);
            }
            out.println("}}");
        }
    }

    static boolean isInit(IMethodCoverage m) {
        return m.getName().equals("<init>") || m.getName().equals("<clinit>");
    }

    static String source(IClassCoverage cc) {
        return cc.getSourceFileName() == null ? null : cc.getPackageName() + "/" + cc.getSourceFileName();
    }

    static CoverageBuilder analyze(ExecutionDataStore store, List<String[]> names, List<byte[]> bytes)
            throws IOException {
        CoverageBuilder cb = new CoverageBuilder();
        Analyzer an = new Analyzer(store, cb);
        for (int i = 0; i < bytes.size(); i++) {
            try {
                an.analyzeClass(bytes.get(i), names.get(i)[0]);
            } catch (IOException e) {
                System.err.println("[testcov] skipped " + names.get(i)[0] + ": " + e.getMessage());
            }
        }
        return cb;
    }

    static boolean wanted(String internal, List<String> include) {
        if (!internal.endsWith(".class")) return false;
        if (include.isEmpty()) return true;
        for (String p : include) if (internal.startsWith(p)) return true;
        return false;
    }

    static void load(Path root, List<String> include, List<String[]> names, List<byte[]> bytes)
            throws IOException {
        if (Files.isDirectory(root)) {
            try (Stream<Path> s = Files.walk(root)) {
                for (Path p : (Iterable<Path>) s::iterator) {
                    String rel = root.relativize(p).toString().replace('\\', '/');
                    if (Files.isRegularFile(p) && wanted(rel, include)) {
                        names.add(new String[] {p.toString(), rel});
                        bytes.add(Files.readAllBytes(p));
                    }
                }
            }
            return;
        }
        try (ZipInputStream z = new ZipInputStream(Files.newInputStream(root))) {
            readZip(z, root.toString(), include, names, bytes);
        }
    }

    static void readZip(ZipInputStream z, String where, List<String> include, List<String[]> names,
                        List<byte[]> bytes) throws IOException {
        for (ZipEntry e; (e = z.getNextEntry()) != null; ) {
            String n = e.getName();
            if (n.startsWith("BOOT-INF/lib/") || n.startsWith("WEB-INF/lib/")) continue;
            String rel = n.startsWith("BOOT-INF/classes/") ? n.substring("BOOT-INF/classes/".length())
                    : n.startsWith("WEB-INF/classes/") ? n.substring("WEB-INF/classes/".length()) : n;
            if (!wanted(rel, include)) continue;
            names.add(new String[] {where + "!" + n, rel});
            bytes.add(readAll(z));
        }
    }

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        for (int r; (r = in.read(buf)) > 0; ) b.write(buf, 0, r);
        return b.toByteArray();
    }

    static void writeMap(PrintWriter out, Map<String, TreeSet<Integer>> m) {
        out.print('{');
        boolean first = true;
        for (Map.Entry<String, TreeSet<Integer>> e : m.entrySet()) {
            if (!first) out.print(',');
            first = false;
            out.print(quote(e.getKey()) + ":" + quote(ranges(e.getValue())));
        }
        out.print('}');
    }

    static String ranges(TreeSet<Integer> s) {
        StringBuilder b = new StringBuilder();
        Integer start = null, prev = null;
        for (int v : s) {
            if (prev != null && v == prev + 1) { prev = v; continue; }
            if (start != null) b.append(b.length() > 0 ? "," : "").append(span(start, prev));
            start = v;
            prev = v;
        }
        if (start != null) b.append(b.length() > 0 ? "," : "").append(span(start, prev));
        return b.toString();
    }

    static String span(int a, int b) { return a == b ? String.valueOf(a) : a + "-" + b; }

    static String quote(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        return b.append('"').toString();
    }
}
