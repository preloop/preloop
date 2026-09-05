# Preloop Console

This directory contains the new Preloop frontend, a modern web application built with Lit, Vite, and TypeScript. It uses Shoelace Web Components for UI.

## Description

This frontend is a single-page application (SPA) that provides a rich, interactive user experience for managing MCP servers, issue trackers, and more through the Preloop API. It is designed to be fast, responsive, and maintainable.

## Development

### Prerequisites

- Node.js (v18 or higher)
- npm (v9 or higher)

### Installing Dependencies

To install the necessary dependencies, run the following command:

```bash
npm install
```

### Running the Development Server

To start the local development server with Hot Module Replacement (HMR), run:

```bash
npm run dev
```

The application will be available at `http://localhost:5173` by default.

### Building for Production

To create an optimized production build, run:

```bash
npm run build
```

The output files will be placed in the `dist/` directory.

### Running Tests

To run the test suite using Web Test Runner, use the following command:

```bash
npm run test
```

Authenticated views extend `AuthedElement` in `src/api.ts` and call the API through `fetchWithAuth` (JWT in `localStorage`, refresh, redirect to `/login` on auth failure). Routes live in `src/components/lit-app.ts`; do not copy a route map into this file.

In Web Test Runner tests, stub `window.fetch` (not ES-module exports). Use `waitUntil()` from `@open-wc/testing` for async DOM updates. Browser console errors during tests that assert error handling are expected.

## Architecture

### SSR Content Slotting for SEO

The frontend uses a slotting mechanism to provide SEO-friendly server-side rendered content while maintaining the benefits of a single-page application (SPA).

#### How It Works

1. **Build Time (`vite-plugin-brand.ts`)**:
   - The Vite plugin generates static HTML pages (index.html, about.html, pricing.html, etc.)
   - Each page includes pre-rendered content inside web component tags in the light DOM
   - Content is wrapped in appropriate wrapper components:
     - Landing page: `<landing-view>` with slotted content
     - Static pages: `<static-view-wrapper>` with article content
   - A `data-ssr-route` attribute on `<lit-app>` indicates which route the SSR content is for

2. **Runtime Hydration (`lit-app.ts`)**:
   - On page load, `firstUpdated()` checks if SSR content exists in the light DOM
   - If the SSR route matches the current URL path, the content is moved to the router outlet
   - The router then reuses this existing content instead of creating new components
   - If routes don't match (e.g., SPA navigation), SSR content is removed

3. **Component Consumption**:
   - `landing-view` reads slotted content via named slots (e.g., `slot="hero-title"`)
   - `static-view-wrapper` uses a default slot to display article content
   - Components can also load content dynamically via JSON/markdown for client-side navigation

#### File Structure

```
vite-plugin-brand.ts          # Generates SSR HTML at build time
src/components/
  lit-app.ts                  # Handles SSR content hydration
  static-view-wrapper.ts      # Wrapper for static pages (privacy, about, etc.)
  static-view.ts              # Dynamic markdown loader for client-side nav
src/views/public/
  landing-view.ts             # Landing page with slot consumption
```

#### Key Functions

- `generateSlottedContentForRoute()` - Creates slotted HTML for landing page
- `generateFullHtmlPage()` - Generates complete HTML pages for static routes
- `loadMarkdownContent()` - Converts markdown to styled HTML articles

#### Benefits

- **SEO**: Search engines see fully rendered content on first load
- **Performance**: No flash of unstyled content (FOUC) with critical CSS
- **SPA Experience**: After hydration, navigation is instant client-side
- **Maintainability**: Content defined in `brands.yaml` and markdown files
