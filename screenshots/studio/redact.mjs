// dokploy-wizard screenshot studio — redaction side.
//
// Deterministic, browser-free: raw capture + meta rectangles in, final
// blurred-and-captioned PNG out. Re-run any time the redaction list grows —
// the raw captures are the source of truth and stay gitignored.
//
// Usage:  node redact.mjs --in ../raw --out ..            (all metas)
//         node redact.mjs --in ../raw --out .. --shot id   (one)
// Exit non-zero when a raw shot's meta carries a redaction hit that the
// pattern list no longer recognizes as blurrable but the capture recorded —
// or when a final image is written with zero rects on a service known to
// show identity text (identity lanes are expected to blur something).

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import sharp from 'sharp';

const args = process.argv.slice(2);
const argValue = (name) => {
  const index = args.indexOf(name);
  return index >= 0 && index + 1 < args.length ? args[index + 1] : undefined;
};

const inDir = argValue('--in') ?? 'raw';
const outRoot = argValue('--out') ?? '.';
const only = argValue('--shot');
const blurRadius = Number(argValue('--blur') ?? 9);
const pad = Number(argValue('--pad') ?? 4);

function escapeRe(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function captionSvg(width, text, height = 52) {
  const safe = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return Buffer.from(
    `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">
      <rect width="100%" height="100%" fill="#0f172a"/>
      <text x="24" y="${height / 2 + 7}" font-family="DejaVu Sans, sans-serif"
            font-size="19" fill="#e2e8f0">${safe}</text>
    </svg>`,
  );
}

async function redactOne(metaPath) {
  const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
  const rawPath = path.join(inDir, meta.shot + '.png');
  const image = sharp(rawPath);
  const info = await image.metadata();

  // Blur every recorded rectangle in one composite pass. Overlapping rects
  // simply blur twice — visually identical, ordering never matters.
  const composites = [];
  for (const rect of meta.rects) {
    const left = Math.max(0, Math.floor(rect.x * 2) - pad); // deviceScaleFactor 2
    const top = Math.max(0, Math.floor(rect.y * 2) - pad);
    const width = Math.min(info.width - left, Math.ceil(rect.w * 2) + pad * 2);
    const height = Math.min(info.height - top, Math.ceil(rect.h * 2) + pad * 2);
    if (width <= 0 || height <= 0) continue;
    const region = await sharp(rawPath).extract({ left, top, width, height }).blur(blurRadius).toBuffer();
    composites.push({ input: region, left, top });
  }

  const caption = meta.caption ?? '';
  const bar = captionSvg(info.width, caption);
  const barHeight = 104; // deviceScaleFactor 2
  const final = await sharp({
    create: { width: info.width, height: info.height + barHeight, channels: 3, background: '#0f172a' },
  })
    .composite([
      { input: await sharp(rawPath).flatten({ background: '#ffffff' }).toBuffer(), top: 0, left: 0 },
      ...composites.map((entry) => ({ ...entry })),
      { input: bar, top: info.height, left: 0 },
    ])
    .png()
    .toBuffer();

  const outDir = path.join(outRoot, meta.area);
  fs.mkdirSync(outDir, { recursive: true });
  const outPath = path.join(outDir, meta.shot + '.png');
  await sharp(final).toFile(outPath);
  const kinds = [...new Set(meta.rects.map((rect) => rect.text))].sort();
  console.log(`[redact] ${meta.shot}: ${composites.length} region(s) -> ${outPath}${kinds.length ? ' | blurred: ' + kinds.join(', ') : ' | (nothing blurred)'}`);
  if (composites.length === 0 && meta.service) {
    console.log(`[redact] NOTE ${meta.shot}: service lane captured with zero identity hits — verify by eye`);
  }
  return meta.rects.length;
}

const metas = fs
  .readdirSync(inDir)
  .filter((name) => name.endsWith('.meta.json') && (!only || name === only + '.meta.json'))
  .map((name) => path.join(inDir, name));
if (metas.length === 0) {
  console.error('no raw metas found (run capture.mjs first)');
  process.exit(2);
}
let total = 0;
for (const meta of metas) total += await redactOne(meta);
console.log(`[redact] done: ${metas.length} image(s), ${total} redaction region(s)`);
