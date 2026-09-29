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

## Related

- [Gateway overhead](../guide/concepts/gateway-overhead.md)
- [Quick Start: CLI onboarding](../guide/quickstart-cli.md)
- [What Preloop includes](../guide/functionality.md)
