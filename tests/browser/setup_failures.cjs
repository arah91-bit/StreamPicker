// Run against a fresh isolated Compose instance: NODE_PATH=<playwright node_modules> node tests/browser/setup_failures.cjs <base-url>
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage({httpCredentials:{username:'fixture-admin',password:'fixture-password-123'}});
  await page.goto(process.argv[2]);
  if (await page.locator('[name=username]').count()) {
  await page.locator('[name=username]').fill('fixture-admin');
  await page.locator('[name=password]').fill('fixture-password-123');
  await page.locator('[name=confirm]').fill('fixture-password-123');
  await page.getByRole('button', {name:'Create account'}).click();
  await page.getByRole('button', {name:'Continue to dashboard'}).click();
  await page.locator('.card[data-card=addon]').waitFor();
  }
  // Network boundaries simulate a partial source failure and rejected restart.
  await page.route('**/api/setup/apply', route => route.fulfill({json:{ok:true,results:{addon:{ok:true},tmdb:{ok:false,detail:'Check the API key'}}}}));
  await page.route('**/api/settings/restart', route => route.fulfill({status:409,json:{detail:'configuration cannot restart: invalid settings'}}));
  const addon = page.locator('.card[data-card=addon]');
  await addon.locator('.swi').check();
  await addon.locator('[data-key="url"]').fill('http://example.test/manifest.json');
  await page.locator('#gobtn').click();
  await page.getByText('Check the API key').waitFor();
  assert.match(await page.locator('#gomsg').innerText(), /retry/i);
  await page.getByRole('button', {name:'Finish →'}).click();
  await page.getByText(/configuration cannot restart/).waitFor({timeout:3000});
  assert.equal(await page.locator('#gobtn').isEnabled(), true);
  console.log('Browser partial failure and rejected restart: passed');
 } finally { await browser.close(); }
})().catch(e=>{ console.error(e); process.exit(1); });
