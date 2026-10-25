import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';
import { createServer } from 'vite';
import { chromium } from '@playwright/test';

let server;
let discover;
let renderPost;

before(async () => {
  server = await createServer({
    configFile: false,
    server: { middlewareMode: true, watch: null },
    optimizeDeps: { noDiscovery: true },
  });
  ({ discover_blog_posts: discover } = await server.ssrLoadModule(
    '/vite-plugin-brand.ts'
  ));
  ({ render_blog_post_html: renderPost } =
    await server.ssrLoadModule('/src/blog-seo.ts'));
});

after(async () => {
  await server?.close();
});

async function readPost(t, frontmatter) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'blog-frontmatter-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.mkdirSync(path.join(dir, 'example', 'blog'), { recursive: true });
  fs.writeFileSync(
    path.join(dir, 'example', 'blog', 'a-post.md'),
    `---
title: A Post
description: A description.
date: 2026-07-18
${frontmatter}
---
Body.
`
  );
  const warnings = [];
  const warn = t.mock.method(console, 'warn', (message) =>
    warnings.push(message)
  );
  try {
    const posts = await discover(dir, 'example');
    assert.equal(posts.length, 1);
    return { post: posts[0], warnings };
  } finally {
    warn.mock.restore();
  }
}

for (const sources of [
  { mp4: '/assets/blog/fleet.mp4', webm: '/assets/blog/fleet.webm' },
  { mp4: '/assets/blog/fleet.mp4' },
  { webm: '/assets/blog/fleet.webm' },
]) {
  test(`passes through hero_video with ${Object.keys(sources).join(' and ')}`, async (t) => {
    const video = {
      ...sources,
      alt: 'Fleet activity',
      caption: 'Agents working',
    };
    const { post, warnings } = await readPost(
      t,
      `og_image: /assets/blog/fleet.png\nhero_video: ${JSON.stringify(video)}`
    );
    assert.deepEqual(JSON.parse(JSON.stringify(post.hero_video)), video);
    assert.deepEqual(warnings, []);
  });
}

test('warns when the video has no og_image poster', async (t) => {
  const { post, warnings } = await readPost(
    t,
    'hero_video: {mp4: /assets/blog/fleet.mp4, alt: Fleet activity}'
  );
  assert.equal(post.hero_video.mp4, '/assets/blog/fleet.mp4');
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /a-post.md.*hero_video.*no og_image/);
});

test('warns and falls back to the image when the video has no sources', async (t) => {
  const { post, warnings } = await readPost(
    t,
    'og_image: /assets/blog/fleet.png\nhero_video: {alt: Fleet activity}'
  );
  assert.equal(post.hero_video, undefined);
  assert.equal(post.og_image, '/assets/blog/fleet.png');
  assert.match(warnings[0], /a-post.md.*at least one source/);
});

for (const value of [
  null,
  'fleet.mp4',
  [],
  {},
  { mp4: '/fleet.mp4' },
  { mp4: '/fleet.mp4', alt: ' ' },
  { mp4: 123, alt: 'Fleet activity' },
  { webm: '', alt: 'Fleet activity' },
  { mp4: '/fleet.mp4', alt: 'Fleet activity', caption: {} },
]) {
  test(`warns and ignores malformed hero_video: ${JSON.stringify(value)}`, async (t) => {
    const { post, warnings } = await readPost(
      t,
      `og_image: /assets/blog/fleet.png\nhero_video: ${JSON.stringify(value)}`
    );
    assert.equal(post.hero_video, undefined);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /a-post.md.*ignoring hero_video/);
  });
}

test('retains image-only posts without a video warning', async (t) => {
  const { post, warnings } = await readPost(
    t,
    'og_image: /assets/blog/fleet.png'
  );
  assert.equal(post.hero_video, undefined);
  assert.deepEqual(warnings, []);
});

for (const postImage of [true, false]) {
  test(`reduced motion shows the ${postImage ? 'post' : 'brand default'} poster at the same width and spacing`, async (t) => {
    const { post } = await readPost(
      t,
      `${postImage ? 'og_image: /assets/blog/fleet.png\n' : ''}hero_video: {mp4: /assets/blog/fleet.mp4, alt: Fleet activity}`
    );
    const browser = await chromium.launch();
    try {
      const page = await browser.newPage();
      // No scripts are loaded: the fallback must work in the static HTML alone.
      const config = {
        name: 'Example',
        landing: { meta: { og_image: '/assets/blog/default.png' } },
      };
      await page.setContent(renderPost(post, config, ''));
      const poster = postImage
        ? '/assets/blog/fleet.png'
        : '/assets/blog/default.png';
      assert.equal(await page.locator('video').getAttribute('poster'), poster);
      assert.equal(
        await page.locator('.blog-hero-poster').getAttribute('src'),
        poster
      );
      await page.emulateMedia({ reducedMotion: 'no-preference' });
      assert.equal(await page.locator('.blog-hero video').isVisible(), true);
      assert.equal(await page.locator('.blog-hero-poster').isVisible(), false);
      const videoWidth = (await page.locator('.blog-hero video').boundingBox())
        .width;
      const spacing = await page
        .locator('.blog-hero')
        .evaluate((figure) => getComputedStyle(figure).marginBottom);
      await page.emulateMedia({ reducedMotion: 'reduce' });
      assert.equal(await page.locator('.blog-hero video').isVisible(), false);
      assert.equal(await page.locator('.blog-hero-poster').isVisible(), true);
      assert.equal(
        (await page.locator('.blog-hero-poster').boundingBox()).width,
        videoWidth
      );
      assert.equal(
        await page
          .locator('.blog-hero')
          .evaluate((figure) => getComputedStyle(figure).marginBottom),
        spacing
      );
    } finally {
      await browser.close();
    }
  });
}
