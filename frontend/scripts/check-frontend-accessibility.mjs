/** Guard readable text colors and programmatic names on input controls. */
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export function accessibilityFindings(source) {
  const findings = [];
  const clean = source.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const match of clean.matchAll(/(?<![\w-])color:\s*var\(--sl-color-neutral-(400|500)\)/g)) {
    findings.push('Text color must use --console-meta-color: ' + match[0]);
  }
  for (const match of clean.matchAll(/<(sl-input|sl-select|sl-textarea|sl-switch|sl-checkbox|sl-radio-group|sl-radio-button|sl-radio|sl-icon-button|input|select|textarea)\b([\s\S]*?)>/g)) {
    const attrs = match[2];
    if (['sl-switch', 'sl-checkbox', 'sl-radio-button', 'sl-radio'].includes(match[1])) {
      const tail = clean.slice(match.index + match[0].length);
      const content = tail.slice(0, tail.indexOf(`</${match[1]}>`)).replace(/<[^>]+>/g, '').trim();
      if (content) continue;
    }
    if (/\b(label|aria-label|aria-labelledby)\s*=\s*(?:"[^"]+"|'[^']+'|\$\{)/.test(attrs) || /type=["']hidden["']/.test(attrs)) continue;
    const id = attrs.match(/\bid=["']([^"']+)["']/)?.[1];
    if (id && clean.includes(`for="${id}"`)) continue;
    findings.push('Control needs a programmatic name: ' + match[0].split('\n')[0]);
  }
  return findings;
}

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  return (await Promise.all(entries.map(async (entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(full);
    return /\.(ts|css)$/.test(entry.name) && !/\.test\.ts$/.test(entry.name) ? [full] : [];
  }))).flat();
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const src = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src');
  const failures = [];
  for (const file of await walk(src)) {
    for (const finding of accessibilityFindings(await readFile(file, 'utf8'))) {
      failures.push(`${path.relative(src, file)}: ${finding}`);
    }
  }
  if (failures.length) {
    console.error(failures.join('\n'));
    process.exitCode = 1;
  }
}
