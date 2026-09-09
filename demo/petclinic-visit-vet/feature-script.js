// What this branch added, filmed: a visit now records the vet that attended it.
//
// The film's rule is "never leave a changed screen unshown", and the only way to keep that rule
// true next time is to stop writing the list of screens down. `changed-screens.js` derives it
// from the diff every run; everything below is driven by whatever it returns.
//
// Two consequences worth knowing before editing this file:
//
//   * A screen with no handler here is still filmed, by `passThrough` — a generic beat that
//     navigates, spotlights the heading and says the screen changed. So the branch that adds a
//     fifth screen gets it in the film without anyone remembering to come back here. Handlers
//     are how a screen gets a BETTER beat, never how it gets one at all.
//   * `ORDER` is narrative only. Screens named in it are filmed in that order; anything the
//     derivation found that is not named is appended, never dropped.
//
// Only one screen earns a workflow: the booking form, which is where the feature actually
// happens. Everything else gets a beat and a caption, because a screen that gained a column
// has nothing to demonstrate — passing through it IS the evidence.

const {changedScreens} = require("./changed-screens.js");

// Narrative order: enter where a reviewer enters, book, then tour what else moved.
const ORDER = ["owners/:id", "pets/:id/visits/add", "visits", "visits/:id/edit"];

module.exports = async ({page, say, pause, get, app, apiUrl}) => {
  const {screens, unreachable, unrouted} = changedScreens();

  // A pet that already HAS visits, so the column the branch added has something in it. Picking
  // the first owner with a pet is how you film an empty table against a freshly seeded database.
  const owners = await (await get(`${apiUrl}/api/owners`)).json();
  const owner = owners.find((o) => (o.pets || []).some((p) => (p.visits || []).length));
  if (!owner) throw new Error("no owner with a visited pet in the database");
  const pet = owner.pets.find((p) => (p.visits || []).length);

  // Route parameters resolve from the collection segment in front of them — `pets/:id` is a pet,
  // `visits/:id` a visit — rather than from a table of route strings, which would be the same
  // hand-maintained list one level down.
  const bag = {owners: () => owner.id, pets: () => pet.id, visits: () => bookedVisitId};
  let bookedVisitId = null;
  const fill = (route) => {
    const segs = route.split("/");
    return segs.map((seg, i) => {
      if (!seg.startsWith(":")) return seg;
      const source = bag[segs[i - 1]];
      const value = source && source();
      if (value == null) throw new Error(`cannot fill ${seg} of ${route}`);
      return value;
    }).join("/");
  };

  const heading = () => page.locator("h2").first();
  const visited = [];
  const missed = [];

  // The fallback. It is deliberately dull and deliberately honest: it names the screen and shows
  // it, which is all "pass through" was ever supposed to mean.
  const passThrough = async (screen) => {
    await page.goto(`${app}/${fill(screen.route)}`);
    await heading().waitFor();
    await say(`This screen changed on this branch too.`, heading());
    await pause(1800);
  };

  const handlers = {
    "owners/:id": async (screen) => {
      await page.goto(`${app}/${fill(screen.route)}`);
      const visitList = page.locator("app-pet-list app-visit-list").first();
      await visitList.waitFor();
      await say("Every pet’s visit list now carries a Vet column.", visitList);
      await pause(2200);
    },

    "pets/:id/visits/add": async (screen) => {
      await page.goto(`${app}/${fill(screen.route)}`);
      await page.locator('h2:has-text("New Visit")').waitFor({state: "visible"});
      await page.locator('input[name="date"]').fill("2026-05-12");
      const description = `Human review demo ${Date.now()}`;
      await page.locator("input#description").fill(description);

      const vetSelect = page.locator("select#vetId");
      await say("The booking form asks who will attend — and lets you say nobody yet.", vetSelect);
      await pause(2200);

      const firstVet = vetSelect.locator('option:not([value$="null"]):not([value=""])').first();
      const vetName = (await firstVet.textContent() || "").trim();
      await vetSelect.selectOption({label: vetName});
      await say(`We book this one with ${vetName}.`, vetSelect);
      await pause(1800);

      await page.locator('button[type="submit"]:has-text("Add Visit")').click();
      await page.waitForURL(new RegExp(`/owners/${owner.id}$`));

      const row = page.locator("app-pet-list").first()
        .locator("app-visit-list tr").filter({hasText: description});
      await row.waitFor();
      const shown = (await row.locator(".visit-vet").textContent() || "").trim();
      await say("Back on the owner, the new visit names the vet that will attend it.", row);
      await pause(2200);

      // The visit just booked is the one the edit screen should open: it is the only row on the
      // branch guaranteed to have a vet on it.
      const all = await (await get(`${apiUrl}/api/visits`)).json();
      const mine = all.find((v) => v.description === description);
      if (mine) bookedVisitId = mine.id;
      return {ok: shown === vetName, vetName, shown};
    },

    "visits": async (screen) => {
      await page.goto(`${app}/${fill(screen.route)}`);
      // The column, not the table. Spotlighting the whole grid is a box round the entire frame,
      // which points at nothing — and what changed here is one header and the cells under it.
      const vetColumn = page.locator("#visitsTable th").filter({hasText: /^Vet$/});
      await vetColumn.waitFor();
      await say("The all-visits page carries the same column.", vetColumn);
      await pause(1900);
    },

    "visits/:id/edit": async (screen) => {
      await page.goto(`${app}/${fill(screen.route)}`);
      const combo = page.locator("app-combo").first();
      await combo.waitFor();
      await say("And on the edit form the vet picker is the design-system combo.", combo);
      await pause(2100);
    },
  };

  const ordered = [
    ...ORDER.map((r) => screens.find((s) => s.route === r)).filter(Boolean),
    ...screens.filter((s) => !ORDER.includes(s.route)),
  ];

  let outcome = {ok: true};
  for (const screen of ordered) {
    const handler = handlers[screen.route] || passThrough;
    try {
      const result = await handler(screen);
      visited.push(screen.route);
      if (result && result.ok === false) {
        outcome = {ok: false, shown: result.shown, vetName: result.vetName};
      }
    } catch (e) {
      // A screen that could not be reached is said out loud rather than quietly dropped: a gap
      // in the film's coverage is exactly the thing a reviewer cannot see for themselves.
      missed.push(`${screen.route} (${e.message.split("\n")[0]})`);
    }
  }

  const notes = [`${visited.length}/${ordered.length} changed screens filmed`];
  for (const u of unreachable) notes.push(`not filmable: /${u.route} — ${u.reason}`);
  for (const u of unrouted) notes.push(`not filmable: ${u.why}`);
  if (missed.length) notes.push(`FAILED to reach: ${missed.join("; ")}`);
  if (!outcome.ok) notes.push(`⚠ the list shows "${outcome.shown}" instead of ${outcome.vetName}`);

  return {ok: outcome.ok && missed.length === 0, note: notes.join(" | ")};
};
