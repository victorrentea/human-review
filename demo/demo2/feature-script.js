// Film of the server-side paginated, sortable Visits grid at /visits.
module.exports = async ({page, say, pause, app}) => {
  const rows = page.locator("#visitsTable tbody tr");
  const firstCell = (cls) => page.locator(`#visitsTable tbody tr:first-child td.${cls}`);
  const header = (name) => page.locator("#visitsTable th.mat-sort-header").filter({hasText: new RegExp(`^\\s*${name}\\s*$`)});
  const paginator = page.locator("mat-paginator");
  const range = page.locator("mat-paginator .mat-mdc-paginator-range-label, mat-paginator .mat-paginator-range-label");
  const missed = [];
  const step = async (name, fn) => {
    try { await fn(); } catch (e) { missed.push(`${name} (${e.message.split("\n")[0]})`); }
  };
  const urlHas = (re) => page.waitForURL(re, {timeout: 10000});

  await step("/visits first page", async () => {
    await page.goto(`${app}/visits`);
    await rows.first().waitFor();
    await range.filter({hasText: /^\s*1 – 10 of \d+/}).waitFor();
    await say("The visits grid shows the newest ten visits, sorted by date.", page.locator("#visitsTable"));
    await pause(1500);
  });

  await step("sort by Pet", async () => {
    const before = await firstCell("visit-pet").innerText();
    await header("Pet").click();
    await urlHas(/sort=pet(,|%2C)asc/);
    await page.waitForFunction((b) =>
      document.querySelector("#visitsTable tbody tr td.visit-pet")?.textContent.trim() !== b, before.trim());
    await say("Click Pet to sort by pet. The sort is in the URL.", header("Pet"));
    await pause(1500);
  });

  await step("sort by Owner", async () => {
    await header("Owner").click();
    await urlHas(/sort=owner(,|%2C)asc/);
    await rows.first().waitFor();
    const asc = (await firstCell("visit-owner").innerText()).trim();
    await header("Owner").click();
    await urlHas(/sort=owner(,|%2C)desc/);
    await page.waitForFunction((b) =>
      document.querySelector("#visitsTable tbody tr td.visit-owner")?.textContent.trim() !== b, asc);
    await say("Owner sorts ascending, then descending, on the server.", header("Owner"));
    await pause(1500);
  });

  await step("5 per page", async () => {
    await paginator.locator("mat-select").click();
    await page.getByRole("option", {name: "5", exact: true}).click();
    await range.filter({hasText: /^\s*1 – 5 of \d+/}).waitFor();
    await say("Choose five items per page.", paginator);
    await pause(1500);
  });

  await step("next page", async () => {
    await page.getByRole("button", {name: "Next page"}).click();
    await urlHas(/page=2/);
    await range.filter({hasText: /^\s*6 – 10 of \d+/}).waitFor();
    await say("Next page: page two, also kept in the URL.", paginator);
    await pause(1500);
  });

  await step("browser back", async () => {
    await page.goBack();
    await range.filter({hasText: /^\s*1 – 5 of \d+/}).waitFor();
    await say("Back restores the previous view.", paginator);
    await pause(1500);
  });

  return {
    ok: missed.length === 0,
    note: `${6 - missed.length}/6 beats filmed on /visits` +
      (missed.length ? ` | FAILED to reach: ${missed.join("; ")}` : ""),
  };
};
