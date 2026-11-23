# SubMux

Self-hosted proxy subscription multiplexer with a web panel.

SubMux can combine several upstream subscriptions into one token-protected URL, cache them, remove duplicate proxy nodes, and keep the admin panel separate from the public subscription listener.

## Features

- multiple upstream subscriptions;
- per-source `User-Agent`, HWID and device headers;
- token bundles with selected sources;
- VLESS / VMess / Trojan / SS URI-list merge;
- full Xray JSON subscription adapter for Throne, preserving routing and balancers;
- semantic deduplication of the same endpoint;
- filters service placeholder nodes with all-zero credentials;
- persistent cache with a live-configurable TTL (30 minutes by default);
- manual refresh from the web panel with visible request timing/status;
- forced public refresh aliases: `?force=1`, `?update=1`, `?forceUpdate=1`, `?refresh=1`, `?reload=1`;
- compatibility burst refresh: 3 requests from the same token + client IP within 60 seconds force an upstream refresh;
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
      # Can be changed later in the admin panel.
      CACHE_TTL_SECONDS: "1800"

      # Repeated-client compatibility refresh.
      BURST_REFRESH_REQUESTS: "3"
      BURST_REFRESH_WINDOW_SECONDS: "60"

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

Force an immediate refresh (aliases are equivalent):

```text
/TOKEN/?force=1
/TOKEN/?update=1
/TOKEN/?forceUpdate=1
/TOKEN/?refresh=1
/TOKEN/?reload=1
```

Responses expose cache state through `X-SubMux-Cache`, `X-SubMux-Cache-Age`,
`X-SubMux-Cache-TTL`, `X-SubMux-Cache-Expires-In`, and
`X-SubMux-Refresh-Reason`.

If the same token and client IP requests a subscription 3 times within 60 seconds,
SubMux treats the third request as an explicit refresh. The counter then resets.

Normal requests use the persistent cache in `/data/submux.db`.

## Merge behavior

Aggregate URLs merge URI-list subscriptions and remove duplicate endpoints. Different UUIDs or display names do not create duplicates when the actual endpoint and transport are the same.

The same display name on different IPs remains separate.

Service entries that use an all-zero credential, for example:

```text
00000000-0000-0000-0000-000000000000
```

are filtered from aggregate subscriptions.

Named URLs such as `/TOKEN/sub/NAME` return the original upstream subscription for `auto`, `base64` and `raw` sources. Sources set to `throne` return converted Throne profiles on both named and aggregate URLs.

Other YAML/JSON configs are not merged; use a named pass-through URL for them.

## Throne adapter

Some providers return full Xray configurations to Happ or INCY and a different list of links to other clients. SubMux can fetch the full configurations and expose them as a subscription for the latest Throne release.

In the source settings:

1. Enter the provider's subscription URL.
2. Set `User-Agent` to `Happ/3.6.0/Android`, or the value your provider requires for full Xray JSON.
3. Keep the HWID already registered for that subscription.
4. Select **Throne (Xray JSON)** and save, then refresh the source.
5. Add the existing SubMux bundle URL or `/TOKEN/sub/NAME` URL to Throne.

The API value for this source format is `"throne"`. It accepts one full Xray JSON object or an array of configurations, as plain text or base64. SubMux wraps each configuration as a full Xray profile for Throne. It preserves outbounds, proxy chains, routing, DNS and balancers; Throne creates the local inbound. Service placeholder configurations with all-zero credentials are filtered out.

Profiles with routing rules that distinguish the original listeners are rejected because Throne replaces those listeners with one bridge.

Converted profiles can share a bundle with ordinary URI-list sources. Each full configuration stays together, so a country's fallback servers and automatic selection remain part of one profile. Provider routing also stays active: its `direct` rules send matching traffic outside the proxy.

The admin panel's **RAW** button returns the original upstream response byte-for-byte, including for Throne sources.

## Image

```text
ghcr.io/dark-v/submux:latest
```

Architectures: `linux/amd64`, `linux/arm64`.

## License

MIT
