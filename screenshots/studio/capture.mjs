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
    // Dotless variant: workspace names mint 'showmeseewhycom-workspace' from
    // the root domain (dots stripped — a legal Docker/CSI name), and those
    // strings leak the domain verbatim (caught in vision review of the Coder
    // settings and LiteLLM keys shots).
    if (kind === 'root-domain' && value.includes('.')) {
      patterns.push({ kind, text: value.replace(/\./g, '') });
      // Hyphenated variant: Docker network names bake 'showmeseewhy-com-shared'
      // (dots to hyphens) and it appears verbatim in the template source view.
      patterns.push({ kind, text: value.replace(/\./g, '-') });
    }
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
    // Box-measured (v0.31.4): the sign-in form is served at '/' itself;
    // '/login' is a hard 404 page. Submitting lands on /dashboard/home —
    // waiting for the dashboard (and the Projects nav item) is the fail-closed
    // proof the session cookie is honoured (a '/' capture at that instant is
    // still the anonymous form — caught in vision review).
    await page.goto(baseUrl('dokploy') + '/', { waitUntil: 'domcontentloaded' });
    await page.getByLabel(/email/i).first().waitFor({ timeout: 45000 });
    await page.getByLabel(/email/i).first().fill(credentials.email);
    await page.getByLabel(/password/i).first().fill(credentials.password);
    await page.locator('button:has-text("Login"), button[type=submit]').first().click();
    await page.waitForURL((url) => url.pathname.startsWith('/dashboard'), { timeout: 30000 });
    await page.waitForSelector('text=/Projects/i', { timeout: 45000 });
  },
  coder: async (page, credentials) => {
    await page.goto(baseUrl('coder') + '/login', { waitUntil: 'domcontentloaded' });
    await page.locator('input[name="email"]').waitFor({ timeout: 15000 });
    await page.locator('input[name="email"]').fill(credentials.email);
    await page.locator('input[name="password"]').fill(credentials.password);
    await page.locator('button[type=submit]').first().click();
    await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 20000 });
  },
  gitea: async (page, credentials) => {
    // Box-measured (28.4.0): the login fields are #user_name/#password and the
    // page carries an unrelated hidden submit button (Stop Timer) — so the
    // submitter is picked by its visible label, never by type alone.
    await page.goto(baseUrl('gitea') + '/user/login', { waitUntil: 'domcontentloaded' });
    await page.locator('#user_name').waitFor({ timeout: 15000 });
    await page.locator('#user_name').fill(credentials.login ?? credentials.email);
    await page.locator('#password').fill(credentials.password);
    await page.locator('button').filter({ hasText: /sign in|log ?in/i }).first().click();
    await page.waitForURL((url) => !url.pathname.startsWith('/user/login'), { timeout: 20000 });
  },
  filex: async (page, credentials) => {
    // Box-measured: the whole console lives under /admin/; /login 404s.
    await page.goto(baseUrl('filex') + '/admin/login', { waitUntil: 'domcontentloaded' });
    await page.locator('input[name="email"]').waitFor({ timeout: 15000 });
    await page.locator('input[name="email"]').fill(credentials.email);
    await page.locator('input[name="password"]').fill(credentials.password);
    await page.locator('button').filter({ hasText: /sign in|log ?in/i }).first().click();
    await page.waitForURL((url) => !url.pathname.startsWith('/admin/login'), { timeout: 20000 });
  },
  litellm: async (page, credentials) => {
    // Box-measured (v1.83.14): the React form hydrates after first paint, so
    // the inputs are awaited before filling. After a successful POST /v2/login
    // the SPA validates the session (GET /v2/user/info) before it will honour
    // any deep link — captured before that, every ?page= route bounces back to
    // /ui/login/. Waiting for the validation response is the reliable gate.
    await page.goto(baseUrl('litellm') + '/ui/login/', { waitUntil: 'domcontentloaded' });
    await page.locator('#username').waitFor({ timeout: 15000 });
    // The UI account is the ADMIN EMAIL (measured: username-style 'clayton'
    // intermittently 401s; the email logs in every time).
    await page.locator('#username').fill(credentials.email);
    await page.locator('#password').fill(credentials.password);
    const validated = page
      .waitForResponse((res) => /user\/info/.test(res.url()), { timeout: 25000 })
      .catch(() => null);
    await page.locator('button').filter({ hasText: /log ?in/i }).first().click();
    await page.waitForURL((url) => !url.pathname.startsWith('/ui/login'), { timeout: 20000 });
    await validated;
    // The landing page (Virtual Keys home) finishing its FIRST table render is
    // the true session-ready signal; any deep link tried before it bounces
    // back to /ui/login/ (measured: ?page=… links captured at that moment all
    // failed, the same links work once the home table is visible).
    await page.waitForSelector('table tbody tr', { timeout: 30000 });
    await page.waitForTimeout(1000);
  },
  docuseal: async (page, credentials) => {
    // Box-measured: this DocuSeal build serves login at /sign_in
    // (/account/sign_in renders the app's own 404 page). After submit it
    // lands on '/' — which doubles as the authenticated dashboard (anonymous
    // '/' shows only the Sign In chrome, so shots anchor on app-only text).
    await page.goto(baseUrl('docuseal') + '/sign_in', { waitUntil: 'domcontentloaded' });
    await page.locator('input[name="user[email]"]').waitFor({ timeout: 30000 });
    await page.locator('input[name="user[email]"]').fill(credentials.email);
    await page.locator('input[name="user[password]"]').fill(credentials.password);
    await page.locator('button[type=submit], input[type=submit]').first().click();
    await page.waitForURL((url) => !url.pathname.startsWith('/sign_in'), { timeout: 20000 });
  },
};

// Where each service parks an anonymous visitor. A captured shot that still
// sits on one of these is an UNAUTHENTICATED capture — the studio treats it
// as a failure rather than quietly committing a login page (measured: every
// service showed one during the first selector-mapping pass). Entries are
// path prefixes; '/' as its own entry means an EXACT match (it is a prefix of
// everything).
const loginPaths = {
  dokploy: ['/login', '/'],
  coder: '/login',
  gitea: '/user/login',
  filex: '/admin/login',
  litellm: '/ui/login',
  docuseal: '/sign_in',
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
      // Form field VALUES are text the walker can't reach (no text nodes) —
      // the Coder settings Name input leaked 'showmeseewhycom-workspace' right
      // under the blurred breadcrumb. Masked password fields render dots, so
      // they are skipped; everything else gets a conservative whole-field
      // blur when its value matches the redaction set.
      for (const el of document.querySelectorAll('input, textarea')) {
        const input = el;
        if (input.type === 'password') continue;
        const value = input.value ?? '';
        regex.lastIndex = 0;
        if (!regex.test(value)) continue;
        const box = input.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;
        rects.push({
          text: value,
          x: box.left + window.scrollX,
          y: box.top + window.scrollY,
          w: box.width,
          h: box.height,
        });
      }
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
  const actor = shot.loginAs ? { ...credentials, ...(shot.loginAs(credentials) ?? {}) } : credentials;
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    // Deliberately NO storage-state reuse between shots (measured): a saved
    // session makes a `noAuth` login-page shot redirect to the dashboard, and
    // a stale LiteLLM JWT half-skips auth and bounces the first deep link.
    // Logging in per shot costs ~3s and buys a deterministic, clean context.
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
      if (shot.service && loginFunctions[shot.service] && !shot.noAuth) {
        // Best-effort auth: storage state may already carry the session.
        // `noAuth` login-page shots must stay anonymous to show the form.
        await loginFunctions[shot.service](page, actor).catch(() => {});
        // SPA settle: React UIs finish wiring the session (cookies, token
        // exchange) after the redirect; capturing before that can bounce the
        // first deep link back to the login screen (measured on LiteLLM).
        await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
        await page.waitForTimeout(1200);
      }
      for (const step of shot.steps) {
        if (step.goto) {
          const path = step.goto.replace('{owner}', actor.login ?? String(actor.email ?? '').split('@')[0]);
          await page.goto(
            path.startsWith('http') ? path : baseUrl(shot.service) + path,
            { waitUntil: 'domcontentloaded' },
          );
        } else if (step.waitForSelector) {
          await page.waitForSelector(step.waitForSelector, { timeout: step.timeout ?? 15000 });
          if (step.wait) await page.waitForTimeout(step.wait);
        } else if (step.hover) {
          await page.locator(step.hover).first().hover();
        } else if (step.click) {
          await page.locator(step.click).first().click().catch((err) => {
            if (!step.optional) throw err;
          });
        } else if (step.wait) {
          await page.waitForTimeout(step.wait);
        } else if (step.fill) {
          await page.locator(step.fill).first().fill(step.value);
        } else if (step.press) {
          await page.keyboard.press(step.press);
        } else if (step.shotAfter) {
          await page.waitForTimeout(step.shotAfter);
        }
      }
    }
    // Fail closed on a silently-unauthenticated capture: an authed shot that
    // still sits on its service's login page throws instead of committing a
    // login screen under a caption about dashboards.
    if (shot.service && !shot.noAuth && loginPaths[shot.service]) {
      const landed = new URL(page.url()).pathname;
      const guards = [loginPaths[shot.service]].flat();
      const onLogin = guards.some((g) => (g === '/' ? landed === '/' : landed.startsWith(g)));
      if (onLogin) {
        throw new Error(`not authenticated for '${shot.service}' (still on ${landed})`);
      }
    }
    // SPA settle: query-routed React pages can navigate mid-evaluate; let
    // the router quiesce before the redaction scan walks the DOM.
    await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(1000);
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
    // Persist nothing: the next shot starts from a clean context.
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
let failures = 0;
try {
  for (const shot of wanted) {
    // One retry per shot: the box fronts everything through a Cloudflare
    // tunnel and React SPAs with post-login session validation, so a single
    // slow load is routine; a second attempt distinguishes that from a
    // genuinely broken selector (measured: filex login, coder navigation).
    let error = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await runShot(browser, shot, credentials);
        error = null;
        break;
      } catch (caught) {
        error = caught;
        if (attempt === 0) console.log(`[capture] retry ${shot.shot}: ${String(caught.message ?? caught).split('\n')[0]}`);
      }
    }
    if (error) {
      failures += 1;
      console.error(`[capture] FAIL ${shot.shot}: ${String(error.message ?? error).split('\n')[0]}`);
    }
  }
} finally {
  await browser.close();
}
console.log(`[capture] done: ${wanted.length - failures}/${wanted.length} shot(s) ok`);
if (failures) process.exit(1);
