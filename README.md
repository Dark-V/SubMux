# SubMux

Self-hosted proxy subscription multiplexer with a web panel.

SubMux can combine several upstream subscriptions into one token-protected URL, cache them, remove duplicate proxy nodes, and keep the admin panel separate from the public subscription listener.

## Features

- multiple upstream subscriptions;
- per-source `User-Agent`, HWID and device headers;
- token bundles with selected sources;
- VLESS / VMess / Trojan / SS URI-list merge;
- semantic deduplication of the same endpoint;
- filters service placeholder nodes with all-zero credentials;
- persistent 30-minute cache by default;
- manual refresh from the web panel or `?forceUpdate=1`;
- stale-cache fallback when an upstream is temporarily unavailable;
- separate admin and public ports.

## Docker Compose

```yaml
services:
  submux:
    image: ghcr.io/dark-v/submux:latest
    container_name: submux
    restart: unless-stopped
    pull_policy: always
    init: true

    ports:
      # Admin panel
      - "81:8080"

      # Public subscription listener / reverse-proxy backend
      - "8081:8081"

    environment:
      ADMIN_PORT: "8080"
      PUBLIC_PORT: "8081"

      # Optional external URL used by Copy URL buttons.
      # Example: https://sub.example.com
      PUBLIC_BASE_URL: ""

      # Leave empty to auto-generate and persist a token in /data/admin_token.
      ADMIN_TOKEN: ""

      # Use 0 for a LAN HTTP admin panel.
      COOKIE_SECURE: "0"

      # Cache TTL in seconds. 1800 = 30 minutes.
      CACHE_TTL_SECONDS: "1800"

    volumes:
      - submux-data:/data

    security_opt:
      - no-new-privileges:true

volumes:
  submux-data:
```

Start:

```bash
docker compose pull
docker compose up -d
```

Admin panel:

```text
http://HOST:81/
```

If `ADMIN_TOKEN` is empty:

```bash
docker exec submux cat /data/admin_token
```

Public listener:

```text
http://HOST:8081/
```

For a reverse proxy, point it only to `HOST:8081`. The public listener does not expose `/admin`, `/login` or `/api/*`.

## Subscription URLs

```text
/TOKEN/
/TOKEN/subs
/TOKEN/sub/NAME
/subs?token=TOKEN
/sub/NAME?token=TOKEN
```

Force an immediate refresh:

```text
/TOKEN/?forceUpdate=1
```

Normal requests use the persistent cache in `/data/submux.db`.

## Merge behavior

Aggregate URLs merge URI-list subscriptions and remove duplicate endpoints. Different UUIDs or display names do not create duplicates when the actual endpoint and transport are the same.

The same display name on different IPs remains separate.

Service entries that use an all-zero credential, for example:

```text
00000000-0000-0000-0000-000000000000
```

are filtered from aggregate subscriptions.

Named URLs such as `/TOKEN/sub/NAME` remain pass-through and return the original upstream subscription.

YAML/JSON configs are not merged; use the named pass-through URL for them.

## Image

```text
ghcr.io/dark-v/submux:latest
```

Architectures: `linux/amd64`, `linux/arm64`.

## License

MIT
