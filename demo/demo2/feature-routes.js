// Derives the routes of the screens a diff touched: changed components -> routes, climbing template containment.
const ts = require("typescript"), fs = require("fs"), path = require("path"), cp = require("child_process");
const root = cp.execSync("git rev-parse --show-toplevel").toString().trim();
const src = path.join(root, "petclinic-frontend/src/app");
function walk(d) { return fs.readdirSync(d, {withFileTypes: true}).flatMap(e => e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]); }
function changedFiles() {
  let base = "origin/main";
  try { base = JSON.parse(fs.readFileSync(path.join(root, "human-review.json"), "utf8")).base || base; } catch (e) {}
  try { return cp.execSync(`git diff --name-only ${base}`, {cwd: root}).toString().split("\n").filter(Boolean).map(f => path.join(root, f)); } catch (e) { return []; }
}
module.exports = function deriveRoutes() {
  const files = walk(src).filter(f => f.endsWith(".ts") && !f.endsWith(".spec.ts"));
  const comps = {}; // class -> {file, selector, html}
  const routes = []; // {path, comp}
  for (const f of files) {
    const sf = ts.createSourceFile(f, fs.readFileSync(f, "utf8"), ts.ScriptTarget.Latest, true);
    const visit = n => {
      if (ts.isClassDeclaration(n) && n.name) {
        const dec = (ts.canHaveDecorators(n) ? ts.getDecorators(n) : []) || [];
        for (const d of dec) if (ts.isCallExpression(d.expression) && d.expression.expression.getText() === "Component") {
          const o = d.expression.arguments[0]; const c = {file: f, selector: null, html: ""};
          for (const p of o ? o.properties : []) {
            const k = p.name && p.name.getText(), v = p.initializer;
            if (k === "selector" && ts.isStringLiteralLike(v)) c.selector = v.text;
            if (k === "template" && ts.isStringLiteralLike(v)) c.html += v.text;
            if (k === "templateUrl" && ts.isStringLiteralLike(v)) {
              try { c.html += fs.readFileSync(path.join(path.dirname(f), v.text), "utf8"); c.htmlFile = path.join(path.dirname(f), v.text); } catch (e) {}
            }
          }
          comps[n.name.text] = c;
        }
      }
      if (ts.isObjectLiteralExpression(n)) {
        const pr = n.properties.find(p => p.name && p.name.getText() === "path");
        const cr = n.properties.find(p => p.name && p.name.getText() === "component");
        if (pr && cr && ts.isStringLiteralLike(pr.initializer)) routes.push({path: pr.initializer.text, comp: cr.initializer.getText()});
      }
      ts.forEachChild(n, visit);
    };
    visit(sf);
  }
  const changed = new Set(changedFiles());
  const hit = new Set(Object.keys(comps).filter(k => changed.has(comps[k].file) || (comps[k].htmlFile && changed.has(comps[k].htmlFile))));
  for (let grew = true; grew;) { // climb containment, past routed ancestors
    grew = false;
    for (const [k, c] of Object.entries(comps)) {
      if (hit.has(k)) continue;
      if ([...hit].some(h => comps[h].selector && c.html.includes(`<${comps[h].selector}`))) { hit.add(k); grew = true; }
    }
  }
  return routes.filter(r => hit.has(r.comp)).map(r => "/" + r.path)
    .filter((p, i, a) => a.indexOf(p) === i);
};
