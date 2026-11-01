# Web_Frontend image (task 14.1) -- builds the Vite SPA and serves the static
# bundle with Nginx. Targets linux/arm64 (the DGX Spark). The top-level Nginx
# reverse proxy (deploy/nginx) routes non-/api traffic to this service; this
# inner Nginx serves index.html with a client-side-routing fallback.

# --- Build stage: produce the static assets in /app/dist -------------------
# Target platform for the base images. Defaults to linux/arm64 (the DGX Spark);
# override with --build-arg TARGET_PLATFORM=linux/amd64 for an x86_64 host
# (e.g. the 192.0.2.10 deployment). Declared before the first FROM so both
# build stages can reference it.
ARG TARGET_PLATFORM=linux/arm64
FROM --platform=${TARGET_PLATFORM} node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# `npm run build` runs `tsc --noEmit && vite build`.
RUN npm run build

# --- Serve stage: static files behind Nginx --------------------------------
FROM --platform=${TARGET_PLATFORM} nginx:1.27-alpine AS serve
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
