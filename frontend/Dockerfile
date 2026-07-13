# Production Dockerfile
# Multi-stage build: Build app, then serve with nginx

# Stage 1: Build the application
FROM node:22-alpine AS builder

WORKDIR /app

# Copy package files and install dependencies
COPY package*.json ./
RUN npm ci --cache .npm --prefer-offline --ignore-scripts

COPY . .
RUN npm run build

# Stage 2: Serve with nginx
FROM nginx:alpine

# Copy nginx configuration
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template

# Copy built application
COPY --from=builder /app/dist /usr/share/nginx/html

# Environment variables for API configuration
ENV API_URL=http://localhost:8000
ENV MOCK_API=false
# DNS for the variable proxy_pass targets. Docker's embedded resolver; override
# when this image runs somewhere without it.
ENV NGINX_RESOLVER=127.0.0.11

# Expose port
EXPOSE 80

# Start nginx with environment variable substitution
CMD ["nginx", "-g", "daemon off;"]
