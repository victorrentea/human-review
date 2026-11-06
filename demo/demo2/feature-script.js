module.exports = async ({page, say, pause, app}) => {
  const table = page.locator("#visitsTable");
  const rows = page.locator("#visitsTable tbody tr");
  const paginator = page.locator("mat-paginator");
  const header = (name) => page.locator("#visitsTable th").filter({hasText: new RegExp(`^\\s*${name}\\s*$`)});
  const rowsAre = (n) => page.waitForFunction(
    (c) => document.querySelectorAll("#visitsTable tbody tr").length === c, n, {timeout: 10000});
  const pickSize = async (size) => {
    await page.locator("mat-paginator mat-select").click();
    await page.getByRole("option", {name: String(size), exact: true}).click();
  };

  // Cold open: the payoff is a link that lands on one exact view of the grid.
  await page.goto(`${app}/visits?page=2&size=5&sort=owner,desc`);
  await rows.first().waitFor();
  await rowsAre(5);
  await paginator.filter({hasText: /6\s*.\s*10\s+of/}).waitFor();
  await say("Paste this link anywhere, and you land right here: page two, five rows, sorted by owner.", paginator);
  await pause(800);
  await say("Yesterday, this grid was one endless scroll. So how did we get here?", table);
  await pause(800);

  // The walk, from the plain grid to that view.
  await page.goto(`${app}/visits`);
  await rows.first().waitFor();
  await paginator.waitFor();
  await rowsAre(10);
  await say("It starts with the ten latest visits, one page at a time.", paginator);
  await pause(800);

  await pickSize(5);
  await rowsAre(5);
  await pickSize(20);
  await rowsAre(20);
  await say("Five rows a page, or twenty.", paginator);
  await pause(800);

  await pickSize(5);
  await rowsAre(5);
  const next = page.getByRole("button", {name: "Next page"});
  await next.waitFor();
  await next.click();
  await page.waitForURL(/page=2/);
  await say("Next page, and the page number lands in the URL.", paginator);
  await pause(800);

  const owner = header("Owner");
  await owner.waitFor();
  await owner.click();
  await page.waitForURL(/sort=owner/);
  await say("Every column header sorts. Owner, ascending, back on page one.", owner);
  await pause(800);

  await owner.click();
  await page.waitForURL(/sort=owner(,|%2C)desc/);
  await say("Click again to reverse it.", owner);
  await pause(800);

  const pet = header("Pet");
  await pet.click();
  await page.waitForURL(/sort=pet/);
  await say("Same for Pet.", pet);
  await pause(800);

  await page.reload();
  await rows.first().waitFor();
  await rowsAre(5);
  await header("Pet").waitFor();
  await say("Reload, and nothing moves: the size, the page and the sort all live in that link.", paginator);
  await pause(800);

  return {ok: true, note: "visits grid: cold open on a deep link; page sizes 5/10/20, next page, sort by Owner/Pet, reload keeps URL state"};
};
