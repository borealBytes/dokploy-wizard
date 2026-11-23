// Throwaway probe: reproduce the operator's LibreWolf banner on stock
// Playwright Firefox by setting the prefs LibreWolf ships with, one combo at
// a time. Usage: --prefs 'a=1,b=c,...' (bools as true/false, strings as-is).
import fs from 'node:fs';
import process from 'node:process';
import { firefox } from 'playwright-core';

const argValue = (name) => {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
};
const values = {};
for (const line of fs.readFileSync(argValue('--install-env'), 'utf8').split(/\r?\n/)) {
  const t = line.trim();
  if (!t || t.startsWith('#')) continue;
  const i = t.indexOf('=');
  if (i > 0) values[t.slice(0, i)] = t.slice(i + 1);
}
const prefs = {};
for (const pair of (argValue('--prefs') || '').split(',').filter(Boolean)) {
  const [k, v] = pair.split('=');
  prefs[k.trim()] = v === 'true' ? true : v === 'false' ? false : Number.isNaN(Number(v)) ? v : Number(v);
}
console.log('PREFS:', JSON.stringify(prefs));
const base = `https://filex.${values.ROOT_DOMAIN}`;
const browser = await firefox.launch({ });
const page = await (await browser.newContext({
  ignoreHTTPSErrors: true, viewport: { width: 1600, height: 1000 },
  firefoxUserPrefs: prefs,
})).newPage();
try {
  await page.goto(base + '/admin/login', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="email"]').waitFor({ timeout: 25000 });
  await page.locator('input[name="email"]').fill(values.DOKPLOY_ADMIN_EMAIL);
  await page.locator('input[name="password"]').fill(values.DOKPLOY_ADMIN_PASSWORD);
  await page.locator('button').filter({ hasText: /sign in|log ?in/i }).first().click();
  await page.waitForURL((u) => !u.pathname.startsWith('/admin/login'), { timeout: 25000 });
  await page.goto(base + '/admin/home', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  for (let i = 0; i < 8; i++) {
    const tour = page.locator('.fe-tour');
    if ((await tour.count()) === 0 || !(await tour.first().isVisible().catch(() => false))) break;
    const btn = tour.first().locator('button').filter({ hasText: /skip|finish|got it|done|next|close/i }).first();
    if (await btn.isVisible().catch(() => false)) await btn.click().catch(() => {});
    else await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(500);
  }
  await page.click('text="My files"');
  await page.waitForTimeout(2500);
  await page.locator('span.fe-list__name:has-text(".docx")').first().dblclick();
  await page.waitForTimeout(10000);
  const banners = await page.locator('text=/did not load|reverse proxy/i').allTextContents().catch(() => []);
  console.log('BANNERS:', JSON.stringify(banners.map((b) => b.slice(0, 80))));
  console.log('OFFICE FRAMES:', JSON.stringify(page.frames().map((f) => f.url()).filter((u) => u.includes('office')).map((u) => u.slice(0, 90))));
  await page.screenshot({ path: '/repo/screenshots/raw/probe-fx.png' });
} finally {
  await browser.close();
}
