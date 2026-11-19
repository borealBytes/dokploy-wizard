// One-time shot prep: upload the office fixture through the FileX web UI as
// the deployer — the same login lane capture.mjs proves on every run. The
// office shots (filex-office-edit / filex-office-versions) need a real .docx
// in My files; this is the honest UI lane, not an S3 back-door.
import fs from 'node:fs';
import process from 'node:process';
import { chromium } from 'playwright-core';

const argValue = (name) => {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
};
// Flag spelled --install-env, NOT --env-file: Node's own bootstrap claims
// `--env-file` regardless of position (measured: the container died with
// `node: /host/.install-min.env: not found`, exit 9, before main ran).
const envFile = argValue('--install-env');
const fixture = argValue('--fixture');
if (!envFile || !fixture) throw new Error('need --install-env and --fixture');

const values = {};
for (const line of fs.readFileSync(envFile, 'utf8').split(/\r?\n/)) {
  const t = line.trim();
  if (!t || t.startsWith('#')) continue;
  const i = t.indexOf('=');
  if (i > 0) values[t.slice(0, i)] = t.slice(i + 1);
}
const base = `https://filex.${values.ROOT_DOMAIN}`;

const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
const context = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1440, height: 900 },
});
const page = await context.newPage();
try {
  // Mirror capture.mjs's filex login lane exactly (box-measured: the console
  // lives under /admin/; /login 404s).
  await page.goto(base + '/admin/login', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="email"]').waitFor({ timeout: 20000 });
  await page.locator('input[name="email"]').fill(values.DOKPLOY_ADMIN_EMAIL);
  await page.locator('input[name="password"]').fill(values.DOKPLOY_ADMIN_PASSWORD);
  await page.locator('button').filter({ hasText: /sign in|log ?in/i }).first().click();
  await page.waitForURL((url) => !url.pathname.startsWith('/admin/login'), { timeout: 20000 });

  await page.goto(base + '/admin/home', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  // Box-measured: a fresh FileX account gets a first-run onboarding tour
  // (role=dialog, aria-modal, class fe-tour) that intercepts every pointer
  // event until dismissed — and the studio's per-shot clean context means it
  // would block EVERY filex shot, not just this upload. Walk the tour to its
  // end through its own controls (Skip/Finish/Got it, and Next while any
  // remain); Escape only as the last resort.
  for (let i = 0; i < 8; i++) {
    const tour = page.locator('.fe-tour');
    if ((await tour.count()) === 0 || !(await tour.first().isVisible().catch(() => false))) break;
    const btn = tour
      .first()
      .locator('button')
      .filter({ hasText: /skip|finish|got it|done|next|close|跳过|完成|下一步/i })
      .first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click().catch(() => {});
    } else {
      await page.keyboard.press('Escape').catch(() => {});
    }
    await page.waitForTimeout(600);
  }
  await page.waitForTimeout(800);
  await page.click('text="My files"');
  await page.waitForTimeout(2000);

  // If the fixture is already there (a prior attempt), that is success.
  const already = await page.locator('text=/office-seed\\.docx/').count();
  if (already > 0) {
    console.log('ALREADY-PRESENT');
  } else {
    // Usual web-UI shapes: a hidden <input type=file>, or an upload button
    // that opens the OS chooser (Playwright's filechooser event).
    const chooser = page.waitForEvent('filechooser', { timeout: 8000 }).catch(() => null);
    const upload = page
      .locator('button, a, [role=button], span')
      .filter({ hasText: /upload|上传/i })
      .first();
    await upload.click({ timeout: 8000 }).catch(() => {});
    const fc = await chooser;
    if (fc) {
      await fc.setFiles(fixture);
    } else {
      await page.locator('input[type=file]').first().setInputFiles(fixture, { timeout: 8000 });
    }
    await page.waitForSelector('text=/office-seed\\.docx/', { timeout: 30000 });
    console.log('UPLOADED');
  }
  await page.screenshot({ path: '/repo/screenshots/raw/upload-prep.png' });
} finally {
  await browser.close();
}
