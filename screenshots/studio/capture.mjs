// dokploy-wizard screenshot studio — capture side.
//
// Drives the LIVE box URLs with Playwright Chromium, performs each shot's
// scripted steps, then scans the rendered DOM for every redaction-pattern
// occurrence and records character-precise rectangles (Range.getClientRects)
// into a per-shot meta file. The blur happens later (redact.mjs) from that
// meta, deterministically and without a browser, so redaction can be
// re-tuned and re-run over raw captures forever.
//
// Usage (inside the studio container):
//   node capture.mjs --env-file /repo/.install.env --redact-env /repo/screenshots/redact.env \
//        --shot <id> | --all [--area <area>] [--out /repo/screenshots/raw] [--video]

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { chromium } from 'playwright-core';
import { SHOTS, SERVICE_SUBDOMAINS } from './playbook.cjs';

const args = process.argv.slice(2);
const argValue = (name) => {
  const index = args.indexOf(name);
  return index >= 0 && index + 1 < args.length ? args[index + 1] : undefined;
};
const has = (name) => args.includes(name);

function readEnvFile(file) {
  const values = {};
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const index = trimmed.indexOf('=');
    if (index > 0) values[trimmed.slice(0, index)] = trimmed.slice(index + 1);
  }
  return values;
}

const installEnv = readEnvFile(argValue('--env-file') ?? '.install.env');
const redactEnv = readEnvFile(argValue('--redact-env') ?? 'redact.env');
const outDir = argValue('--out') ?? 'raw';
const rootDomain = installEnv.ROOT_DOMAIN ?? redactEnv.REDACT_ROOT_DOMAIN;
if (!rootDomain) throw new Error('ROOT_DOMAIN missing from --env-file (and REDACT_ROOT_DOMAIN absent)');

const baseOverride = {};
for (const shot of args) {
  const match = shot.match(/^--url=([a-z]+)=(https?:\/\/.+)$/);
  if (match) baseOverride[match[1]] = match[2];
}
const baseUrl = (service) =>
  baseOverride[service] ?? `https://${SERVICE_SUBDOMAINS[service]}.${rootDomain}`;

// Redaction patterns: real identity strings that must never survive into a
// committed PNG. Root domain matches are the point at which the SUB-part of
// a hostname stays visible: only the root label sequence gets a rectangle.
const patterns = [];
const add = (raw, kind) => {
  for (const value of String(raw ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)) {
    patterns.push({ kind, text: value });
  }
};
add(redactEnv.REDACT_ROOT_DOMAIN, 'root-domain');
add(redactEnv.REDACT_EMAIL, 'email');
add(redactEnv.REDACT_LOGIN, 'login');
add(redactEnv.REDACT_ORG, 'org');
add(redactEnv.REDACT_TAILNET_LABELS, 'tailnet');
add(redactEnv.REDACT_BOX_IP, 'ip');
const keep = new Set(
  String(redactEnv.REDACT_KEEP ?? 'dokploy-wizard,wizard-provisioner')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean),
);
const escapeRe = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const combined = new RegExp(
  patterns
    .map((pattern) => pattern.text)
    .sort((a, b) => b.length - a.length)
    .map((text) => escapeRe(text))
    .join('|'),
  'gi',
);

const loginFunctions = {
  dokploy: async (page, credentials) => {
    await page.goto(baseUrl('dokploy') + '/', { waitUntil: 'domcontentloaded' });
    await page.getByLabel(/email/i).first().fill(credentials.email);
    await page.getByLabel(/password/i).first().fill(credentials.password);
    await page.locator('button[type=submit]').first().click();
  },
  coder: async (page, credentials) => {
    await page.goto(baseUrl('coder') + '/login', { waitUntil: 'domcontentloaded' });
    await page.locator('input[name="email"]').fill(credentials.email);
    await page.locator('input[name="password"]').fill(credentials.password);
    await page.locator('button[type=submit]').first().click();
  },
  gitea: async (page, credentials) => {
    await page.goto(baseUrl('gitea') + '/user/login', { waitUntil: 'domcontentloaded' });
    await page.locator('#username').fill(credentials.login ?? credentials.email);
    await page.locator('#password').fill(credentials.password);
    await page.locator('button[type=submit]').first().click();
  },
  filex: async (page, credentials) => {
    await page.goto(baseUrl('filex') + '/login', { waitUntil: 'domcontentloaded' });
    await page.locator('input[type=email], input[name*=mail i]').first().fill(credentials.email);
    await page.locator('input[type=password]').first().fill(credentials.password);
    await page.locator('button[type=submit]').first().click();
  },
  litellm: async (page, credentials) => {
    await page.goto(baseUrl('litellm') + '/ui/login', { waitUntil: 'domcontentloaded' });
    await page.locator('#username').fill(credentials.login ?? credentials.email);
    await page.locator('#password').fill(credentials.password);
    await page.locator('button').filter({ hasText: /login/i }).first().click();
  },
  docuseal: async (page, credentials) => {
    await page.goto(baseUrl('docuseal') + '/account/sign_in', { waitUntil: 'domcontentloaded' });
    await page.locator('input[name="user[email]"]').fill(credentials.email);
    await page.locator('input[name="user[password]"]').fill(credentials.password);
    await page.locator('button[type=submit]').first().click();
  },
};

// Character-precise rectangles: every regex hit inside a text node becomes
// one (or more, for wrapped nodes) client rectangles converted to PAGE
// coordinates (fullPage screenshots use page coordinates).
async function collectRedactionRects(page) {
  return page.evaluate(
    ({ source, flags }) => {
      const regex = new RegExp(source, flags);
      const rects = [];
      const walk = (root) => {
        for (const node of root.childNodes ?? []) {
          if (node.nodeType === Node.TEXT_NODE) {
            const text = node.nodeValue ?? '';
            regex.lastIndex = 0;
            let match;
            while ((match = regex.exec(text)) !== null) {
              if (match[0].length === 0) continue;
              const range = document.createRange();
              range.setStart(node, match.index);
              range.setEnd(node, match.index + match[0].length);
              for (const rect of range.getClientRects()) {
                if (rect.width < 1 || rect.height < 1) continue;
                rects.push({
                  text: match[0],
                  x: rect.left + window.scrollX,
                  y: rect.top + window.scrollY,
                  w: rect.width,
                  h: rect.height,
                });
              }
            }
          } else if (node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName;
            if (tag !== 'SCRIPT' && tag !== 'STYLE') walk(node);
          }
        }
      };
      walk(document.body);
      // De-duplicate identical rectangles (nested inline wrappers).
      const seen = new Set();
      return rects.filter((rect) => {
        const key = `${Math.round(rect.x)}:${Math.round(rect.y)}:${rect.text}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    },
    { source: combined.source, flags: combined.flags },
  );
}

async function runShot(browser, shot, credentials) {
  const stateDir = path.join(outDir, '.states');
  fs.mkdirSync(stateDir, { recursive: true });
  const actor = shot.loginAs ? { ...credentials, ...(shot.loginAs(credentials) ?? {}) } : credentials;
  const stateKey = (shot.loginAs ? 'actor-' : '') + (actor.email ?? actor.login ?? 'anon');
  const statePath = path.join(stateDir, stateKey + '.json');
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    storageState: !shot.terminal && fs.existsSync(statePath) ? statePath : undefined,
    recordVideo: has('--video') ? { dir: path.join(outDir, 'video'), size: { width: 1440, height: 900 } } : undefined,
  });
  const page = await context.newPage();
  try {
    if (shot.terminal) {
      // CLI transcripts render as deterministic HTML — same fonts, same
      // redaction scan, none of a live terminal's flicker.
      const esc = (line) =>
        line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const body = [
        `<div class="bar">${esc(shot.terminal.title)}</div>`,
        ...shot.terminal.lines.map((line) => `<pre>${esc(line) || ' '}</pre>`),
      ].join('');
      const html = `<!doctype html><meta charset="utf-8"><style>
        body{margin:0;background:#0b1021;color:#dbe6ff;font:16px/1.55 DejaVu Sans Mono,monospace}
        .bar{background:#161d33;padding:10px 18px;color:#8ea2c8}
        pre{margin:0;padding:6px 18px;white-space:pre-wrap}
      </style>${body}`;
      await page.goto('data:text/html;charset=utf-8,' + encodeURIComponent(html), { waitUntil: 'load' });
      await page.setViewportSize({ width: 980, height: Math.max(360, 40 + shot.terminal.lines.length * 27 + 60) });
    } else {
      if (shot.service && loginFunctions[shot.service]) {
        // Best-effort auth: storage state may already carry the session.
        await loginFunctions[shot.service](page, actor).catch(() => {});
      }
      for (const step of shot.steps) {
        if (step.goto) {
          await page.goto(
            step.goto.startsWith('http') ? step.goto : baseUrl(shot.service) + step.goto,
            { waitUntil: 'domcontentloaded' },
          );
        } else if (step.wait) {
          await page.waitForTimeout(step.wait);
        } else if (step.waitForSelector) {
          await page.waitForSelector(step.waitForSelector, { timeout: step.timeout ?? 15000 });
        } else if (step.click) {
          await page.locator(step.click).first().click();
        } else if (step.fill) {
          await page.locator(step.fill).first().fill(step.value);
        } else if (step.press) {
          await page.keyboard.press(step.press);
        } else if (step.shotAfter) {
          await page.waitForTimeout(step.shotAfter);
        }
      }
    }
    const rects = await collectRedactionRects(page);
    const extra = [];
    for (const selector of shot.blur ?? []) {
      const box = await page.locator(selector).first().boundingBox().catch(() => null);
      if (box) extra.push({ text: `selector:${selector}`, ...box });
    }
    fs.mkdirSync(outDir, { recursive: true });
    const png = path.join(outDir, shot.shot + '.png');
    await page.screenshot({ path: png, fullPage: !shot.viewportOnly && !shot.terminal });
    fs.writeFileSync(
      path.join(outDir, shot.shot + '.meta.json'),
      JSON.stringify(
        {
          shot: shot.shot,
          area: shot.area,
          caption: shot.caption,
          url: page.url().startsWith('data:') ? `(rendered transcript: ${shot.terminal?.title ?? ''})` : page.url(),
          service: shot.service ?? null,
          fullPage: !shot.viewportOnly && !shot.terminal,
          rects: [...rects, ...extra],
          captured: new Date().toISOString(),
        },
        null,
        2,
      ),
    );
    // Persist the authenticated state for reuse across shots of one service.
    if (!shot.terminal) await context.storageState({ path: statePath }).catch(() => {});
    const kinds = [...new Set(rects.map((rect) => rect.text))];
    console.log(`[capture] ${shot.shot}: ${rects.length} redaction rects (${kinds.length} distinct) -> ${png}`);
    if (shot.expectMissing && shot.expectMissing.length) {
      const joined = rects.map((rect) => rect.text.toLowerCase()).join('\n');
      for (const must of shot.expectMissing) {
        if (!joined.includes(must.toLowerCase())) {
          throw new Error(`fail-closed scan: expected redaction of "${must}" found none`);
        }
      }
    }
  } finally {
    await context.close();
  }
}

const wanted = has('--all')
  ? SHOTS.filter((shot) => !argValue('--area') || shot.area === argValue('--area'))
  : SHOTS.filter((shot) => shot.shot === argValue('--shot'));
if (wanted.length === 0) {
  console.error(`no shots matched (${argValue('--shot') ?? '--all'})`);
  process.exit(2);
}

const credentials = {
  email: installEnv.DOKPLOY_ADMIN_EMAIL,
  login: redactEnv.REDACT_LOGIN || installEnv.DOKPLOY_ADMIN_EMAIL?.split('@')[0],
  password: installEnv.DOKPLOY_ADMIN_PASSWORD,
};

const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
try {
  for (const shot of wanted) await runShot(browser, shot, credentials);
} finally {
  await browser.close();
}
console.log(`[capture] done: ${wanted.length} shot(s)`);
