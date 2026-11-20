// Shot prep for the office lanes: uploads the fixture .docx through the
// FileX web UI as the deployer — the same login lane capture.mjs proves on
// every run, not an S3 back-door. Flags:
//   --install-env FILE   operator env (NOT --env-file: Node's own bootstrap
//                        claims that name regardless of position — measured:
//                        the container died with `node: <file>: not found`,
//                        exit 9, before main ran)
//   --fixture FILE       the .docx to upload (its basename is the row name)
//   --replace            delete an existing row of the same name first
//   --cull NAME[,NAME]   delete these stale rows too (old fixtures)
//   --snapshot           after upload, mint one Version row through the
//                        file's own Activity pane ("Snapshot current
//                        state") so the office-versions shot has a real row
// Box-measured lanes this uses: the first-run tour (.fe-tour) must be walked
// away; a single row click SELECTS (toolbar gains Delete); the Details
// drawer's Activity tab renders "Snapshot current state".
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { chromium } from 'playwright-core';

const argValue = (name) => {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
};
const hasFlag = (name) => process.argv.includes(name);
const envFile = argValue('--install-env');
const fixture = argValue('--fixture');
if (!envFile || !fixture) throw new Error('need --install-env and --fixture');
const rowName = path.basename(fixture);
const cull = (argValue('--cull') ?? '').split(',').map((s) => s.trim()).filter(Boolean);

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

  // Row removal lane (box-measured with probe-delete): the toolbar buttons
  // on a selection are unlabeled icons — the honest lane is the row's own
  // "More actions" kebab, its "Delete" menu item, and the confirm dialog's
  // "Move to Trash" button ("Delete? 1 item will be moved to trash."). The
  // row then leaves My files (it lands in Trash, which no shot shows).
  const removeRow = async (name) => {
    const row = page.locator(`span.fe-list__name:has-text("${name}")`).first();
    if ((await row.count()) === 0 || !(await row.isVisible().catch(() => false))) return false;
    const rowBox = page.locator('div.fe-list__row').filter({ hasText: name }).first();
    await row.click();
    await page.waitForTimeout(1000);
    // The row kebab (aria-label="More actions"; measured on div.fe-list__row
    // .is-clickable) only arms on hover — a direct click on the un-hovered
    // row timed out, the hovered one lands.
    await rowBox.hover();
    await page.waitForTimeout(400);
    await rowBox.locator('button[aria-label="More actions"]').first().click();
    await page.waitForTimeout(800);
    const menuDelete = page.locator('[role=menuitem], .el-dropdown-menu__item, li, button')
      .filter({ hasText: /^delete$/i }).first();
    if (await menuDelete.isVisible().catch(() => false)) {
      await menuDelete.click();
    } else {
      // Measured fallback: the open menu's Delete item answers to a plain
      // text click (probe-delete reached the dialog this way).
      await page.click('text=/^delete$/i');
    }
    await page.waitForTimeout(800);
    await page.locator('[role=dialog] button, .el-message-box button')
      .filter({ hasText: /move to trash/i }).first().click();
    await page.waitForFunction(
      (n) => !Array.from(document.querySelectorAll('span.fe-list__name'))
        .some((e) => e.textContent?.includes(n)),
      name,
      { timeout: 15000 },
    );
    return true;
  };
  for (const stale of cull) {
    console.log(await removeRow(stale) ? `CULLED ${stale}` : `NO-ROW ${stale}`);
  }
  if (await removeRow(rowName)) console.log(`REPLACED ${rowName}`);

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
  await page.waitForSelector(`text=/${rowName.replace('.', '\\.')}/`, { timeout: 30000 });
  console.log('UPLOADED');

  if (hasFlag('--snapshot')) {
    // The upload drops a progress toast that intercepts pointer events for a
    // moment (measured: the first snapshot pass timed out its row click);
    // dismiss it, then drive the same Details→Activity lane the
    // office-versions shot uses.
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(3000);
    // The same Details→Activity lane the office-versions shot drives, then
    // the pane's own snapshot control (box-measured label). This is the
    // honest way to mint a version row headlessly: ONLYOFFICE saves never
    // land without focus, the snapshot button does.
    await page.click(`span.fe-list__name:has-text("${rowName}")`);
    await page.waitForTimeout(1500);
    await page.locator('[aria-label="Details"], [title="Details"]').first().click();
    await page.waitForTimeout(1500);
    await page.click('text="Activity"');
    await page.waitForTimeout(2000);
    await page
      .locator('button')
      .filter({ hasText: /snapshot/i })
      .first()
      .click();
    await page.waitForTimeout(3000);
    console.log('SNAPSHOTTED');
  }
  await page.screenshot({ path: '/repo/screenshots/raw/upload-prep.png' });
} finally {
  await browser.close();
}
