# Authoring blog posts

Blog posts are Markdown files at `content/<brand>/blog/<slug>.md`, published
only for SaaS brands. Slugs use lowercase letters, numbers and hyphens.
Each post starts with YAML front matter:

```yaml
---
title: A release update
description: A short summary for search results and the blog index.
date: 2026-10-20
author: Jane Doe
tags: [Releases]
og_image: /assets/blog/release.png
---
```

`title`, `description` and `date` are required. Optional fields include `updated`
(ISO date), `author_url`, `related` (internal routes), and `draft: true` to omit
the post from publication. `og_image` supplies the post hero and index thumbnail.

For a silent screencast hero, add `hero_video`:

```yaml
og_image: /assets/blog/fleet-map.png
hero_video:
  webm: /assets/blog/fleet-map.webm
  mp4: /assets/blog/fleet-map.mp4
  alt: Agents working and waiting for approval in the Fleet Map
  caption: Live activity across the agent fleet
```

At least one of `mp4` or `webm` is required, along with non-empty `alt` text
(the video's accessible label). `caption` is optional. All values must be
non-empty strings. WebM is offered first when both formats are present.
Invalid video metadata produces a build warning and leaves the image hero.
Provide `og_image` as the poster and static reduced-motion fallback; the build
warns if it is missing and uses the brand's default social image for the video
poster, reduced-motion fallback and structured data when available. The index
and social cards continue to use `og_image`.

The video autoplays muted, loops, and plays inline. The blog stylesheet hides
it and shows the poster when `prefers-reduced-motion: reduce` is set, without
JavaScript. Keep this one deliberate motion within the design motion budget
and use real product recordings. Video structured data uses the post title,
description and date, the poster, and the MP4 source (or WebM if MP4 is absent).
