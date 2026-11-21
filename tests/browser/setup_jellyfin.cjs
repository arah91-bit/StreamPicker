// Fresh isolated Compose instance and test-only Jellyfin fixture required.
// NODE_PATH=<playwright node_modules> node tests/browser/setup_jellyfin.cjs <dashboard-url> <fixture-base>
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({httpCredentials:{username:'fixture-admin',password:'fixture-password-123'}});
  await page.goto(process.argv[2]);
  await page.locator('[name=username]').fill('fixture-admin');
  await page.locator('[name=password]').fill('fixture-password-123');
  await page.locator('[name=confirm]').fill('fixture-password-123');
  await page.getByRole('button',{name:'Create account'}).click();
  await page.getByRole('button',{name:'Continue to dashboard'}).click();
  const card=page.locator('.card[data-card=jellyfin]');
  await card.locator('.swi').check();
  await card.locator('[data-key=JELLYFIN_URL]').fill(process.argv[3]+'/missing');
  await card.locator('[data-key=JELLYFIN_USERNAME]').fill('fixture');
  await card.locator('[data-key=JELLYFIN_PASSWORD]').fill('fixture-password');
  await page.locator('#gobtn').click();
  await page.locator('#checks .bad').waitFor();
  assert.match(await page.locator('#gomsg').innerText(), /try again/i);
  await card.locator('[data-key=JELLYFIN_URL]').fill(process.argv[3]);
  await page.locator('#gobtn').click();
  await page.getByRole('button',{name:'Finish →'}).waitFor();
  assert.match(await page.locator('#checks').innerText(), /✓ Jellyfin library/);
  await page.locator('#gobtn').click();
  await page.getByText('Add to Nuvio/Stremio').waitFor({timeout:60000});
  console.log('Enrollment, source failure and retry, restart and install link passed');
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1)});
