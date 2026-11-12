const deriveRoutes = require("./feature-routes.js");

module.exports = async ({page, say, pause, get, app, apiUrl}) => {
  const missed = [], unfillable = [];
  let routes = [];
  try { routes = deriveRoutes(); } catch (e) { missed.push("route derivation: " + e.message); }
  if (!routes.includes("/visits")) routes.unshift("/visits");

  const rows = () => page.locator("#visitsTable tbody tr");
  const firstDate = () => page.locator("#visitsTable .visit-date").first().textContent();

  // Handlers only make a beat better; every other screen gets the default beat.
  const handlers = {
    "/visits": async () => {
      await page.goto(`${app}/visits`);
      const table = page.locator("#visitsTable");
      await table.waitFor();
      const paginator = page.locator("mat-paginator");
      await paginator.waitFor();
      await say("Every visit in the clinic, one page at a time, sortable by any column.", table);
      await say("Before, this grid dumped every visit at once. So how did it get there?");
      await say("The paginator lets you pick five, ten or twenty visits per page.", paginator);
      const first = await firstDate();
      const next = page.locator("button.mat-mdc-paginator-navigation-next");
      await next.waitFor();
      await next.click();
      await page.waitForFunction(d => {
        const c = document.querySelector("#visitsTable .visit-date");
        return c && c.textContent !== d;
      }, first);
      await say("Next page loads the following visits from the server.", rows().first());
      const header = page.locator("th[mat-sort-header='description']");
      await header.waitFor();
      await header.click();
      await page.waitForFunction(() => document.querySelector("th[mat-sort-header='description'][aria-sort]"));
      await say("Clicking a column header sorts the whole list, not just this page.", header);
      await header.click();
      await pause(1000);
      await say("Descending flips the order across every page. That is the grid, paged and sortable.", table);
      await pause(1500);
    },
  };

  const defaultBeat = async route => {
    await page.goto(`${app}${route}`);
    const h = page.locator("h1, h2").first();
    await h.waitFor();
    await say("This screen changed too.", h);
  };

  let visited = 0;
  const total = routes.length;
  for (const route of routes) {
    if (/:/.test(route)) { unfillable.push(route); continue; }
    try {
      await (handlers[route] || (() => defaultBeat(route)))();
      visited++;
    } catch (e) {
      missed.push(route);
    }
  }
  let note = `${visited}/${total} changed screens filmed`;
  if (missed.length) note += ` | FAILED to reach: ${missed.join("; ")}`;
  if (unfillable.length) note += ` | not filmable: ${unfillable.join("; ")}`;
  return {ok: missed.length === 0, note};
};
