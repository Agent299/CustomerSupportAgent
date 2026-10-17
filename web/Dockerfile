# Static Next.js export served by Caddy (Caddyfile is mounted at runtime from deploy/oracle).
FROM node:24.22.0-alpine3.24 AS build
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
# Empty = same origin: Caddy proxies /api to the backend, so no CORS and first-party cookies.
ENV NEXT_PUBLIC_API_BASE_URL="" \
    NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

FROM caddy:2.11.7-alpine
COPY --from=build /app/out /srv/www
