# Production Dockerfile
# Multi-stage build: Build app, then serve with nginx

# Stage 1: Build the application
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS builder

WORKDIR /app

# Copy package files and install dependencies
COPY package*.json ./
RUN npm ci --cache .npm --prefer-offline --ignore-scripts

COPY . .
# Browser error reporting is opt-in: pass --build-arg VITE_SENTRY_DSN=<dsn>
# to enable Sentry in the built bundle. Empty (the default) leaves it off.
ARG VITE_SENTRY_DSN=""
ENV VITE_SENTRY_DSN=${VITE_SENTRY_DSN}
RUN npm run build

# Stage 2: Serve with nginx
FROM nginx:alpine@sha256:54f2a904c251d5a34adf545a72d32515a15e08418dae0266e23be2e18c66fefa

# Copy nginx configuration
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY docker/security-headers.conf /etc/nginx/snippets/security-headers.conf

# Copy built application
COPY --from=builder /app/dist /usr/share/nginx/html

# Environment variables for API configuration
ENV API_URL=http://localhost:8000
ENV MOCK_API=false
# DNS for the variable proxy_pass targets. Docker's embedded resolver; override
# when this image runs somewhere without it.
ENV NGINX_RESOLVER=127.0.0.11
# Strict-Transport-Security for requests that arrived over HTTPS (see
# docs/operations/tls.md). On by default; HSTS_ENABLED=false opts out.
ENV HSTS_ENABLED=true
ENV HSTS_MAX_AGE=31536000
ENV HSTS_INCLUDE_SUBDOMAINS=false
ENV HSTS_PRELOAD=false

# Expose port
EXPOSE 80

# Start nginx with environment variable substitution
CMD ["nginx", "-g", "daemon off;"]
