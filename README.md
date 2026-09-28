# SubMux

SubMux is a small self-hosted subscription multiplexer. It fetches one or more upstream proxy subscriptions with per-source `User-Agent`, `X-HWID` and optional device headers, then exposes them through token-protected endpoints.

The web panel includes light/dark themes, subscription management, upstream tests and access-token ACLs.

## Endpoints

Every public subscription request requires a token.

```text
GET /<TOKEN>/subs
GET /<TOKEN>/sub/<name>
GET /subs?token=<TOKEN>
GET /sub/<name>?token=<TOKEN>
Authorization: Bearer <TOKEN>
```

`/subs` returns all subscriptions allowed by that token. `/sub/<name>` returns only the named upstream subscription.

Tokens can either:

- access all current and future subscriptions;
- access only selected subscriptions.

## Quick start

```bash
git clone https://github.com/Dark-V/SubMux.git
cd SubMux
# edit ADMIN_TOKEN in compose.yaml first
docker compose up -d --build
```

Open `http://HOST:8080/`, sign in with `ADMIN_TOKEN`, then add a subscription.

Example source:

```text
Name: superpupervpn
URL: https://provider.example/subscription/...
User-Agent: v2raytun/android
HWID: YOUR_DEVICE_HWID
```

The public URLs will look like:

```text
http://HOST:8080/1fdsf3fhgds3d.../subs
http://HOST:8080/1fdsf3fhgds3d.../sub/superpupervpn
```

## Docker

Published images are built for `linux/amd64` and `linux/arm64`:

```text
ghcr.io/dark-v/submux:latest
```

Minimal compose:

```yaml
services:
  submux:
    image: ghcr.io/dark-v/submux:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      ADMIN_TOKEN: "replace-with-a-long-random-value"
    volumes:
      - submux-data:/data

volumes:
  submux-data:
```

If `ADMIN_TOKEN` is omitted, SubMux generates one, stores it in `/data/admin_token`, and prints it once to the container log.

## Legacy env import

For migration from the original single-upstream container, SubMux understands these environment variables on an empty database:

```yaml
environment:
  UPSTREAM_URL: "https://provider.example/subscription/..."
  INITIAL_SUB_NAME: "main"
  USER_AGENT: "v2raytun/android"
  HWID: "YOUR_HWID"
  DEVICE_OS: "Android"
  VER_OS: "Android 15"
  DEVICE_MODEL: "Device model"
  APP_VERSION: "5.23.74"
```

They are imported into SQLite on first start. Do not commit real subscription URLs or HWIDs to Git.

## Merge behavior

For a named endpoint, SubMux returns that upstream body unchanged and forwards the useful subscription headers.

For `/subs`, upstreams are fetched concurrently. Base64 URI-list subscriptions are decoded, deduplicated by line, merged, and encoded back to base64. Raw text sources are concatenated as text. If formats are mixed, the aggregate output is raw text. Use `format=base64` or `format=raw` in the panel if auto-detection is not appropriate.

`Subscription-Userinfo` is combined where available. `upload` and `download` are summed, finite totals are summed, `total=0` remains unlimited, and the earliest non-zero expiry is used.

## Security notes

A subscription token is a bearer credential. Path/query tokens are supported because many proxy clients only accept URL-based credentials, but they can appear in reverse-proxy access logs and browser history. `Authorization: Bearer` is also supported when the client allows it.

The admin panel is protected separately by `ADMIN_TOKEN`. With HTTPS behind a reverse proxy, set `COOKIE_SECURE=1`.

## Build locally

```bash
docker build -t submux:local .
docker run --rm -p 8080:8080 -e ADMIN_TOKEN=test -v submux-data:/data submux:local
```

## License

MIT
