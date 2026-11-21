package hr.complexity;

import com.github.javaparser.JavaParser;
import com.github.javaparser.JavaToken;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.*;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.nodeTypes.NodeWithTypeParameters;
import com.github.javaparser.ast.stmt.*;
import com.github.javaparser.ast.type.*;
import com.github.javaparser.resolution.declarations.ResolvedConstructorDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.resolution.types.ResolvedType;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Cognitive complexity of the whole flow behind every entry point, read from a syntax tree
 * with calls resolved by type (JavaParser + JavaSymbolSolver). The second engine behind
 * {@code endpoint-complexity.py}; see {@code reference/complexity-engine-eval.md}.
 *
 * <p>Source only: the type solver is every {@code src/main/java} root under {@code --root}
 * plus the JDK by reflection. Nothing of the reviewed project is built, and no library jar
 * is on the solver — a call into Spring, Jackson or any other library is dropped exactly as
 * the regex engine drops it, and a call whose target the solver cannot place but whose name
 * the project declares is reported as <em>not followed</em> instead of vanishing.
 *
 * <p>Usage: {@code java -cp … hr.complexity.ComplexityEngine --root DIR [--out FILE]}.
 * Emits the same JSON list {@code endpoint-complexity.py} writes, plus per entry point
 * {@code engine}, {@code unresolvedCount}, {@code unresolved}, and per flow method the
 * {@code file}/{@code line} of its declaration.
 */
public final class ComplexityEngine {

    static final Map<String, String> MAPPINGS = Map.of(
            "GetMapping", "GET", "PostMapping", "POST", "PutMapping", "PUT",
            "DeleteMapping", "DELETE", "PatchMapping", "PATCH", "RequestMapping", "ANY");
    static final Map<String, String> LISTENERS = Map.of(
            "KafkaListener", "KAFKA", "RabbitListener", "RABBIT", "JmsListener", "JMS");
    /** Names every object answers to: an unplaceable `x.equals(y)` is not a lost project call. */
    static final Set<String> OBJECT_METHODS = Set.of("equals", "hashCode", "toString", "getClass",
            "notify", "notifyAll", "wait", "clone", "finalize", "compareTo");
    static final int MAX_LINE = 160;
    static final String DEBUG = System.getenv("HR_CX_DEBUG");

    // ── the model ────────────────────────────────────────────────────────────────────────

    static final class TypeInfo {
        String qname, simple, file, pkg;
        String base;                  // qname before a module tag: two modules' copies share it
        Node node;                    // TypeDeclaration, or the EnumConstantDeclaration of a constant body
        CompilationUnit cu;
        boolean isInterface, isAbstract, isFinal, externalSuper;
        final Set<String> supers = new LinkedHashSet<>();     // direct project supertypes
        final Set<String> allSupers = new LinkedHashSet<>();  // transitive
        final Set<String> typeParams = new HashSet<>();
        final List<Method> methods = new ArrayList<>();
        final Set<String> externalArgs = new LinkedHashSet<>();  // project types in a library supertype's <…>
        final Set<String> superArgs = new LinkedHashSet<>();     // project types in any supertype's <…>
        TypeInfo outer;
    }

    static final class Method {
        String key, name, display, file;
        int line, endLine;            // the declaration's whole span, annotations to `}`
        TypeInfo owner;
        Node decl;
        Node body;                    // null for an abstract / interface method
        boolean isStatic, isPrivate, isCtor, isFinal;
        List<String> params = new ArrayList<>();
        Set<String> typeParams = new HashSet<>();
        int cc, cyc;
        final List<Map<String, Object>> hits = new ArrayList<>();
        final List<Node> sites = new ArrayList<>();
        final List<String> callees = new ArrayList<>();
        final List<Map<String, Object>> unresolved = new ArrayList<>();
        final Set<String> overriders = new LinkedHashSet<>();
    }

    final Path root;
    final Map<String, CompilationUnit> cus = new LinkedHashMap<>();   // rel path -> cu
    final Map<String, List<String>> lines = new HashMap<>();
    final Map<String, TypeInfo> types = new LinkedHashMap<>();        // qname -> type
    final Map<String, List<String>> typesBySimple = new HashMap<>();
    final Map<String, List<String>> typesByBase = new HashMap<>();
    final Map<String, Method> methods = new LinkedHashMap<>();        // key -> method
    final Map<String, Method> byPos = new HashMap<>();                // file:line:col -> method
    final Map<String, List<Method>> byName = new HashMap<>();
    final List<String> parseErrors = new ArrayList<>();
    JavaParser parser;

    ComplexityEngine(Path root) { this.root = root.toAbsolutePath().normalize(); }

    // ── parsing ──────────────────────────────────────────────────────────────────────────

    void parse() throws IOException {
        List<Path> roots;
        try (Stream<Path> s = Files.walk(root)) {
            roots = s.filter(Files::isDirectory)
                    .filter(p -> p.endsWith(Paths.get("src", "main", "java")))
                    .filter(p -> !hidden(root.relativize(p)))
                    .sorted().collect(Collectors.toList());
        }
        CombinedTypeSolver solver = new CombinedTypeSolver(new ReflectionTypeSolver(true));
        ParserConfiguration plain = new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_25);
        for (Path r : roots) solver.add(new JavaParserTypeSolver(r, plain));
        ParserConfiguration cfg = new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_25)
                .setSymbolResolver(new JavaSymbolSolver(solver));
        parser = new JavaParser(cfg);
        for (Path r : roots) {
            List<Path> files;
            try (Stream<Path> s = Files.walk(r)) {
                files = s.filter(p -> p.toString().endsWith(".java")).sorted().collect(Collectors.toList());
            }
            for (Path f : files) {
                String rel = root.relativize(f).toString().replace('\\', '/');
                if (hidden(root.relativize(f))) continue;
                String text = Files.readString(f, StandardCharsets.UTF_8);
                ParseResult<CompilationUnit> res = parser.parse(text);
                if (!res.isSuccessful() || res.getResult().isEmpty()) {
                    parseErrors.add(rel + ": " + res.getProblems().stream().findFirst()
                            .map(p -> p.getVerboseMessage().split("\n")[0]).orElse("unparseable"));
                    continue;
                }
                CompilationUnit cu = res.getResult().get();
                cu.setStorage(f);
                cus.put(rel, cu);
                lines.put(rel, Arrays.asList(text.split("\r\n|\r|\n", -1)));
            }
        }
    }

    static boolean hidden(Path rel) {
        for (Path part : rel) if (part.toString().startsWith(".")) return true;
        return false;
    }

    // ── indexing types and methods ───────────────────────────────────────────────────────

    void index() {
        for (Map.Entry<String, CompilationUnit> e : cus.entrySet()) {
            String rel = e.getKey();
            CompilationUnit cu = e.getValue();
            String pkg = cu.getPackageDeclaration().map(p -> p.getNameAsString()).orElse("");
            for (TypeDeclaration<?> td : cu.getTypes()) indexType(td, null, rel, cu, pkg);
        }
        for (TypeInfo t : types.values()) linkSupers(t);
        for (TypeInfo t : types.values()) closeSupers(t, t, new HashSet<>());
    }

    void indexType(TypeDeclaration<?> td, TypeInfo outer, String rel, CompilationUnit cu, String pkg) {
        TypeInfo t = new TypeInfo();
        t.simple = td.getNameAsString();
        t.qname = td.getFullyQualifiedName().orElse((pkg.isEmpty() ? "" : pkg + ".") + t.simple);
        if (outer != null && !t.qname.startsWith(outer.qname + ".")) t.qname = outer.qname + "." + t.simple;
        t.base = t.qname;
        if (types.containsKey(t.qname)) {
            // The same class in two modules (`before/` and `after/`, a copied util): both are
            // real, both may hold entry points. The second is named by its module.
            int at = rel.indexOf("/src/main/java/");
            String module = at > 0 ? rel.substring(0, at) : "root";
            String q = t.qname + "[" + module + "]";
            int n = 2;
            while (types.containsKey(q)) q = t.qname + "[" + module + "#" + n++ + "]";
            t.qname = q;
        }
        t.file = rel; t.cu = cu; t.pkg = pkg; t.node = td; t.outer = outer;
        if (td instanceof ClassOrInterfaceDeclaration c) {
            t.isInterface = c.isInterface();
            t.isAbstract = c.isAbstract() || c.isInterface();
            t.isFinal = c.isFinal();
        } else if (td instanceof EnumDeclaration || td instanceof RecordDeclaration) {
            t.isFinal = !(td instanceof EnumDeclaration en && en.getEntries().stream().anyMatch(c -> !c.getClassBody().isEmpty()));
            t.externalSuper = true;   // java.lang.Enum / java.lang.Record: values(), valueOf(), accessors
        } else if (td instanceof AnnotationDeclaration) {
            t.externalSuper = true;
        }
        if (td instanceof NodeWithTypeParameters<?> tp)
            tp.getTypeParameters().forEach(p -> t.typeParams.add(p.getNameAsString()));
        types.put(t.qname, t);
        typesBySimple.computeIfAbsent(t.simple, k -> new ArrayList<>()).add(t.qname);
        typesByBase.computeIfAbsent(t.base, k -> new ArrayList<>()).add(t.qname);
        for (BodyDeclaration<?> member : td.getMembers()) {
            if (member instanceof TypeDeclaration<?> nested) indexType(nested, t, rel, cu, pkg);
            else if (member instanceof CallableDeclaration<?> || member instanceof CompactConstructorDeclaration)
                addMethod(t, member);
        }
        if (td instanceof EnumDeclaration en) {
            for (EnumConstantDeclaration c : en.getEntries()) {
                if (c.getClassBody().isEmpty()) continue;
                TypeInfo ct = new TypeInfo();
                ct.simple = c.getNameAsString();
                ct.qname = t.qname + "." + ct.simple;
                ct.base = ct.qname;
                ct.file = rel; ct.cu = cu; ct.pkg = pkg; ct.node = c; ct.outer = t; ct.isFinal = true;
                ct.supers.add(t.qname);
                types.put(ct.qname, ct);
                for (BodyDeclaration<?> member : c.getClassBody()) {
                    if (member instanceof TypeDeclaration<?> nested) indexType(nested, ct, rel, cu, pkg);
                    else if (member instanceof CallableDeclaration<?>) addMethod(ct, member);
                }
            }
        }
    }

    void addMethod(TypeInfo t, BodyDeclaration<?> member) {
        Method m = new Method();
        m.owner = t; m.decl = member; m.file = t.file;
        m.line = member.getBegin().map(p -> p.line).orElse(1);
        m.endLine = member.getEnd().map(p -> p.line).orElse(m.line);
        if (member instanceof MethodDeclaration md) {
            m.name = md.getNameAsString();
            m.body = md.getBody().orElse(null);
            m.isStatic = md.isStatic();
            m.isPrivate = md.isPrivate();
            m.isFinal = md.isFinal();
            md.getTypeParameters().forEach(p -> m.typeParams.add(p.getNameAsString()));
            for (Parameter p : md.getParameters()) m.params.add(simpleType(p.getType()) + (p.isVarArgs() ? "..." : ""));
            // An interface method with neither body nor `default` is abstract; so is an `abstract` one.
        } else if (member instanceof ConstructorDeclaration cd) {
            m.name = t.simple;
            m.body = cd.getBody();
            m.isCtor = true;
            m.isPrivate = cd.isPrivate();
            cd.getTypeParameters().forEach(p -> m.typeParams.add(p.getNameAsString()));
            for (Parameter p : cd.getParameters()) m.params.add(simpleType(p.getType()) + (p.isVarArgs() ? "..." : ""));
        } else if (member instanceof CompactConstructorDeclaration cc) {
            m.name = t.simple;
            m.body = cc.getBody();
            m.isCtor = true;
            if (t.node instanceof RecordDeclaration rd)
                for (Parameter p : rd.getParameters()) m.params.add(simpleType(p.getType()));
        } else {
            return;
        }
        t.methods.add(m);
        byPos.put(pos(member), m);
        byName.computeIfAbsent(m.name, k -> new ArrayList<>()).add(m);
    }

    /** Keys are assigned once every type is indexed, so an overload is named by its params. */
    void assignKeys() {
        for (TypeInfo t : types.values()) {
            Map<String, Long> counts = t.methods.stream().collect(Collectors.groupingBy(x -> x.name, Collectors.counting()));
            for (Method m : t.methods) {
                String base = t.qname + "#" + m.name;
                m.key = counts.get(m.name) > 1 ? base + "(" + String.join(",", m.params) + ")" : base;
                // Two declarations that still collide (impossible in valid Java) keep the first.
                String k = m.key;
                int n = 2;
                while (methods.containsKey(k)) k = m.key + "#" + n++;
                m.key = k;
                m.display = t.simple + "." + m.name + "(" + String.join(", ", m.params) + ")";
                methods.put(m.key, m);
            }
        }
        // Overrides, read per concrete hierarchy: within every type T, a body declared anywhere
        // in T's hierarchy implements a same-signature method of any *other* supertype of T
        // that it does not itself sit above. That is the ordinary override (Child.save over
        // Base.save) and also the inherited implementation (`class Impl extends BaseImpl
        // implements Svc {}` — BaseImpl.doIt is what a call on Svc.doIt runs).
        for (TypeInfo t : types.values()) {
            List<Method> pool = new ArrayList<>(t.methods);
            for (String sup : t.allSupers) if (types.containsKey(sup)) pool.addAll(types.get(sup).methods);
            for (Method b : pool) {
                if (b.isStatic || b.isCtor || b.isPrivate) continue;
                for (Method m : pool) {
                    if (m == b || m.body == null || m.owner == b.owner || m.isStatic || m.isCtor || m.isPrivate) continue;
                    if (b.owner.allSupers.contains(m.owner.qname)) continue;   // m sits above b: b overrides m, not the reverse
                    if (sameSignature(b, m)) b.overriders.add(m.key);
                }
            }
        }
    }

    void linkSupers(TypeInfo t) {
        List<ClassOrInterfaceType> sup = new ArrayList<>();
        if (t.node instanceof ClassOrInterfaceDeclaration c) {
            sup.addAll(c.getExtendedTypes());
            sup.addAll(c.getImplementedTypes());
        } else if (t.node instanceof EnumDeclaration en) {
            sup.addAll(en.getImplementedTypes());
        } else if (t.node instanceof RecordDeclaration rd) {
            sup.addAll(rd.getImplementedTypes());
        }
        for (ClassOrInterfaceType st : sup) {
            t.superArgs.addAll(projectTypesIn(st, t));
            String q = projectTypeOf(st, t);
            if (q != null) t.supers.add(q);
            else {
                t.externalSuper = true;
                // A library supertype hands back its first type argument (`JpaRepository<Owner,
                // OwnerId>`, `CrudRepository<Owner, Long>`): the id type is never what comes back.
                st.getTypeArguments().filter(a -> !a.isEmpty()).ifPresent(a -> t.externalArgs.addAll(typesOf(a.get(0), t)));
            }
        }
    }

    void closeSupers(TypeInfo t, TypeInfo cur, Set<String> seen) {
        for (String s : cur.supers) {
            if (!seen.add(s)) continue;
            t.allSupers.add(s);
            TypeInfo st = types.get(s);
            if (st != null) {
                if (st.externalSuper) t.externalSuper = true;
                t.externalArgs.addAll(st.externalArgs);
                t.superArgs.addAll(st.superArgs);
                closeSupers(t, st, seen);
            }
        }
    }

    /** The project type a written type names, or null when it is not one of ours. */
    String projectTypeOf(ClassOrInterfaceType st, TypeInfo from) {
        try {
            ResolvedType r = st.resolve();
            if (r.isReferenceType()) {
                String q = r.asReferenceType().getQualifiedName();
                return types.containsKey(q) ? q : null;
            }
        } catch (Throwable ignored) {
            // fall through to the name
        }
        return lookupSimple(st.getNameAsString(), from == null ? null : from.cu, from);
    }

    /** A simple type name as the file that writes it would see it: nested, imported, same package, unique. */
    String lookupSimple(String simple, CompilationUnit cu, TypeInfo from) {
        for (TypeInfo o = from; o != null; o = o.outer) {
            String q = o.qname + "." + simple;
            if (types.containsKey(q)) return q;
            if (o.simple.equals(simple)) return o.qname;
        }
        List<String> cands = typesBySimple.getOrDefault(simple, List.of());
        if (cands.isEmpty()) return null;
        if (cu != null) {
            for (ImportDeclaration imp : cu.getImports()) {
                String n = imp.getNameAsString();
                if (!imp.isAsterisk() && !imp.isStatic() && n.endsWith("." + simple))
                    return types.containsKey(n) ? n : null;   // an explicit import of a library type shadows ours
            }
            String pkg = cu.getPackageDeclaration().map(p -> p.getNameAsString()).orElse("");
            String same = (pkg.isEmpty() ? "" : pkg + ".") + simple;
            if (types.containsKey(same)) return same;
            for (ImportDeclaration imp : cu.getImports()) {
                if (imp.isAsterisk() && !imp.isStatic() && types.containsKey(imp.getNameAsString() + "." + simple))
                    return imp.getNameAsString() + "." + simple;
            }
        }
        return cands.size() == 1 ? cands.get(0) : null;
    }

    boolean sameSignature(Method base, Method m) {
        if (!base.name.equals(m.name) || base.params.size() != m.params.size()) return false;
        for (int i = 0; i < base.params.size(); i++) {
            String a = base.params.get(i), b = m.params.get(i);
            if (a.equals(b)) continue;
            if (isTypeVar(a, base) || isTypeVar(b, m)) continue;
            return false;
        }
        return true;
    }

    static boolean isTypeVar(String t, Method m) {
        if (m == null) return false;
        String bare = t.replace("[]", "").replace("...", "");
        return m.typeParams.contains(bare) || (m.owner != null && m.owner.typeParams.contains(bare));
    }

    static String simpleType(Type t) {
        if (t instanceof ClassOrInterfaceType c) return c.getNameAsString();
        if (t instanceof ArrayType a) return simpleType(a.getComponentType()) + "[]";
        return t.asString();
    }

    static String pos(Node n) {
        String file = n.findCompilationUnit().flatMap(CompilationUnit::getStorage)
                .map(s -> s.getPath().toAbsolutePath().normalize().toString()).orElse("?");
        return file + ":" + n.getBegin().map(p -> p.line + ":" + p.column).orElse("?");
    }

    // ── scoring one method, from the tree ────────────────────────────────────────────────

    final class Scorer {
        final Method m;
        final Set<BinaryExpr> consumed = Collections.newSetFromMap(new IdentityHashMap<>());

        Scorer(Method m) { this.m = m; }

        void hit(int line, int inc, String why) {
            m.cc += inc;
            m.hits.add(hitAt(m.file, line, inc, why));
        }

        void walk(Node n, int nest) {
            if (n instanceof IfStmt s) { ifChain(s, nest, false); return; }
            if (n instanceof ForStmt || n instanceof ForEachStmt || n instanceof WhileStmt || n instanceof DoStmt) {
                String kw = n instanceof DoStmt ? "do" : n instanceof WhileStmt ? "while" : "for";
                hit(line(n), 1 + nest, kw);
                m.cyc++;
                for (Node c : n.getChildNodes()) walk(c, nest + 1);
                return;
            }
            if (n instanceof SwitchStmt || n instanceof SwitchExpr) {
                hit(line(n), 1 + nest, "switch");
                NodeList<SwitchEntry> entries = n instanceof SwitchStmt ss ? ss.getEntries() : ((SwitchExpr) n).getEntries();
                for (SwitchEntry e : entries) if (!e.getLabels().isEmpty()) m.cyc++;
                for (Node c : n.getChildNodes()) walk(c, nest + 1);
                return;
            }
            if (n instanceof TryStmt t) {
                for (Expression r : t.getResources()) walk(r, nest);
                walk(t.getTryBlock(), nest);
                for (CatchClause c : t.getCatchClauses()) {
                    hit(line(c), 1 + nest, "catch");
                    m.cyc++;
                    walk(c.getBody(), nest + 1);
                }
                t.getFinallyBlock().ifPresent(f -> walk(f, nest));
                return;
            }
            if (n instanceof ConditionalExpr c) {
                hit(tokenLine(c.getCondition(), "?", line(c)), 1 + nest, "?:");
                m.cyc++;
                for (Node ch : n.getChildNodes()) walk(ch, nest + 1);
                return;
            }
            if (n instanceof LambdaExpr l) {
                walk(l.getBody(), nest + 1);
                return;
            }
            if (n instanceof LocalClassDeclarationStmt || n instanceof LocalRecordDeclarationStmt) {
                for (Node c : n.getChildNodes()) walk(c, nest + 1);
                return;
            }
            if (n instanceof BreakStmt b && b.getLabel().isPresent()) { hit(line(n), 1, "break label"); return; }
            if (n instanceof ContinueStmt c && c.getLabel().isPresent()) { hit(line(n), 1, "continue label"); return; }
            if (n instanceof BinaryExpr b && isLogical(b) && !consumed.contains(b)) {
                List<BinaryExpr> seq = new ArrayList<>();
                flatten(b, seq);
                consumed.addAll(seq);
                BinaryExpr prev = null;
                for (BinaryExpr cur : seq) {
                    m.cyc++;
                    if (prev == null || prev.getOperator() != cur.getOperator()) {
                        String op = cur.getOperator() == BinaryExpr.Operator.AND ? "&&" : "||";
                        hit(tokenLine(cur.getLeft(), op, line(cur)), 1, op);
                    }
                    prev = cur;
                }
            }
            if (n instanceof MethodCallExpr || n instanceof MethodReferenceExpr
                    || n instanceof ObjectCreationExpr || n instanceof ExplicitConstructorInvocationStmt) {
                m.sites.add(n);
            }
            if (n instanceof ObjectCreationExpr oc && oc.getAnonymousClassBody().isPresent()) {
                oc.getScope().ifPresent(s -> walk(s, nest));
                for (Expression a : oc.getArguments()) walk(a, nest);
                for (BodyDeclaration<?> b : oc.getAnonymousClassBody().get()) walk(b, nest + 1);
                return;
            }
            for (Node c : n.getChildNodes()) walk(c, nest);
        }

        void ifChain(IfStmt s, int nest, boolean elseIf) {
            if (!elseIf) hit(line(s), 1 + nest, "if");
            m.cyc++;
            walk(s.getCondition(), nest);
            walk(s.getThenStmt(), nest + 1);
            if (s.getElseStmt().isPresent()) {
                Statement e = s.getElseStmt().get();
                // The `else` of an `else if` is the pair's whole cost: +1, flat.
                hit(tokenLine(s.getThenStmt(), "else", line(e)), 1, "else");
                if (e instanceof IfStmt ei) ifChain(ei, nest, true);
                else walk(e, nest + 1);
            }
        }

        boolean isLogical(BinaryExpr b) {
            return b.getOperator() == BinaryExpr.Operator.AND || b.getOperator() == BinaryExpr.Operator.OR;
        }

        /** In-order run of `&&`/`||` through parentheses — Sonar's flattening. `!` and calls stop it. */
        void flatten(Expression e, List<BinaryExpr> out) {
            while (e instanceof EnclosedExpr en) e = en.getInner();
            if (e instanceof BinaryExpr b && isLogical(b)) {
                flatten(b.getLeft(), out);
                out.add(b);
                flatten(b.getRight(), out);
            }
        }
    }

    static int line(Node n) { return n.getBegin().map(p -> p.line).orElse(1); }

    /** The line of the first `text` token after `after` ends — an operator or a keyword. */
    static int tokenLine(Node after, String text, int fallback) {
        Optional<JavaToken> t = after.getTokenRange().map(r -> r.getEnd()).flatMap(JavaToken::getNextToken);
        while (t.isPresent()) {
            JavaToken tok = t.get();
            if (tok.getText().equals(text)) return tok.getRange().map(r -> r.begin.line).orElse(fallback);
            if (!tok.getCategory().isWhitespaceOrComment() && !tok.getText().equals(")")) break;
            t = tok.getNextToken();
        }
        return fallback;
    }

    Map<String, Object> hitAt(String file, int line, int inc, String why) {
        List<String> ls = lines.getOrDefault(file, List.of());
        String src = line >= 1 && line <= ls.size() ? ls.get(line - 1).strip() : "";
        if (src.length() > MAX_LINE) src = src.substring(0, MAX_LINE) + "…";
        Map<String, Object> h = new LinkedHashMap<>();
        h.put("file", file); h.put("line", line); h.put("code", src); h.put("inc", inc); h.put("why", why);
        return h;
    }

    void score() {
        for (Method m : methods.values()) {
            m.cyc = 1;
            if (m.body == null) continue;
            Scorer s = new Scorer(m);
            // A method's own nesting starts at 0; its body is a block.
            s.walk(m.body, 0);
        }
    }

    // ── resolving calls ──────────────────────────────────────────────────────────────────

    /** What a call site means: the declaration it binds to (if ours), and every body that may run. */
    static final class Target {
        String primary;               // the declaration the call binds to, when it is ours
        final List<String> run = new ArrayList<>();
        String unresolved;            // why the call could not be placed, when it is a lost project call
    }

    void resolveAll() {
        for (Method m : methods.values()) {
            boolean recursed = false;
            for (Node site : m.sites) {
                Target t;
                try {
                    t = resolve(site, m);
                } catch (Throwable ex) {   // StackOverflowError included: the solver is recursive
                    t = new Target();
                    t.unresolved = "solver error: " + ex.getClass().getSimpleName();
                }
                if (t.unresolved != null) {
                    if (projectish(site, m)) {
                        Map<String, Object> u = new LinkedHashMap<>();
                        u.put("call", oneLine(site.toString()));
                        u.put("file", m.file);
                        u.put("line", line(site));
                        u.put("reason", t.unresolved);
                        m.unresolved.add(u);
                    }
                    continue;
                }
                if (!recursed && m.key.equals(t.primary)) {
                    recursed = true;
                    m.cc += 1;
                    m.hits.add(hitAt(m.file, line(site), 1, "recursion"));
                }
                for (String k : t.run) if (!k.equals(m.key) && !m.callees.contains(k)) m.callees.add(k);
            }
        }
    }

    /** Is a call the solver could not place one into this project — or a library's? */
    boolean projectish(Node site, Method m) {
        String name = siteName(site);
        if (name == null) return false;
        if (site instanceof ObjectCreationExpr oc) return lookupSimple(oc.getType().getNameAsString(), m.owner.cu, m.owner) != null;
        if (OBJECT_METHODS.contains(name)) return false;
        return byName.containsKey(name);
    }

    static String siteName(Node site) {
        if (site instanceof MethodCallExpr mc) return mc.getNameAsString();
        if (site instanceof MethodReferenceExpr mr) return mr.getIdentifier();
        if (site instanceof ObjectCreationExpr oc) return oc.getType().getNameAsString();
        return null;
    }

    static String oneLine(String s) {
        String o = s.replaceAll("\\s+", " ").strip();
        return o.length() > 120 ? o.substring(0, 120) + "…" : o;
    }

    Target resolve(Node site, Method from) {
        if (site instanceof MethodCallExpr mc) return resolveCall(mc, from);
        if (site instanceof MethodReferenceExpr mr) return resolveRef(mr, from);
        if (site instanceof ObjectCreationExpr oc) return resolveNew(oc, from);
        if (site instanceof ExplicitConstructorInvocationStmt ec) return resolveExplicit(ec, from);
        return new Target();
    }

    Target bound(Node declAst, boolean virtual, Method from) {
        Target t = new Target();
        Method d = declAst == null ? null : byPos.get(pos(declAst));
        if (d == null || d.key == null) return t;   // a library's, generated, or inside a local class
        d = sameModuleTwin(d, from);
        t.primary = d.key;
        if (d.body != null) t.run.add(d.key);
        if (virtual && !d.isStatic && !d.isPrivate && !d.isCtor && !d.isFinal)
            for (String o : d.overriders) if (!t.run.contains(o)) t.run.add(o);
        return t;
    }

    /**
     * The same class in two modules: the solver hands back whichever it met first, but a
     * module calls its own copy. Prefer the twin declared in the caller's module.
     */
    Method sameModuleTwin(Method d, Method from) {
        List<String> twins = typesByBase.getOrDefault(d.owner.base, List.of());
        if (twins.size() < 2 || module(d.file).equals(module(from.file))) return d;
        for (String q : twins) {
            TypeInfo ti = types.get(q);
            if (ti == null || !module(ti.file).equals(module(from.file))) continue;
            for (Method m : ti.methods)
                if (m.name.equals(d.name) && m.params.equals(d.params) && m.isCtor == d.isCtor) return m;
        }
        return d;
    }

    /** A class named by its base name: the caller's module's copy first, then the others. */
    List<String> twins(String base, Method from) {
        List<String> all = new ArrayList<>(typesByBase.getOrDefault(base, List.of(base)));
        all.sort(Comparator.comparing(q -> module(types.get(q) == null ? "" : types.get(q).file).equals(module(from.file)) ? 0 : 1));
        return all;
    }

    static String module(String rel) {
        int at = rel.indexOf("src/main/java/");
        return at > 0 ? rel.substring(0, at) : "";
    }

    Target resolveCall(MethodCallExpr mc, Method from) {
        boolean viaSuper = mc.getScope().map(s -> s instanceof SuperExpr).orElse(false);
        try {
            ResolvedMethodDeclaration d = mc.resolve();
            return bound(d.toAst().orElse(null), !viaSuper, from);
        } catch (Throwable ex) {
            return fallbackCall(mc.getNameAsString(), mc.getArguments().size(), mc.getScope().orElse(null), from, false);
        }
    }

    /**
     * The solver could not bind the call — usually an argument whose type lives in a library
     * jar it does not have. Bind it by name and arity instead, but only on a receiver whose
     * type is known: every overload that fits is followed (an over-count, stated), and a
     * receiver whose type is unknown is reported, never guessed at.
     */
    Target fallbackCall(String name, int arity, Expression scope, Method from, boolean anyArity) {
        Target t = new Target();
        List<String> owners = new ArrayList<>();
        boolean virtual = true, payload = false;
        if (scope == null || scope instanceof ThisExpr) {
            for (TypeInfo o = from.owner; o != null; o = o.outer) owners.add(o.qname);
            // A static import of a project method.
            for (ImportDeclaration imp : from.owner.cu.getImports()) {
                if (!imp.isStatic()) continue;
                String n = imp.getNameAsString();
                if (imp.isAsterisk() && types.containsKey(n)) owners.addAll(twins(n, from));
                else if (n.endsWith("." + name) && types.containsKey(n.substring(0, n.length() - name.length() - 1)))
                    owners.addAll(twins(n.substring(0, n.length() - name.length() - 1), from));
            }
        } else if (scope instanceof SuperExpr) {
            owners.addAll(from.owner.supers);
            virtual = false;
            if (from.owner.supers.isEmpty()) return t;   // a library superclass's method
        } else {
            Inferred inf;
            boolean typed = false;
            try {
                ResolvedType rt = scope.calculateResolvedType();
                typed = true;
                String q = rt.isReferenceType() ? rt.asReferenceType().getQualifiedName() : null;
                if (q == null || !types.containsKey(q)) return t;   // a library type's method, a primitive, a type variable
                inf = new Inferred();
                inf.types.add(q);
            } catch (Throwable ex) {
                // The solver gave up on the receiver. What it is can still be read off the
                // source: the declaration of a variable (`MapSqlParameterSource p`, `VectorStore
                // store`), the static type a chain arrives at (a Lombok getter returns its
                // field's type), or the project type a library value carries
                // (`repo.findById(id).orElseThrow()` is an Owner when repo is a
                // `JpaRepository<Owner, Long>`; `ResponseEntity<Owner>.getBody()` is an Owner).
                inf = infer(scope, from, 0);
                if (inf.declaredLibrary) return t;   // `ResponseEntity<Owner> r; r.getStatusCode()`: the library's method
                if (DEBUG != null && scope.toString().contains(DEBUG)) {
                    System.err.println("[debug] " + scope + "." + name + " types=" + inf.types + " lib=" + inf.library
                            + " payload=" + inf.payload + " tv=" + inf.typeVar + " args=" + inf.args + " ex=" + ex);
                    if (scope instanceof NameExpr dn) System.err.println("[debug]   decl=" + declarationOf(dn, from));
                }
                if (inf.types.isEmpty()) {
                    if (inf.library) return t;   // a library value with nothing of ours inside
                    // An unknown receiver called with a name every JDK value answers to —
                    // `x.equalsIgnoreCase(…)`, `x.stream()`, `x.add(…)` — is taken for the
                    // JDK's, even when the project happens to declare a method of that name.
                    if (JDK_NAMES.contains(name) && !(unwrap(scope) instanceof NameExpr)) return t;
                    t.unresolved = "receiver type unknown";
                    return t;
                }
            }
            for (String q : inf.types) owners.addAll(twins(types.get(q) == null ? q : types.get(q).base, from));
            payload = inf.payload;
        }
        boolean external = false;
        LinkedHashSet<Method> found = new LinkedHashSet<>();
        for (String o : owners) {
            TypeInfo ti = types.get(o);
            if (ti == null) continue;
            if (ti.externalSuper) external = true;
            List<String> chain = new ArrayList<>();
            chain.add(o);
            chain.addAll(ti.allSupers);
            for (String c : chain) {
                TypeInfo ct = types.get(c);
                if (ct == null) continue;
                for (Method m : ct.methods)
                    if (!m.isCtor && m.name.equals(name) && (anyArity || arityFits(m, arity))) found.add(m);
            }
            if (!found.isEmpty() && !payload) break;   // the nearest enclosing type that declares it wins
        }
        if (found.isEmpty() && (scope == null || scope instanceof ThisExpr) && staticLibraryImport(from.owner.cu, name)) return t;
        if (payload) {
            // The receiver only *carries* project types (`Optional<Owner>`, `Page<Post>`): a name
            // the container answers to is the container's, and a name more than one carried
            // type declares cannot be told apart — reported, never guessed.
            if (JDK_NAMES.contains(name) || CONTAINER_NAMES.contains(name)) return t;
            Set<String> owning = found.stream().map(m -> m.owner.qname).collect(Collectors.toCollection(LinkedHashSet::new));
            Set<String> roots = new LinkedHashSet<>();
            for (String o : owners) {
                TypeInfo ti = types.get(o);
                if (ti == null) continue;
                List<String> chain = new ArrayList<>(List.of(o));
                chain.addAll(ti.allSupers);
                if (chain.stream().anyMatch(owning::contains)) roots.add(o);
            }
            if (roots.size() > 1) {
                t.unresolved = "ambiguous: " + name + " on " + roots.stream().map(q -> q.substring(q.lastIndexOf('.') + 1))
                        .collect(Collectors.joining(" or "));
                return t;
            }
        }
        if (found.isEmpty()) {
            // Inherited from a library supertype (`JpaRepository.findById`), generated by
            // Lombok, a library method on a value that merely carries one of ours
            // (`Optional<Owner>.orElseThrow`), or not ours at all: nothing in the source to follow.
            if (payload || external || !byName.containsKey(name) || OBJECT_METHODS.contains(name)) return t;
            for (String o : owners) if (lombokGenerated(types.get(o), name)) return t;
            t.unresolved = "no " + name + "/" + arity + " on " + owners.stream().map(s -> s.substring(s.lastIndexOf('.') + 1)).collect(Collectors.joining(", "));
            return t;
        }
        for (Method m : found) {
            if (t.primary == null) t.primary = m.key;
            if (m.body != null && !t.run.contains(m.key)) t.run.add(m.key);
            if (virtual && !m.isStatic && !m.isPrivate && !m.isFinal)
                for (String o : m.overriders) if (!t.run.contains(o)) t.run.add(o);
        }
        if (found.size() > 1) t.primary = null;   // ambiguous: no recursion claim on a guess
        return t;
    }

    // ── what a receiver is, when the solver cannot say ───────────────────────────────────

    /** Project types an expression is, or carries. Empty and `library` = nothing of ours. */
    static final class Inferred {
        final Set<String> types = new LinkedHashSet<>();
        boolean payload;   // `types` are what a library value carries (`Optional<Owner>`), not its own type
        boolean library;
        boolean typeVar;   // the type was (or carried) a type variable: what it stands for is in the receiver's `args`
        boolean declaredLibrary;   // the expression's own static type is a library type: a call on it is the library's
        final Set<String> args = new LinkedHashSet<>();   // project types in the static type's <…>

        static Inferred lib() { Inferred i = new Inferred(); i.library = true; return i; }

        static Inferred of(String q, boolean payload) {
            Inferred i = new Inferred();
            i.types.add(q);
            i.payload = payload;
            return i;
        }
    }

    static final Set<String> LOMBOK_LOG = Set.of("Slf4j", "XSlf4j", "Log4j", "Log4j2", "Log", "CommonsLog",
            "JBossLog", "Flogger", "CustomLog");

    Inferred infer(Expression e, Method from, int depth) {
        if (depth > 25) return new Inferred();
        while (e instanceof EnclosedExpr en) e = en.getInner();
        try {
            return fromResolved(e.calculateResolvedType());
        } catch (Throwable ignored) {
            // read it off the source below
        }
        if (e instanceof CastExpr c) return fromType(c.getType(), from);
        if (e instanceof ThisExpr th) {
            if (th.getTypeName().isPresent()) {
                String q = lookupSimple(th.getTypeName().get().getIdentifier(), from.owner.cu, from.owner);
                return q == null ? new Inferred() : Inferred.of(q, false);
            }
            return Inferred.of(from.owner.qname, false);
        }
        if (e instanceof ObjectCreationExpr oc) return fromType(oc.getType(), from);
        if (e instanceof SuperExpr) {
            if (from.owner.supers.isEmpty()) return Inferred.lib();   // a library superclass
            Inferred i = new Inferred();
            i.types.addAll(from.owner.supers);
            return i;
        }
        if (e instanceof LiteralExpr) return Inferred.lib();
        if (e instanceof NameExpr ne) {
            String n = ne.getNameAsString();
            Node d = declarationOf(ne, from);
            if (d instanceof Parameter p) {
                if (p.getType() instanceof UnknownType) {
                    if (p.getParentNode().orElse(null) instanceof LambdaExpr lam) return lambdaParam(lam, p, from, depth);
                    return new Inferred();
                }
                return fromType(p.getType(), from);
            }
            if (d instanceof VariableDeclarator vd) {
                if (vd.getType().isVarType())
                    return vd.getInitializer().map(i -> infer(i, from, depth + 1)).orElse(new Inferred());
                return fromType(vd.getType(), from);
            }
            if (Character.isUpperCase(n.charAt(0))) {
                String q = lookupSimple(n, from.owner.cu, from.owner);
                return q != null ? Inferred.of(q, false) : Inferred.lib();   // `Owner.of(…)` vs `Integer.parseInt`
            }
            if (n.equals("log")) {
                for (TypeInfo o = from.owner; o != null; o = o.outer)
                    if (o.node instanceof NodeWithAnnotations<?> na && na.getAnnotations().stream()
                            .anyMatch(a -> LOMBOK_LOG.contains(a.getName().getIdentifier()))) return Inferred.lib();
            }
            return new Inferred();
        }
        if (e instanceof FieldAccessExpr fa) {
            String n = fa.getNameAsString();
            if (Character.isUpperCase(n.charAt(0))) {
                if (types.containsKey(fa.toString())) return Inferred.of(fa.toString(), false);
                String q = lookupSimple(n, from.owner.cu, from.owner);
                if (q != null) return Inferred.of(q, false);
            }
            Inferred s = infer(fa.getScope(), from, depth + 1);
            for (String q : s.types) {
                VariableDeclarator f = fieldOf(types.get(q), n);
                if (f != null) return fromType(f.getType(), methodContext(types.get(q), from));
            }
            return s.library ? Inferred.lib() : new Inferred();
        }
        if (e instanceof MethodCallExpr mc) {
            String name = mc.getNameAsString();
            int arity = mc.getArguments().size();
            Inferred s = mc.getScope().map(x -> infer(x, from, depth + 1)).orElse(Inferred.of(from.owner.qname, false));
            // A library method on a library value that carries nothing of ours returns nothing
            // of ours we could follow (`root.join(…).get(…)`, `page.getPageable()`).
            Inferred out = new Inferred();
            for (Expression a : mc.getArguments()) {
                // `rest.getForObject(url, Owner.class)`: the class literal says what comes back.
                if (a instanceof ClassExpr ce && ce.getType() instanceof ClassOrInterfaceType ct) {
                    String q = lookupSimple(ct.getNameAsString(), from.owner.cu, from.owner);
                    if (q != null) { out.types.add(q); out.payload = true; }
                }
            }
            boolean literal = !out.types.isEmpty();   // `mapper.map(order, OrderDto.class)` is an OrderDto
            if ((s.library || s.declaredLibrary) && s.types.isEmpty() && !literal) {
                // A library call handed project values (`Mono.just(order)`,
                // `Flux.fromIterable(orders)`, `Optional.of(owner)`) carries them on.
                for (Expression a : mc.getArguments()) {
                    if (a instanceof LambdaExpr || a instanceof MethodReferenceExpr) continue;
                    Inferred ai = infer(a, from, depth + 1);
                    if (!ai.types.isEmpty()) { out.types.addAll(ai.types); out.payload = true; }
                }
            }
            if (s.declaredLibrary && s.types.isEmpty() && out.types.isEmpty()) return Inferred.lib();
            if (s.payload && MAPPERS.contains(name)) {
                // `stream.map(o -> toDto(o))`, `mono.flatMap(…)`: the element is whatever the
                // function returns, no longer what the container carried.
                Inferred el = new Inferred();
                if (!mc.getArguments().isEmpty() && mc.getArgument(0) instanceof LambdaExpr fl
                        && fl.getExpressionBody().isPresent()) {
                    Inferred r = infer(fl.getExpressionBody().get(), from, depth + 1);
                    el.types.addAll(r.types);
                    el.library = r.library;
                }
                el.payload = !el.types.isEmpty();
                return el;
            }
            if (s.payload && (JDK_NAMES.contains(name) || CONTAINER_NAMES.contains(name))) {
                out.types.addAll(s.types);   // `list.get(0)`, `opt.orElseThrow()`: still what the container carries
                out.payload = true;
                return out;
            }
            int libraryResults = 0, otherResults = 0;
            for (String q : s.types) {
                TypeInfo ti = types.get(q);
                if (ti == null) continue;
                List<Method> found = new ArrayList<>();
                List<String> chain = new ArrayList<>(List.of(q));
                chain.addAll(ti.allSupers);
                for (String c : chain) {
                    TypeInfo ct = types.get(c);
                    if (ct != null) for (Method m : ct.methods)
                        if (!m.isCtor && m.name.equals(name) && arityFits(m, arity)) found.add(m);
                }
                if (!found.isEmpty()) {
                    for (Method m : found) {
                        if (m.decl instanceof MethodDeclaration md) {
                            Inferred r = fromType(md.getType(), m);
                            out.types.addAll(r.types);
                            out.payload |= r.payload;
                            if (r.library || (r.declaredLibrary && r.types.isEmpty() && !r.typeVar)) libraryResults++;
                            else otherResults++;
                            if (r.typeVar) {
                                // `Optional<T> findByName(…)` on a `NamedRepository<Product>`:
                                // T is whatever the receiver was declared with.
                                out.types.addAll(s.args);
                                out.types.addAll(ti.superArgs);
                                out.payload = true;
                            }
                        }
                    }
                    continue;
                }
                VariableDeclarator lf = lombokField(ti, name);
                if (lombokGenerated(ti, name) && name.matches("(set|with)[A-Z].*|builder|toBuilder")) {
                    // `@With`/chained `@Setter` return the object itself; `builder()` a builder
                    // whose `build()` is the object again — carried as a payload through the chain.
                    out.types.add(q);
                    out.payload |= name.endsWith("uilder");
                } else if (lf != null) {
                    Inferred r = fromType(lf.getType(), methodContext(ti, from));
                    out.types.addAll(r.types);
                    out.payload |= r.payload;
                } else if (s.payload) {
                    out.types.add(q);           // a library call on a value carrying q: still carries q
                    out.payload = true;
                } else if (ti.externalSuper && !ti.externalArgs.isEmpty()) {
                    out.types.addAll(ti.externalArgs);   // `JpaRepository<Owner, Long>.findById` carries Owner
                    out.payload = true;
                }
            }
            // Every method it can be returns a library value with nothing of ours in it
            // (`List<String>`, `BigDecimal`): what is called on that is the library's.
            if (out.types.isEmpty() && (s.library || (libraryResults > 0 && otherResults == 0))) return Inferred.lib();
            return out;
        }
        return new Inferred();
    }

    /** A placeholder method whose owner is `t` — the type-variable scope `fromType` needs. */
    Method methodContext(TypeInfo t, Method fallback) {
        if (t == null) return fallback;
        if (!t.methods.isEmpty()) return t.methods.get(0);
        Method m = new Method();
        m.owner = t;
        return m;
    }

    Inferred fromResolved(ResolvedType rt) {
        if (rt.isReferenceType()) {
            String q = rt.asReferenceType().getQualifiedName();
            if (types.containsKey(q)) {
                Inferred own = Inferred.of(q, false);
                Inferred a = new Inferred();
                collectResolvedArgs(rt, a, 0);
                own.args.addAll(a.types);
                return own;
            }
            Inferred out = new Inferred();
            collectResolvedArgs(rt, out, 0);
            out.declaredLibrary = true;
            out.args.addAll(out.types);
            if (out.types.isEmpty()) out.library = true;
            else out.payload = true;
            return out;
        }
        if (rt.isPrimitive() || rt.isVoid() || rt.isNull()) return Inferred.lib();
        if (rt.isArray()) return fromResolved(rt.asArrayType().getComponentType());
        return new Inferred();   // a type variable, a wildcard: unknown
    }

    void collectResolvedArgs(ResolvedType rt, Inferred out, int depth) {
        if (depth > 6 || !rt.isReferenceType()) return;
        for (ResolvedType a : rt.asReferenceType().typeParametersValues()) {
            if (a.isReferenceType()) {
                String q = a.asReferenceType().getQualifiedName();
                if (types.containsKey(q)) out.types.add(q);
                collectResolvedArgs(a, out, depth + 1);
            }
        }
    }

    Inferred fromType(Type t, Method ctx) {
        if (t.isPrimitiveType() || t.isVoidType()) return Inferred.lib();
        if (t instanceof ArrayType a) return fromType(a.getComponentType(), ctx);
        if (t instanceof ClassOrInterfaceType ct) {
            TypeInfo where = ctx == null ? null : ctx.owner;
            if (isTypeVar(ct.getNameAsString(), ctx)) {
                Inferred v = new Inferred();
                v.typeVar = true;
                return v;
            }
            String q = where == null ? null : writtenType(ct, where);
            Inferred out = new Inferred();
            out.args.addAll(projectTypesIn(t, where));
            for (ClassOrInterfaceType c : t.findAll(ClassOrInterfaceType.class))
                if (c != t && isTypeVar(c.getNameAsString(), ctx)) out.typeVar = true;
            if (q != null) {
                out.types.add(q);
                out.typeVar = false;
                return out;
            }
            out.types.addAll(out.args);
            out.declaredLibrary = true;
            if (out.types.isEmpty() && !out.typeVar) out.library = true;
            else if (!out.types.isEmpty()) out.payload = true;
            return out;
        }
        return new Inferred();
    }

    /** The project types a written type is or carries: `Owner` -> {Owner}, `List<Owner>` -> {Owner}. */
    Set<String> typesOf(Type a, TypeInfo where) {
        Set<String> out = new LinkedHashSet<>();
        if (a instanceof ClassOrInterfaceType c) {
            String q = writtenType(c, where);
            if (q != null) { out.add(q); return out; }
        }
        out.addAll(projectTypesIn(a, where));
        return out;
    }

    /** Project types named anywhere inside a written type's type arguments. */
    Set<String> projectTypesIn(Type t, TypeInfo where) {
        Set<String> out = new LinkedHashSet<>();
        for (ClassOrInterfaceType c : t.findAll(ClassOrInterfaceType.class)) {
            if (c == t) continue;
            String q = lookupSimple(c.getNameAsString(), where == null ? null : where.cu, where);
            if (q != null) out.add(q);
        }
        return out;
    }

    /** The declaration a name refers to: by the solver, else by scope, read off the source. */
    Node declarationOf(NameExpr ne, Method from) {
        String n = ne.getNameAsString();
        try {
            Node d = ne.resolve().toAst().orElse(null);
            if (d instanceof VariableDeclarationExpr vde)
                d = vde.getVariables().stream().filter(v -> v.getNameAsString().equals(n)).findFirst().orElse(null);
            if (d instanceof FieldDeclaration fd)
                d = fd.getVariables().stream().filter(v -> v.getNameAsString().equals(n)).findFirst().orElse(null);
            if (d instanceof Parameter || d instanceof VariableDeclarator) return d;
        } catch (Throwable ignored) {
            // the solver cannot see it; read the scopes
        }
        int line = line(ne);
        for (Node a = ne.getParentNode().orElse(null); a != null; a = a.getParentNode().orElse(null)) {
            if (a instanceof LambdaExpr l)
                for (Parameter p : l.getParameters()) if (p.getNameAsString().equals(n)) return p;
            if (a instanceof CatchClause cc && cc.getParameter().getNameAsString().equals(n)) return cc.getParameter();
            if (a instanceof CallableDeclaration<?> c) {
                for (Parameter p : c.getParameters()) if (p.getNameAsString().equals(n)) return p;
            }
            if (a instanceof CallableDeclaration<?> || a instanceof LambdaExpr || a instanceof InitializerDeclaration
                    || a instanceof CompactConstructorDeclaration) {
                VariableDeclarator best = null;
                for (VariableDeclarator vd : a.findAll(VariableDeclarator.class)) {
                    if (!vd.getNameAsString().equals(n) || vd.getParentNode().orElse(null) instanceof FieldDeclaration) continue;
                    if (line(vd) <= line) best = vd;
                }
                if (best != null) return best;
            }
            if (a instanceof TypeDeclaration<?> td) {
                for (FieldDeclaration f : td.getFields())
                    for (VariableDeclarator v : f.getVariables()) if (v.getNameAsString().equals(n)) return v;
            }
        }
        for (String s : from.owner.allSupers) {
            VariableDeclarator f = fieldOf(types.get(s), n);
            if (f != null) return f;
        }
        return null;
    }

    static VariableDeclarator fieldOf(TypeInfo t, String name) {
        if (t == null || !(t.node instanceof TypeDeclaration<?> td)) return null;
        for (FieldDeclaration f : td.getFields())
            for (VariableDeclarator v : f.getVariables()) if (v.getNameAsString().equals(name)) return v;
        if (td instanceof RecordDeclaration rd)
            for (Parameter p : rd.getParameters()) if (p.getNameAsString().equals(name)) return new VariableDeclarator(p.getType(), name);
        return null;
    }

    static final Set<String> LOMBOK_TYPE = Set.of("Data", "Getter", "Setter", "Value", "With", "Wither",
            "Builder", "SuperBuilder", "Accessors");
    static final Set<String> LOMBOK_FIELD = Set.of("Getter", "Setter", "With", "Wither");

    /** A getter, setter, wither or builder Lombok writes at compile time — never in the source. */
    boolean lombokGenerated(TypeInfo t, String name) {
        if (t == null) return false;
        List<TypeInfo> chain = new ArrayList<>();
        chain.add(t);
        for (String s : t.allSupers) if (types.containsKey(s)) chain.add(types.get(s));
        for (TypeInfo c : chain) {
            if (!(c.node instanceof TypeDeclaration<?> td)) continue;
            boolean onType = td.getAnnotations().stream().anyMatch(a -> LOMBOK_TYPE.contains(a.getName().getIdentifier()));
            if (onType && Set.of("builder", "toBuilder", "build").contains(name)) return true;
            String prop = name.replaceFirst("^(get|set|is|with)(?=[A-Z])", "");
            if (prop.equals(name) || prop.isEmpty()) continue;
            String field = Character.toLowerCase(prop.charAt(0)) + prop.substring(1);
            for (FieldDeclaration f : td.getFields()) {
                boolean onField = f.getAnnotations().stream().anyMatch(a -> LOMBOK_FIELD.contains(a.getName().getIdentifier()));
                if ((onType || onField) && f.getVariables().stream().anyMatch(v -> v.getNameAsString().equals(field)
                        || (name.startsWith("is") && v.getNameAsString().equals(name))))
                    return true;
            }
        }
        return false;
    }

    /** The field a Lombok getter (`getX`/`isX`) reads, when Lombok writes that getter. */
    VariableDeclarator lombokField(TypeInfo t, String name) {
        if (t == null) return null;
        String prop = name.replaceFirst("^(get|is)(?=[A-Z])", "");
        if (prop.equals(name) || !lombokGenerated(t, name)) return null;
        String field = Character.toLowerCase(prop.charAt(0)) + prop.substring(1);
        VariableDeclarator f = fieldOf(t, field);
        for (Iterator<String> it = t.allSupers.iterator(); f == null && it.hasNext(); ) f = fieldOf(types.get(it.next()), field);
        return f != null ? f : fieldOf(t, name);   // a boolean field named `isActive` has the getter `isActive()`
    }

    static final Set<String> LOMBOK_CTOR = Set.of("NoArgsConstructor", "AllArgsConstructor",
            "RequiredArgsConstructor", "Data", "Value", "Builder", "SuperBuilder");

    /** `import static com.google.common.base.Preconditions.checkNotNull;` (or `.*` of a type not ours). */
    boolean staticLibraryImport(CompilationUnit cu, String name) {
        for (ImportDeclaration imp : cu.getImports()) {
            if (!imp.isStatic()) continue;
            String n = imp.getNameAsString();
            if (imp.isAsterisk() ? !types.containsKey(n)
                    : n.endsWith("." + name) && !types.containsKey(n.substring(0, n.length() - name.length() - 1)))
                return true;
        }
        return false;
    }

    /**
     * The project type a written type names. `SomeLibraryResult.Flight` is the library's
     * nested class even when this project has a `Flight` of its own: a qualified name is
     * looked up whole, never by its last segment.
     */
    String writtenType(ClassOrInterfaceType ct, TypeInfo where) {
        if (ct.getScope().isEmpty()) return lookupSimple(ct.getNameAsString(), where.cu, where);
        List<String> parts = new ArrayList<>();
        for (ClassOrInterfaceType c = ct; c != null; c = c.getScope().orElse(null)) parts.add(0, c.getNameAsString());
        String whole = String.join(".", parts);
        if (types.containsKey(whole)) return whole;
        if (!Character.isUpperCase(parts.get(0).charAt(0))) return null;   // a package-qualified name not ours
        String outer = lookupSimple(parts.get(0), where.cu, where);
        if (outer == null) return null;
        String q = outer + "." + String.join(".", parts.subList(1, parts.size()));
        return types.containsKey(q) ? q : null;
    }

    /** Method names of the JDK types code calls most — a call by one of them on an unknown receiver is the JDK's. */
    static final Set<String> JDK_NAMES = new HashSet<>();
    static {
        for (Class<?> c : List.of(Object.class, String.class, StringBuilder.class, CharSequence.class,
                Collection.class, List.class, Set.class, Map.class, Map.Entry.class, Optional.class, Iterator.class,
                java.util.stream.Stream.class, java.util.stream.Collectors.class, java.math.BigDecimal.class,
                java.math.BigInteger.class, Integer.class, Long.class, Double.class, Boolean.class,
                java.time.LocalDate.class, java.time.LocalDateTime.class, java.time.ZonedDateTime.class,
                java.time.Instant.class, java.time.Duration.class, java.util.Date.class, Throwable.class,
                java.util.function.Function.class, java.util.function.Predicate.class, java.util.function.Supplier.class,
                java.util.function.Consumer.class))
            for (java.lang.reflect.Method m : c.getMethods()) JDK_NAMES.add(m.getName());
    }

    /**
     * What an untyped lambda parameter is. In order: the parameter type of the project method
     * the lambda is handed to (`withRetry(rt, calc -> calc.apply(o))` with a
     * `Consumer<PriceCalculator>`); the element of the value a one-parameter lambda is called
     * on (`owners.stream().map(o -> …)`, `Mono.just(order).map(x -> …)`); the declared
     * functional type it is assigned or returned as (`ItemProcessor<Order, Invoice> p = o -> …`).
     * A library target with nothing of ours in it makes the parameter the library's
     * (`Specification<T> s = (root, query, cb) -> …`); anything else stays unknown — reported.
     */
    Inferred lambdaParam(LambdaExpr lam, Parameter p, Method from, int depth) {
        int idx = lam.getParameters().indexOf(p);
        Node parent = lam.getParentNode().orElse(null);
        while (parent instanceof EnclosedExpr || parent instanceof CastExpr) parent = parent.getParentNode().orElse(null);
        if (parent instanceof MethodCallExpr call) {
            int argAt = call.getArguments().indexOf(lam);
            List<String> owners = new ArrayList<>();
            if (call.getScope().isEmpty() || call.getScope().get() instanceof ThisExpr) {
                for (TypeInfo o = from.owner; o != null; o = o.outer) owners.add(o.qname);
            } else {
                Inferred s = infer(call.getScope().get(), from, depth + 1);
                if (!s.payload) owners.addAll(s.types);
            }
            List<Method> callees = new ArrayList<>();
            for (String o : owners) {
                TypeInfo ti = types.get(o);
                if (ti == null) continue;
                List<String> chain = new ArrayList<>(List.of(o));
                chain.addAll(ti.allSupers);
                for (String c : chain) {
                    TypeInfo ct = types.get(c);
                    if (ct != null) for (Method m : ct.methods)
                        if (m.name.equals(call.getNameAsString()) && arityFits(m, call.getArguments().size())) callees.add(m);
                }
                if (!callees.isEmpty()) break;
            }
            if (!callees.isEmpty() && argAt >= 0) {
                Inferred out = new Inferred();
                boolean exact = true, library = true;
                for (Method m : callees) {
                    if (!(m.decl instanceof CallableDeclaration<?> cd) || argAt >= cd.getParameters().size()) continue;
                    Type ft = cd.getParameter(argAt).getType();
                    FArg fa = functionalArg(ft, idx, lam.getParameters().size(), m);
                    out.types.addAll(fa.types);
                    exact &= fa.exact;
                    library &= fa.library;
                    if (fa.typeVar != null) {
                        // `<T> void each(List<T> xs, …, Consumer<T> c)`: T is what the
                        // argument passed for a `List<T>` parameter carries.
                        for (int j = 0; j < cd.getParameters().size() && j < call.getArguments().size(); j++) {
                            if (j == argAt) continue;
                            Type pj = cd.getParameter(j).getType();
                            boolean mentions = pj.findAll(ClassOrInterfaceType.class).stream()
                                    .anyMatch(c -> c.getNameAsString().equals(fa.typeVar));
                            if (!mentions) continue;
                            Inferred aj = infer(call.getArguments().get(j), from, depth + 1);
                            out.types.addAll(aj.types);
                            exact = false;
                        }
                    }
                }
                if (out.types.isEmpty() && library) return Inferred.lib();
                out.payload = !out.types.isEmpty() && !exact;   // `Consumer<PriceCalculator>`: the parameter IS one
                return out;   // a project callee: what it says, or unknown — reported
            }
            if (lam.getParameters().size() == 1 && call.getScope().isPresent()) {
                Inferred s = infer(call.getScope().get(), from, depth + 1);
                Inferred el = new Inferred();
                el.types.addAll(s.types);
                el.payload = !el.types.isEmpty();
                el.library = s.library || (s.declaredLibrary && s.types.isEmpty());
                return el;
            }
            if (call.getScope().isEmpty() && !byName.containsKey(call.getNameAsString())) return Inferred.lib();
            return new Inferred();
        }
        Type target = null;
        Method ctx = from;
        if (parent instanceof VariableDeclarator vd && !vd.getType().isVarType()) target = vd.getType();
        if (parent instanceof ReturnStmt) {
            Node m = parent;
            while (m != null && !(m instanceof MethodDeclaration) && !(m instanceof LambdaExpr)) m = m.getParentNode().orElse(null);
            if (m instanceof MethodDeclaration md) target = md.getType();
        }
        if (target != null) {
            FArg fa = functionalArg(target, idx, lam.getParameters().size(), ctx);
            if (!fa.types.isEmpty()) {
                Inferred out = new Inferred();
                out.types.addAll(fa.types);
                out.payload = !fa.exact;
                return out;
            }
            if (fa.library) return Inferred.lib();
        }
        if (parent instanceof ObjectCreationExpr oc && writtenType(oc.getType(), from.owner) == null) return Inferred.lib();
        return new Inferred();
    }

    /** What one parameter of a lambda typed by a functional type is. */
    static final class FArg {
        final Set<String> types = new LinkedHashSet<>();
        boolean exact;      // the parameter IS that project type (`Consumer<Owner>`)
        boolean library;    // nothing of ours can arrive there
        String typeVar;     // the parameter is a type variable of the callee, to be bound from its other arguments
    }

    /** `java.util.function`: the n-th parameter is the n-th type argument (all of them, for the operators). */
    static final Set<String> JDK_FUNCTIONAL = Set.of("Function", "Consumer", "Predicate", "BiFunction", "BiConsumer",
            "BiPredicate", "ToIntFunction", "ToLongFunction", "ToDoubleFunction", "ToIntBiFunction", "ToLongBiFunction",
            "ToDoubleBiFunction", "ObjIntConsumer", "ObjLongConsumer", "ObjDoubleConsumer", "Comparator", "Callable");
    static final Set<String> JDK_OPERATORS = Set.of("UnaryOperator", "BinaryOperator");
    /** Library callbacks whose parameters are the library's own objects, whatever their type argument. */
    static final Set<String> LIBRARY_CALLBACKS = Set.of("Specification", "RowMapper", "ResultSetExtractor",
            "RowCallbackHandler", "PreparedStatementSetter", "PreparedStatementCreator", "ConnectionCallback",
            "StatementCallback", "CallableStatementCallback", "TransactionCallback", "SessionCallback",
            "RedisCallback", "Answer", "ExchangeFilterFunction", "HandlerFunction", "RouterFunction");

    /**
     * The `idx`-th of `n` parameters of a lambda typed `t`. A `java.util.function` type maps
     * parameter to type argument one to one; a known library callback (`Specification<Owner>`,
     * `RowMapper<Row>`) hands the library's own objects (`Root`, `ResultSet`) whatever its
     * argument; any other one-parameter lambda takes the first type argument
     * (`ItemProcessor<Order, Invoice>`, `Converter<String, Score>`). Several parameters of an
     * unknown functional type are not guessed at.
     */
    FArg functionalArg(Type t, int idx, int n, Method ctx) {
        FArg out = new FArg();
        if (!(t instanceof ClassOrInterfaceType ct) || ctx == null || ctx.owner == null) return out;
        if (writtenType(ct, ctx.owner) != null) return out;   // a project functional interface: the solver types it
        String name = ct.getNameAsString();
        List<Type> args = ct.getTypeArguments().map(ArrayList::new).orElse(new ArrayList<>());
        if (LIBRARY_CALLBACKS.contains(name)) { out.library = true; return out; }
        Type arg = null;
        if (JDK_FUNCTIONAL.contains(name)) arg = idx < args.size() ? args.get(idx) : null;
        else if (JDK_OPERATORS.contains(name)) arg = args.isEmpty() ? null : args.get(0);
        else if (n == 1) arg = args.isEmpty() ? null : args.get(0);
        else {
            boolean ours = args.stream().anyMatch(a -> !typesOf(a, ctx.owner).isEmpty());
            out.library = !ours;
            return out;
        }
        if (arg == null) { out.library = args.isEmpty(); return out; }
        if (arg instanceof ClassOrInterfaceType ac && isTypeVar(ac.getNameAsString(), ctx)) {
            out.typeVar = ac.getNameAsString();
            return out;
        }
        out.types.addAll(typesOf(arg, ctx.owner));
        out.exact = arg instanceof ClassOrInterfaceType ac2 && writtenType(ac2, ctx.owner) != null;
        out.library = out.types.isEmpty();
        return out;
    }

    /** Names of the library containers project values travel in — Spring Data, Spring Web, Reactor. */
    static final Set<String> CONTAINER_NAMES = Set.of("getContent", "getTotalElements", "getTotalPages", "getNumber",
            "getNumberOfElements", "getSize", "hasContent", "hasNext", "hasPrevious", "getPageable", "getSort",
            "isFirst", "isLast", "nextPageable", "previousPageable", "getBody", "getStatusCode", "getStatusCodeValue",
            "getHeaders", "hasBody", "block", "blockFirst", "blockLast", "subscribe", "flatMap", "flatMapMany",
            "flatMapIterable", "zipWith", "zipWhen", "then", "thenReturn", "switchIfEmpty", "defaultIfEmpty",
            "collectList", "collectMap", "doOnNext", "doOnError", "doOnSuccess", "onErrorResume", "onErrorReturn",
            "onErrorMap", "log", "cache", "share", "next", "last", "take", "skip", "buffer", "window", "retry");

    /** Container methods whose function argument replaces the element. */
    static final Set<String> MAPPERS = Set.of("map", "flatMap", "mapNotNull", "concatMap", "switchMap", "flatMapMany",
            "flatMapIterable", "flatMapSequential", "thenApply", "thenCompose", "mapToObj", "transform", "handle");

    static Expression unwrap(Expression e) {
        while (e instanceof EnclosedExpr en) e = en.getInner();
        return e;
    }

    static boolean arityFits(Method m, int arity) {
        int n = m.params.size();
        if (n > 0 && m.params.get(n - 1).endsWith("...")) return arity >= n - 1;
        return n == arity;
    }

    Target resolveRef(MethodReferenceExpr mr, Method from) {
        String id = mr.getIdentifier();
        Expression scope = mr.getScope();
        if (id.equals("new")) {
            String q = null;
            if (scope instanceof TypeExpr te && te.getType() instanceof ClassOrInterfaceType ct)
                q = projectTypeOf(ct, from.owner);
            Target t = new Target();
            if (q == null) return t;
            for (Method m : types.get(q).methods)
                if (m.isCtor && m.body != null) { t.run.add(m.key); if (t.primary == null) t.primary = m.key; }
            return t;
        }
        boolean viaSuper = scope instanceof SuperExpr;
        try {
            ResolvedMethodDeclaration d = mr.resolve();
            return bound(d.toAst().orElse(null), !viaSuper, from);
        } catch (Throwable ex) {
            // `Type::name` — static, or unbound instance (`Owner::getId`): every `name` of that type.
            if (scope instanceof TypeExpr te && te.getType() instanceof ClassOrInterfaceType ct) {
                String q = projectTypeOf(ct, from.owner);
                return q == null ? new Target() : fallbackOn(q, id, from);
            }
            if (scope instanceof TypeExpr) return new Target();
            return fallbackCall(id, -1, scope, from, true);
        }
    }

    Target fallbackOn(String q, String name, Method from) {
        Target t = new Target();
        TypeInfo ti = types.get(q);
        List<String> chain = new ArrayList<>();
        chain.add(q);
        chain.addAll(ti.allSupers);
        LinkedHashSet<Method> found = new LinkedHashSet<>();
        for (String c : chain) {
            TypeInfo ct = types.get(c);
            if (ct == null) continue;
            for (Method m : ct.methods) if (!m.isCtor && m.name.equals(name)) found.add(m);
        }
        if (found.isEmpty()) {
            if (ti.externalSuper || !byName.containsKey(name)) return t;
            t.unresolved = "no " + name + " on " + ti.simple;
            return t;
        }
        for (Method m : found) {
            if (m.body != null && !t.run.contains(m.key)) t.run.add(m.key);
            if (!m.isStatic && !m.isPrivate && !m.isFinal)
                for (String o : m.overriders) if (!t.run.contains(o)) t.run.add(o);
        }
        if (found.size() == 1) t.primary = found.iterator().next().key;
        return t;
    }

    Target resolveNew(ObjectCreationExpr oc, Method from) {
        try {
            ResolvedConstructorDeclaration d = oc.resolve();
            Node ast = d.toAst().orElse(null);
            if (ast != null) return bound(ast, false, from);
        } catch (Throwable ignored) {
            // fall through to arity
        }
        String q = projectTypeOf(oc.getType(), from.owner);
        Target t = new Target();
        if (q == null) return t;                 // a library type
        List<Method> ctors = types.get(q).methods.stream().filter(x -> x.isCtor).collect(Collectors.toList());
        if (ctors.isEmpty()) return t;           // the implicit constructor: nothing to run
        int arity = oc.getArguments().size();
        List<Method> fit = ctors.stream().filter(c -> arityFits(c, arity)).collect(Collectors.toList());
        boolean lombokCtor = types.get(q).node instanceof NodeWithAnnotations<?> na && na.getAnnotations().stream()
                .anyMatch(a -> LOMBOK_CTOR.contains(a.getName().getIdentifier()));
        if (fit.isEmpty() && lombokCtor) return t;   // a constructor Lombok writes
        if (fit.isEmpty()) {
            t.unresolved = "no constructor " + q.substring(q.lastIndexOf('.') + 1) + "/" + arity;
            return t;
        }
        for (Method c : fit) if (c.body != null) t.run.add(c.key);
        if (fit.size() == 1) t.primary = fit.get(0).key;
        return t;
    }

    Target resolveExplicit(ExplicitConstructorInvocationStmt ec, Method from) {
        try {
            ResolvedConstructorDeclaration d = ec.resolve();
            Node ast = d.toAst().orElse(null);
            if (ast != null) return bound(ast, false, from);
        } catch (Throwable ignored) {
            // fall through to arity
        }
        Target t = new Target();
        List<String> owners = ec.isThis() ? List.of(from.owner.qname) : new ArrayList<>(from.owner.supers);
        int arity = ec.getArguments().size();
        for (String q : owners) {
            TypeInfo ti = types.get(q);
            if (ti == null || ti.isInterface) continue;
            for (Method c : ti.methods) if (c.isCtor && arityFits(c, arity) && c.body != null) t.run.add(c.key);
        }
        if (t.run.size() == 1) t.primary = t.run.get(0);
        return t;
    }

    // ── entry points ─────────────────────────────────────────────────────────────────────

    final List<Map<String, Object>> entries = new ArrayList<>();

    void findEntries() {
        for (TypeInfo t : types.values()) {
            String classPath = "";
            if (t.node instanceof NodeWithAnnotations<?> na) {
                for (AnnotationExpr a : na.getAnnotations()) {
                    if (MAPPINGS.containsKey(a.getName().getIdentifier())) classPath = firstString(a, t, "path", "value");
                }
            }
            for (Method m : t.methods) {
                if (!(m.decl instanceof MethodDeclaration md)) continue;
                String kind = null, verb = null, path = "";
                for (AnnotationExpr a : md.getAnnotations()) {
                    String n = a.getName().getIdentifier();
                    if (MAPPINGS.containsKey(n)) {
                        kind = "http";
                        verb = MAPPINGS.get(n);
                        if (n.equals("RequestMapping")) verb = requestMethod(a);
                        path = firstString(a, t, "path", "value");
                    } else if (n.equals("McpTool") || n.equals("Tool")) {
                        kind = "mcp"; verb = "MCP"; path = firstString(a, t, "name");
                    } else if (LISTENERS.containsKey(n)) {
                        kind = "listener"; verb = LISTENERS.get(n); path = firstString(a, t, "topics", "queues", "destination");
                    } else if (n.equals("Scheduled")) {
                        kind = "job"; verb = "JOB"; path = "";
                    }
                }
                if (kind == null) continue;
                Method handler = m;
                String prefix = classPath;
                if (t.isInterface) {
                    // A mapping declared on an interface (an OpenAPI-generated `…Api`, whose
                    // methods are often `default` stubs answering 501): the request runs the
                    // controller that implements it, so that is where the flow starts — and
                    // that controller's own class-level path is the one Spring serves.
                    List<Method> impls = m.overriders.stream().map(methods::get)
                            .filter(x -> x != null && x.body != null && !x.owner.isInterface).collect(Collectors.toList());
                    Method impl = impls.stream().filter(x -> isController(x.owner)).findFirst()
                            .orElse(impls.isEmpty() ? null : impls.get(0));
                    if (impl != null) {
                        handler = impl;
                        String own = classMapping(impl.owner);
                        if (own != null) prefix = own;
                    } else if (m.body == null) continue;
                }
                if (kind.equals("http")) {
                    String joined = prefix + "/" + path;
                    path = "/" + Arrays.stream(joined.split("/")).filter(s -> !s.isEmpty()).collect(Collectors.joining("/"));
                } else if (path.isEmpty()) {
                    path = handler.display.substring(0, handler.display.indexOf('('));
                }
                entries.add(entry(kind, verb, path, handler));
            }
        }
        entries.sort(Comparator.<Map<String, Object>>comparingInt(e -> -(Integer) e.get("flowCc"))
                .thenComparing(e -> (String) e.get("path")));
    }

    static boolean isController(TypeInfo t) {
        return t.node instanceof NodeWithAnnotations<?> na && na.getAnnotations().stream()
                .anyMatch(a -> Set.of("RestController", "Controller").contains(a.getName().getIdentifier()));
    }

    /** The path of a class-level mapping annotation, or null when the class carries none. */
    String classMapping(TypeInfo t) {
        if (!(t.node instanceof NodeWithAnnotations<?> na)) return null;
        for (AnnotationExpr a : na.getAnnotations())
            if (MAPPINGS.containsKey(a.getName().getIdentifier())) return firstString(a, t, "path", "value");
        return null;
    }

    String requestMethod(AnnotationExpr a) {
        if (a instanceof NormalAnnotationExpr na) {
            for (MemberValuePair p : na.getPairs()) {
                if (!p.getNameAsString().equals("method")) continue;
                Expression v = p.getValue();
                if (v instanceof ArrayInitializerExpr arr && !arr.getValues().isEmpty()) v = arr.getValues().get(0);
                String s = v.toString();
                return s.substring(s.lastIndexOf('.') + 1);
            }
        }
        return "ANY";
    }

    /** The first string an annotation carries for one of `names` — or as its single value. */
    String firstString(AnnotationExpr a, TypeInfo t, String... names) {
        if (a instanceof SingleMemberAnnotationExpr s) return stringOf(s.getMemberValue(), t);
        if (a instanceof NormalAnnotationExpr na) {
            for (String name : names)
                for (MemberValuePair p : na.getPairs())
                    if (p.getNameAsString().equals(name)) return stringOf(p.getValue(), t);
        }
        return "";
    }

    String stringOf(Expression v, TypeInfo t) {
        if (v instanceof ArrayInitializerExpr arr) return arr.getValues().isEmpty() ? "" : stringOf(arr.getValues().get(0), t);
        if (v instanceof StringLiteralExpr s) return s.getValue();
        if (v instanceof TextBlockLiteralExpr tb) return tb.getValue();
        if (v instanceof BinaryExpr b && b.getOperator() == BinaryExpr.Operator.PLUS)
            return stringOf(b.getLeft(), t) + stringOf(b.getRight(), t);
        if (v instanceof EnclosedExpr en) return stringOf(en.getInner(), t);
        if (v instanceof NameExpr || v instanceof FieldAccessExpr) {
            // A constant: `@GetMapping(PATH)` or `@GetMapping(Paths.OWNERS)`, folded when it is ours.
            String name = v instanceof NameExpr ne ? ne.getNameAsString() : ((FieldAccessExpr) v).getNameAsString();
            String owner = v instanceof FieldAccessExpr fa ? fa.getScope().toString() : null;
            List<TypeInfo> look = new ArrayList<>();
            if (owner == null) for (TypeInfo o = t; o != null; o = o.outer) look.add(o);
            else {
                String q = lookupSimple(owner.substring(owner.lastIndexOf('.') + 1), t.cu, t);
                if (q != null) look.add(types.get(q));
            }
            for (TypeInfo o : look) {
                if (!(o.node instanceof TypeDeclaration<?> td)) continue;
                for (FieldDeclaration f : td.getFields())
                    for (VariableDeclarator vd : f.getVariables())
                        if (vd.getNameAsString().equals(name) && vd.getInitializer().isPresent())
                            return stringOf(vd.getInitializer().get(), o);
            }
        }
        return "";
    }

    Map<String, Object> entry(String kind, String verb, String path, Method handler) {
        List<String> flow = flow(handler.key);
        Set<String> inFlow = new HashSet<>(flow);
        int total = 0;
        List<Map<String, Object>> items = new ArrayList<>();
        List<Map<String, Object>> lost = new ArrayList<>();
        for (String k : flow) {
            Method m = methods.get(k);
            total += m.cc;
            Map<String, Object> it = new LinkedHashMap<>();
            it.put("method", k);
            it.put("display", m.display);
            it.put("cognitive", m.cc);
            it.put("cyclomatic", m.cyc);
            it.put("calls", m.callees.stream().filter(inFlow::contains).collect(Collectors.toList()));
            it.put("hits", m.hits);
            it.put("file", m.file);
            it.put("line", m.line);
            // The span, so `critical-flows.py` can tell whether a diff hunk lands inside it.
            it.put("endLine", m.endLine);
            items.add(it);
            for (Map<String, Object> u : m.unresolved) {
                Map<String, Object> c = new LinkedHashMap<>();
                c.put("from", k);
                c.putAll(u);
                lost.add(c);
            }
        }
        Map<String, Object> e = new LinkedHashMap<>();
        e.put("kind", kind);
        e.put("httpMethod", verb);
        e.put("path", path);
        e.put("handler", handler.display);
        e.put("metric", "cognitive");
        e.put("flowCc", total);
        e.put("methods", flow.size());
        e.put("flow", items);
        e.put("engine", "javaparser");
        e.put("unresolvedCount", lost.size());
        e.put("unresolved", lost);
        return e;
    }

    List<String> flow(String key) {
        List<String> order = new ArrayList<>();
        Set<String> seen = new HashSet<>(List.of(key));
        Deque<String> queue = new ArrayDeque<>(List.of(key));
        while (!queue.isEmpty()) {
            String cur = queue.poll();
            Method m = methods.get(cur);
            if (m == null) continue;
            order.add(cur);
            for (String t : m.callees) if (seen.add(t)) queue.add(t);
        }
        return order;
    }

    // ── JSON ─────────────────────────────────────────────────────────────────────────────

    static void json(Object o, StringBuilder sb) {
        if (o == null) sb.append("null");
        else if (o instanceof String s) str(s, sb);
        else if (o instanceof Number || o instanceof Boolean) sb.append(o);
        else if (o instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                str(String.valueOf(e.getKey()), sb);
                sb.append(':');
                json(e.getValue(), sb);
            }
            sb.append('}');
        } else if (o instanceof Collection<?> c) {
            sb.append('[');
            boolean first = true;
            for (Object x : c) {
                if (!first) sb.append(',');
                first = false;
                json(x, sb);
            }
            sb.append(']');
        } else str(o.toString(), sb);
    }

    static void str(String s, StringBuilder sb) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        sb.append('"');
    }

    public static void main(String[] args) throws IOException {
        Path root = null, out = null;
        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("--root")) root = Paths.get(args[++i]);
            else if (args[i].equals("--out")) out = Paths.get(args[++i]);
        }
        if (root == null) {
            System.err.println("usage: ComplexityEngine --root DIR [--out FILE]");
            System.exit(2);
        }
        ComplexityEngine e = new ComplexityEngine(root);
        e.parse();
        if (!e.parseErrors.isEmpty()) {
            // A file the parser refuses is a set of entry points that would silently vanish.
            // Refuse the whole snapshot instead: the wrapper falls back to the regex engine.
            for (String p : e.parseErrors) System.err.println("[complexity-engine] parse error: " + p);
            System.exit(3);
        }
        e.index();
        e.assignKeys();
        e.score();
        e.resolveAll();
        e.findEntries();
        StringBuilder sb = new StringBuilder();
        json(e.entries, sb);
        if (out != null) Files.writeString(out, sb.toString(), StandardCharsets.UTF_8);
        else System.out.println(sb);
        int lost = e.entries.stream().mapToInt(x -> (Integer) x.get("unresolvedCount")).sum();
        System.err.printf("[complexity-engine] %d files, %d methods, %d entry points, %d calls not followed%n",
                e.cus.size(), e.methods.size(), e.entries.size(), lost);
    }
}
