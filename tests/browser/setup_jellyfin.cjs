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
  // Use the exact URL shown by the dashboard, without printing its secret path.
  const manifestUrl = (await page.locator('.install .irow .murl').innerText()).trim();
  assert.match(manifestUrl, /^https?:\/\/.*\/manifest\.json$/);
  const manifest = await fetch(manifestUrl);
  assert.equal(manifest.status, 200, 'displayed manifest must be reachable');
  assert.ok((await manifest.json()).id, 'manifest must identify an addon');
  const streamUrl = manifestUrl.replace(/manifest\.json$/, 'stream/movie/tt0133093.json');
  let streams = [];
  for (let attempt = 0; attempt < 12 && !streams.length; attempt++) {
   const response = await fetch(streamUrl);
   assert.equal(response.status, 200, 'stream endpoint must be reachable');
   streams = (await response.json()).streams || [];
   if (!streams.length) await new Promise(resolve => setTimeout(resolve, 5000));
  }
  assert.ok(streams.length, 'fixture movie must return a stream');
  const media = await fetch(streams[0].url, {headers:{Range:'bytes=0-65535'}});
  assert.equal(media.status, 206, 'returned stream must support byte ranges');
  assert.equal((await media.arrayBuffer()).byteLength, 65536);
  console.log(`Enrollment, failed check and retry, apply, displayed manifest, ${streams.length} streams and 65536 media bytes passed`);
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1)});
