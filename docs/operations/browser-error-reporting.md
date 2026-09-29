# Browser error reporting

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The console can report browser errors to Sentry. It is off by default: the
repository contains no DSN, and a build without one never initialises Sentry.

To enable it, pass the DSN at build time. The value is embedded in the
built JavaScript, so it cannot be changed at runtime:

```bash
# npm build
VITE_SENTRY_DSN="https://<key>@<sentry-host>/<project>" npm run build

# frontend Docker image
docker build --build-arg VITE_SENTRY_DSN="https://<key>@<sentry-host>/<project>" frontend/
```

A hosted build that reported errors before this setting existed must now
pass `VITE_SENTRY_DSN` explicitly. Without it, the next build stops
reporting and nothing fails.

Browser traces are sampled at 1 percent. Backend error reporting is
configured separately (Helm `sentry.enabled`, off by default).
