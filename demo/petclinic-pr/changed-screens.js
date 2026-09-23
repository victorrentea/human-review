// Which screens did this branch actually change?
//
// The film has one rule it must not break: never leave a changed screen unshown. A hand-written
// list of screens satisfies that rule exactly once — the next person to put a field on a fourth
// form gets a film that confidently tours the other three, under the one heading a reviewer
// trusts without reading. So the list is derived from the diff, every run.
//
// The derivation is three joins, all against the working tree rather than against memory:
//
//   1. what changed   — `git diff <merge-base>..HEAD` over the Angular source, minus the specs
//                       (a changed test is not a changed screen)
//   2. what is routed — every `Routes` array in every *-routing.module.ts, children flattened
//                       into full paths, so `pets/:id` + `visits/add` is one screen
//   3. what contains what — which components each template embeds, by selector
//
// Join 3 is the one that earns its keep. `visit-list` is not routed and never will be: it is a
// table embedded in `pet-list`, which is embedded in `owner-detail`, which IS routed. Adding a
// column to it changes a screen whose own files the diff never touches. Walking containment
// upward to the nearest routed ancestor is what turns "a component changed" into "a screen a
// reviewer can be shown".
//
// Parsed with the TypeScript compiler, not with regexes over source text. The recorder already
// puts petclinic-test/node_modules on NODE_PATH, so it costs nothing, and it is the difference
// between surviving a reformat and quietly returning an empty screen list on one.

const {execFileSync} = require("child_process");
const fs = require("fs");
const path = require("path");
const ts = require("typescript");

const APP_DIR = "petclinic-frontend/src/app";
// A .spec.ts is not a screen, and neither is the generated API surface: both change constantly
// for reasons that put nothing new in front of a reviewer.
const SOURCE_OF_A_SCREEN = /\.component\.(ts|html|css|scss)$/;
const NOT_A_SCREEN = /\.spec\.ts$/;

const git = (root, args) =>
  execFileSync("git", args, {cwd: root, encoding: "utf8"}).trim();

/** The commit this branch is being reviewed against — the same one the guide diffs. */
function mergeBase(root, baseRef) {
  for (const ref of [baseRef, "origin/" + baseRef, "main", "origin/main", "master"]) {
    if (!ref) continue;
    try {
      return git(root, ["merge-base", ref, "HEAD"]);
    } catch (e) { /* not a ref here; try the next */ }
  }
  throw new Error("no base branch to diff against (tried " + baseRef + ", main, master)");
}

/** Every .ts under the app, so the parse sees components the diff did not touch. */
function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (entry.name.endsWith(".ts") && !entry.name.endsWith(".spec.ts")) out.push(full);
  }
  return out;
}

const parse = (file) =>
  ts.createSourceFile(file, fs.readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);

const literal = (node) =>
  node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
    ? node.text : null;

const prop = (obj, name) => {
  for (const p of obj.properties) {
    if (ts.isPropertyAssignment(p) && p.name && p.name.getText() === name) return p.initializer;
  }
  return null;
};

/** class name -> {selector, template absolute path} for every @Component in the app. */
function readComponents(files) {
  const byClass = new Map();
  for (const file of files) {
    const src = parse(file);
    src.forEachChild((node) => {
      if (!ts.isClassDeclaration(node) || !node.name) return;
      const decorators = ts.getDecorators ? ts.getDecorators(node) : node.decorators;
      for (const dec of decorators || []) {
        if (!ts.isCallExpression(dec.expression)) continue;
        if (dec.expression.expression.getText() !== "Component") continue;
        const arg = dec.expression.arguments[0];
        if (!arg || !ts.isObjectLiteralExpression(arg)) continue;
        const templateUrl = literal(prop(arg, "templateUrl"));
        // An @Input is what a PARENT passes down. This router is built with plain
        // `RouterModule.forRoot(routes, {})` — no `withComponentInputBinding()` — so nothing
        // in a URL can ever fill one. A component that needs one is therefore not a screen a
        // reviewer can be sent to, however real its entry in the routing module looks.
        const inputs = [];
        for (const member of node.members) {
          const decs = ts.getDecorators ? ts.getDecorators(member) : member.decorators;
          for (const d of decs || []) {
            const call = ts.isCallExpression(d.expression) ? d.expression.expression : null;
            if (call && call.getText() === "Input" && member.name) inputs.push(member.name.getText());
          }
        }
        byClass.set(node.name.text, {
          className: node.name.text,
          file,
          inputs,
          selector: literal(prop(arg, "selector")),
          template: templateUrl ? path.resolve(path.dirname(file), templateUrl) : null,
        });
      }
    });
  }
  return byClass;
}

/** Every routed path in the app, children flattened, as [{route, className}]. */
function readRoutes(files) {
  const routes = [];
  const join = (parent, child) =>
    [parent, child].filter((s) => s !== "" && s != null).join("/");

  const collect = (array, prefix) => {
    for (const element of array.elements) {
      if (!ts.isObjectLiteralExpression(element)) continue;
      const routePath = literal(prop(element, "path"));
      if (routePath == null) continue;
      const full = join(prefix, routePath);
      const component = prop(element, "component");
      if (component && ts.isIdentifier(component)) {
        routes.push({route: full, className: component.text});
      }
      const children = prop(element, "children");
      if (children && ts.isArrayLiteralExpression(children)) collect(children, full);
    }
  };

  for (const file of files.filter((f) => f.endsWith("-routing.module.ts"))) {
    parse(file).forEachChild((node) => {
      if (!ts.isVariableStatement(node)) return;
      for (const decl of node.declarationList.declarations) {
        const typed = decl.type && decl.type.getText() === "Routes";
        if (!typed) continue;
        if (decl.initializer && ts.isArrayLiteralExpression(decl.initializer)) {
          collect(decl.initializer, "");
        }
      }
    });
  }
  return routes;
}

/** child class -> the classes whose template embeds it, by selector. */
function readContainment(components) {
  const bySelector = new Map();
  for (const c of components.values()) if (c.selector) bySelector.set(c.selector, c.className);

  const parents = new Map();
  for (const c of components.values()) {
    if (!c.template || !fs.existsSync(c.template)) continue;
    const html = fs.readFileSync(c.template, "utf8");
    for (const [selector, childClass] of bySelector) {
      if (childClass === c.className) continue;
      // The selector as a real element tag: `<app-visit-list` and nothing that merely
      // contains those characters.
      if (new RegExp("<" + selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "[\\s/>]").test(html)) {
        if (!parents.has(childClass)) parents.set(childClass, new Set());
        parents.get(childClass).add(c.className);
      }
    }
  }
  return parents;
}

/**
 * The screens this branch changed, each with the reason it is in the list.
 *
 * Returns {base, screens:[{route, className, why}], unrouted:[{className, why}]}. A changed
 * component that reaches no routed ancestor is REPORTED, never dropped: "we could not show it"
 * is a fact about the film's coverage and belongs in the run summary, not in a silence.
 */
function changedScreens({root, baseRef = process.env.HUMAN_REVIEW_BASE_REF || "main"} = {}) {
  // Resolved from THIS file rather than from the working directory: the recorder runs node with
  // whatever cwd the caller had, and a screen list silently derived against another repository
  // is the failure this whole module exists to prevent.
  root = root || git(__dirname, ["rev-parse", "--show-toplevel"]);
  const base = mergeBase(root, baseRef);
  const changedFiles = git(root, ["diff", "--name-only", base + "..HEAD", "--", APP_DIR])
    .split("\n")
    .filter((f) => f && SOURCE_OF_A_SCREEN.test(f) && !NOT_A_SCREEN.test(f));

  const files = walk(path.join(root, APP_DIR));
  const components = readComponents(files);
  const routes = readRoutes(files);
  const parents = readContainment(components);

  const routeOf = new Map();
  for (const r of routes) {
    if (!routeOf.has(r.className)) routeOf.set(r.className, r.route);
  }

  // file path -> the component class declared beside it
  const classOfPath = new Map();
  for (const c of components.values()) {
    classOfPath.set(c.file.replace(/\.ts$/, ""), c.className);
    if (c.template) classOfPath.set(c.template.replace(/\.[^.]+$/, ""), c.className);
  }

  const changedClasses = new Set();
  for (const rel of changedFiles) {
    const key = path.join(root, rel).replace(/\.[^.]+$/, "");
    const cls = classOfPath.get(key);
    if (cls) changedClasses.add(cls);
  }

  const screens = new Map();
  const unrouted = [];
  for (const cls of changedClasses) {
    if (routeOf.has(cls)) {
      screens.set(routeOf.get(cls), {route: routeOf.get(cls), className: cls,
        why: cls + " changed"});
      continue;
    }
    // Not routed: climb containment to every routed ancestor. Breadth-first, and cycle-safe,
    // because Angular templates can and do recurse.
    const seen = new Set([cls]);
    const queue = [...(parents.get(cls) || [])];
    let placed = false;
    while (queue.length) {
      const up = queue.shift();
      if (seen.has(up)) continue;
      seen.add(up);
      if (routeOf.has(up)) {
        placed = true;
        const route = routeOf.get(up);
        if (!screens.has(route)) {
          screens.set(route, {route, className: up, why: cls + " changed, and " + up
            + " is the screen that shows it"});
        }
      }
      // Climb PAST a routed ancestor, never stop at it. `pet-list` is routed at /pets AND
      // embedded in the owner detail page: the visit table inside it changed on both, and
      // stopping at the first hit is exactly how one of the two goes unshown.
      queue.push(...(parents.get(up) || []));
    }
    if (!placed) {
      unrouted.push({className: cls,
        why: cls + " changed but is on no routed screen — nothing to film"});
    }
  }

  // A route can be in the list and still be unfilmable. Separating the two here, rather than
  // letting the feature script discover it by filming a blank page, is what turns a silent gap
  // in the coverage into a sentence in the run summary.
  const filmable = [];
  const unreachable = [];
  for (const s of screens.values()) {
    const comp = components.get(s.className);
    const inputs = (comp && comp.inputs) || [];
    if (inputs.length) {
      unreachable.push({...s, reason: s.className + " is driven by @Input(" + inputs.join(", ")
        + "), which no URL can supply — it is only ever a child of another screen"});
    } else {
      filmable.push(s);
    }
  }
  return {base, screens: filmable, unreachable, unrouted};
}

module.exports = {changedScreens};
