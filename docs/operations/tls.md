# Expose Preloop With TLS

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The OSS installer starts Preloop on local HTTP ports:

- Console: `http://localhost:3000`
- API: `http://localhost:8000`
- Model gateway: `http://localhost:8001`

For a public deployment, keep those services on local ports and terminate TLS in a
host reverse proxy. This keeps the Docker Compose stack simple while letting you
use standard web-server tooling for certificates, redirects, request limits, and
access logs.

The examples below assume a single public hostname:

```text
https://preloop.example.com
```

## Prerequisites

- A Linux host with ports `80` and `443` reachable from the internet.
- DNS `A` or `AAAA` records for your hostname pointing at the host.
- Docker Compose stack installed with `curl -fsSL https://preloop.ai/install/oss | sh`.
- Nginx and Certbot installed on the host.

On Ubuntu or Debian:

```bash
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

## Configure Preloop's Public URL

Set `PRELOOP_URL` to the public HTTPS origin in `~/.preloop-oss/docker-compose.yaml`
for the `api`, `gateway`, `scheduler`, and `worker` services:

```yaml
environment:
  PRELOOP_URL: https://preloop.example.com
```

`PRELOOP_URL` is used for OAuth metadata, MCP resource URLs, webhook registration,
approval links, email links, Agent Control WebSocket URLs, and gateway metadata. Use
the public URL users and agents will actually open.

Apply the change:

```bash
cd ~/.preloop-oss
docker compose up -d
```

## Nginx Reverse Proxy

Create `/etc/nginx/sites-available/preloop`:

```nginx
server {
    listen 80;
    server_name preloop.example.com;

    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /mcp {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /.well-known/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /oauth/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /approval/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /openai/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /anthropic/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /gemini/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable and validate it:

```bash
sudo ln -s /etc/nginx/sites-available/preloop /etc/nginx/sites-enabled/preloop
sudo nginx -t
sudo systemctl reload nginx
```

## Issue a Certificate

Use Certbot's Nginx integration:

```bash
sudo certbot --nginx -d preloop.example.com
```

Choose the redirect option when prompted so plain HTTP redirects to HTTPS.
Certbot installs a renewal timer automatically on most Linux distributions.

Check renewal status:

```bash
systemctl list-timers | grep certbot
sudo certbot renew --dry-run
```

## Verify

From a different machine:

```bash
curl -fsSL https://preloop.example.com/api/v1/health
curl -fsSL https://preloop.example.com/api/v1/version
```

Then open:

```text
https://preloop.example.com
```

For CLI and agent onboarding, target the public origin:

```bash
PRELOOP_URL=https://preloop.example.com preloop login --headless
```

## Notes

- Keep Postgres and NATS unexposed. Only the reverse proxy needs public ports.
- Gateway routes can stream for a long time, so the example disables proxy
  buffering and raises read/send timeouts for `/openai/`, `/anthropic/`, and
  `/gemini/`.
- If you use separate hostnames for console, API, and gateway, update
  `PRELOOP_URL` and the proxy routes consistently so OAuth, MCP, approval links,
  Agent Control WebSocket URLs, and model gateway URLs point at reachable public URLs.

On a public TLS install, `/openai/`, `/anthropic/`, and `/gemini/` should go
from the edge proxy straight to the gateway. To split that hop from the
Preloop process itself, measure HTTP to port 8001 and HTTPS on the public
origin with the same script: [Gateway overhead](../guide/concepts/gateway-overhead.md).

## Strict-Transport-Security (HSTS)

Preloop sends `Strict-Transport-Security` on responses to requests that arrived
over HTTPS: the request carries `X-Forwarded-Proto: https` (the reverse proxy
example above sets it), or the connection scheme itself is `https`. Plain HTTP
requests never get the header. The console nginx adds it for everything it
serves and proxies, and drops any copy from upstream so a response carries
exactly one. The API and model gateway add it themselves for traffic that
reaches them directly, unless the response already has one.

This is on by default. Installs that upgrade to this release and serve HTTPS
start sending a one-year policy.

| Env var (Compose: `console`, `api`, `gateway`) | Helm value | Default | Meaning |
|---|---|---|---|
| `HSTS_ENABLED` | `securityHeaders.hsts.enabled` | `true` | Master switch. `false` sends no header. |
| `HSTS_MAX_AGE` | `securityHeaders.hsts.maxAge` | `31536000` | Policy lifetime in seconds. |
| `HSTS_INCLUDE_SUBDOMAINS` | `securityHeaders.hsts.includeSubDomains` | `false` | Appends `; includeSubDomains`. |
| `HSTS_PRELOAD` | `securityHeaders.hsts.preload` | `false` | Appends `; preload`. |

Set the same values on the console, API and gateway services in Docker
Compose; Helm passes `securityHeaders.hsts` to all three.

!!! warning
    A browser that has seen the header refuses plain HTTP for the host until
    `max-age` expires, and turning HSTS off later does not clear policies
    browsers already cached. For a first rollout on a host that may go back to
    HTTP, start with a short `HSTS_MAX_AGE` (for example `300`) and raise it
    once HTTPS is stable.

    `includeSubDomains` applies the policy to every subdomain of the host,
    including ones Preloop does not serve. Enable it only when all of them
    serve HTTPS. `preload` asks browsers to hard-code the policy and is
    difficult to undo; it requires `includeSubDomains` and a `max-age` of at
    least `31536000`. The API refuses to start and `helm template` fails when
    `preload` is set without both. Preloop never submits a domain to the
    preload list.

Check it:

```bash
curl -sI https://preloop.example.com/ | grep -i strict-transport-security
curl -sI https://preloop.example.com/api/v1/health | grep -i strict-transport-security
```

## Related

- [Gateway overhead](../guide/concepts/gateway-overhead.md)
- [Quick Start: CLI onboarding](../guide/quickstart-cli.md)
- [What Preloop includes](../guide/functionality.md)
