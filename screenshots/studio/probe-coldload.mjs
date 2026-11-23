// Throwaway cold-load check: does a FRESH browser session (no cache) see the
// FileX fallback banner for the docx preview, or the real frame? Answers the
// operator's live screenshot without their browser cache in the way.
import fs from 'node:fs';
import process from 'node:process';
import { chromium } from 'playwright-core';

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
const base = `https://filex.${values.ROOT_DOMAIN}`;
const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
const page = await (await browser.newContext({
  ignoreHTTPSErrors: true, viewport: { width: 1600, height: 1000 },
})).newPage();
const bad = [];
page.on('response', (res) => {
  if (res.status() >= 400 && /office\./.test(res.url())) bad.push(`${res.status()} ${new URL(res.url()).pathname}`);
});
try {
  await page.goto(base + '/admin/login', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="email"]').waitFor({ timeout: 20000 });
  await page.locator('input[name="email"]').fill(values.DOKPLOY_ADMIN_EMAIL);
  await page.locator('input[name="password"]').fill(values.DOKPLOY_ADMIN_PASSWORD);
  await page.locator('button').filter({ hasText: /sign in|log ?in/i }).first().click();
  await page.waitForURL((u) => !u.pathname.startsWith('/admin/login'), { timeout: 20000 });
  await page.goto(base + '/admin/home', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  for (let i = 0; i < 8; i++) {
    const tour = page.locator('.fe-tour');
    if ((await tour.count()) === 0 || !(await tour.first().isVisible().catch(() => false))) break;
    const btn = tour.first().locator('button').filter({ hasText: /skip|finish|got it|done|next|close/i }).first();
    if (await btn.isVisible().catch(() => false)) await btn.click().catch(() => {});
    else await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(500);
  }
  await page.click('text="My files"');
  await page.waitForTimeout(2000);
  await page.locator('span.fe-list__name:has-text(".docx")').first().dblclick();
  await page.waitForTimeout(9000);
  const banners = await page.locator('text=/did not load|reverse proxy/i').allTextContents().catch(() => []);
  console.log('BANNERS:', JSON.stringify(banners.map((b) => b.slice(0, 120))));
  const frames = page.frames().map((f) => f.url()).filter((u) => u.includes('office'));
  console.log('OFFICE FRAMES:', JSON.stringify(frames));
  const editorFrame = page.frames().find((f) => f.url().includes('documenteditor'));
  if (editorFrame) {
    const state = await editorFrame.evaluate(() => ({
      canvas: document.querySelectorAll('canvas').length,
      asc: typeof window.Asc !== 'undefined',
    })).catch((e) => ({ err: String(e).slice(0, 120) }));
    console.log('EDITOR FRAME STATE:', JSON.stringify(state));
  }
  await page.screenshot({ path: '/repo/screenshots/raw/coldload-check.png' });
  console.log('OFFICE 4xx:', JSON.stringify(bad));
} finally {
  await browser.close();
}
