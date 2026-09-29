# SubMux

SubMux is a small self-hosted subscription multiplexer. It fetches one or more upstream proxy subscriptions with per-source `User-Agent`, `X-HWID` and optional device headers, then exposes them through token-protected endpoints.

The web panel includes light/dark themes, subscription management, upstream tests and access-token ACLs.

## Endpoints

Every public subscription request requires a token.

```text
GET /<TOKEN>/
GET /<TOKEN>/?forceUpdate=1
GET /<TOKEN>/subs
GET /<TOKEN>/sub/<name>
GET /subs?token=<TOKEN>
GET /sub/<name>?token=<TOKEN>
Authorization: Bearer <TOKEN>
```

`/<TOKEN>/` and `/subs` return all subscriptions allowed by that token. `/sub/<name>` returns only the named upstream subscription. Add `?forceUpdate=1` to an aggregate or named URL to bypass the cache and refresh the required upstream source(s) immediately.

Tokens can either:

- access all current and future subscriptions;
- access only selected subscriptions.

## Quick start

```bash
git clone https://github.com/Dark-V/SubMux.git
cd SubMux
# optional: cp .env.example .env and set ADMIN_TOKEN
docker compose pull
docker compose up -d
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
http://HOST:8080/1fdsf3fhgds3d.../
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
      CACHE_TTL_SECONDS: "1800"
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

For aggregate endpoints, upstreams are fetched concurrently when the cache needs refresh. SubMux safely merges URI-list subscriptions (for example VLESS/VMess/Trojan/SS links) in either raw or base64 form. It decodes base64 lists, removes semantic duplicates, and re-encodes the result when every source is base64.

Duplicate identity ignores URI credentials/userinfo (for example different VLESS UUIDs) and the display fragment after `#`, while preserving scheme, host/IP, port, path and transport/security query parameters. Therefore the same endpoint from two accounts collapses to the first entry in bundle order, but the same display name on different IPs remains two distinct profiles.

Arbitrary YAML/JSON configs are intentionally **not** line-merged because that would corrupt them. They remain available through the named pass-through endpoint `/sub/<name>`. Use the per-source format setting only to disambiguate URI-list encoding.

`Subscription-Userinfo` is combined where available. `upload` and `download` are summed, finite totals are summed, `total=0` remains unlimited, and the earliest non-zero expiry is used.

## Cache behavior

SubMux keeps both per-source and per-bundle caches persistently in `/data/submux.db`. The default TTL is 1800 seconds (30 minutes) and can be changed with `CACHE_TTL_SECONDS`.

A normal request returns the cached bundle without contacting upstream while it is fresh. A background worker checks for expired bundles once per minute and refreshes them. Overlapping bundles reuse fresh per-source cache entries, so a source shared by several tokens is not fetched repeatedly during the same refresh window.

`?forceUpdate=1` bypasses the TTL for that request, re-fetches every source required by the selected bundle, updates source cache, rebuilds the bundle and stores the new bundle cache.

If a source refresh fails but a previous good source cache exists, SubMux uses that stale copy and marks the response with `X-SubMux-Stale`. If an entire refresh fails but a previous bundle for the same configuration exists, that bundle is served with `X-SubMux-Cache: STALE`.

Useful response headers:

```text
X-SubMux-Cache: HIT | REFRESH | STALE | ERROR
X-SubMux-Cache-Age: <seconds>
X-SubMux-Stale: <source names, if any>
```

## Security notes

A subscription token is a bearer credential. Path/query tokens are supported because many proxy clients only accept URL-based credentials, but they can appear in reverse-proxy access logs and browser history. `Authorization: Bearer` is also supported when the client allows it.

The admin panel is protected separately by `ADMIN_TOKEN`. With HTTPS behind a reverse proxy, set `COOKIE_SECURE=1`.

## Build locally

```bash
docker build -t submux:local .
docker run --rm -p 8080:8080 -e ADMIN_TOKEN=test -v submux-data:/data submux:local
```

Every push to `main` also runs a runtime smoke-test before the multi-architecture image is published. The smoke-test boots the container, checks the admin login/API, adds two mock upstreams, verifies aggregate output, verifies a subscription-scoped token, and only then pushes the image.

## License

MIT
