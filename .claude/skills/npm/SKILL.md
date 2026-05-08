---
name: npm
description: Use when managing nginx-proxy-manager (NPM) on the singapore host (217.15.166.15) — adding/listing/deleting proxy hosts, issuing or renewing Let's Encrypt certs (Cloudflare DNS-01), wiring new containers onto the `proxy` Docker network, or troubleshooting 502/404/redirect-loop/websocket issues. Triggers nginx proxy manager, NPM, proxy host, reverse proxy, tatapai.site cert, wildcard cert, cloudflare dns-01, jc21/nginx-proxy-manager.
---

# Nginx Proxy Manager on `singapore`

NPM is the TLS terminator + reverse proxy for everything on `217.15.166.15`. It's a single `jc21/nginx-proxy-manager` container with state in `/opt/stacks/npm/{data,letsencrypt}`. Stack root is mirrored locally at `related-services/npm/`.

## Endpoints

| What | URL |
|------|-----|
| Admin UI (direct, IP)     | `http://217.15.166.15:81` |
| Admin UI (proxied, HTTPS) | `https://nginx.tatapai.site` |
| HTTP listener             | `http://217.15.166.15:80` → 308 to HTTPS for known hosts |
| HTTPS listener            | `https://217.15.166.15:443` |
| REST API base             | same as admin UI, `/api/...` |

Credentials live in `npm/.env`:

| Var | Use |
|-----|-----|
| `NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` | login + `POST /api/tokens` |
| `CLOUDFLARE_DNS_API_TOKEN` | DNS-01 challenge (`Zone:Zone:Read` + `Zone:DNS:Edit` on `tatapai.site`) |

NPM does **not** read `.env` itself — values are entered via the UI. The file is the source-of-truth backup.

## Network model

NPM and every backend share the external Docker network `proxy`. Backends are reachable by **container name** (`forward_host: outline`, `forward_port: 3000`). Do **not** publish backend ports on the host — keep them on `expose:` only.

New service compose snippet:

```yaml
services:
  app:
    networks: [proxy, internal]
    expose: ["3000"]
networks:
  proxy:
    external: true
    name: proxy
  internal:
    driver: bridge
```

## REST API

### Token

```bash
TOKEN=$(curl -s -X POST http://localhost:81/api/tokens \
  -H 'Content-Type: application/json' \
  -d "{\"identity\":\"$NPM_ADMIN_EMAIL\",\"secret\":\"$NPM_ADMIN_PASSWORD\"}" \
  | jq -r .token)
```

JWT, 1-day TTL. Renew via `GET /api/tokens` with the same header.

When operating from a workstation (not on the singapore host), tunnel first: `ssh -L 81:localhost:81 singapore` and use `http://localhost:81`.

### Issue wildcard cert (Cloudflare DNS-01)

```bash
curl -s -X POST http://localhost:81/api/nginx/certificates \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "provider": "letsencrypt",
    "nice_name": "tatapai.site wildcard",
    "domain_names": ["tatapai.site", "*.tatapai.site"],
    "meta": {
      "dns_challenge": true,
      "dns_provider": "cloudflare",
      "dns_provider_credentials": "# Cloudflare API token\ndns_cloudflare_api_token = '"$CLOUDFLARE_DNS_API_TOKEN"'\n",
      "propagation_seconds": 30
    }
  }'
```

Schema gotchas (NPM v2.13+):

- `meta` uses `additionalProperties: false`. Allowed keys are exactly: `certificate`, `certificate_key`, `dns_challenge`, `dns_provider`, `dns_provider_credentials`, `letsencrypt_certificate`, `propagation_seconds`, `key_type`. **Do not** send `letsencrypt_email` or `letsencrypt_agree` — both are silently rejected by AJV with a misleading "must NOT have additional properties" message.
- Bumping `propagation_seconds` past 30s is the right knob if Cloudflare records aren't visible in time.

### Add a proxy host

```bash
curl -s -X POST http://localhost:81/api/nginx/proxy-hosts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "domain_names": ["app.tatapai.site"],
    "forward_scheme": "http",
    "forward_host": "app",
    "forward_port": 3000,
    "block_exploits": true,
    "allow_websocket_upgrade": true,
    "access_list_id": "0",
    "certificate_id": 1,
    "ssl_forced": true,
    "http2_support": true,
    "hsts_enabled": false,
    "hsts_subdomains": false,
    "caching_enabled": false,
    "advanced_config": "",
    "meta": {"letsencrypt_agree": false, "dns_challenge": false},
    "locations": []
  }'
```

`access_list_id` must be a string `"0"` (not number) — another schema quirk.

### List / inspect / delete / renew

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:81/api/nginx/proxy-hosts | jq
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:81/api/nginx/certificates | jq
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:81/api/nginx/proxy-hosts/<id>
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://localhost:81/api/nginx/certificates/<id>/renew
```

NPM auto-renews 30 days before expiry via container cron — manual renew is only needed when a credential changes.

## Web UI flow (the normal way)

1. **SSL Certificates → Add → Let's Encrypt** (DNS Challenge → Cloudflare, paste token). Wildcards (`*.tatapai.site` + `tatapai.site`) cover every subdomain you'll add later.
2. **Hosts → Proxy Hosts → Add**:
   - Domain Names: e.g. `app.tatapai.site`
   - Scheme: `http` (NPM does TLS, backend speaks plain HTTP)
   - Forward Hostname/IP: container name (e.g. `outline`)
   - Forward Port: container port
   - **Block Common Exploits**: on
   - **Websockets Support**: on (anything realtime — Outline, Grafana, Code-Server, …)
   - **SSL** tab: pick the wildcard cert, **Force SSL**, **HTTP/2 Support**, optionally HSTS.
3. Cloudflare DNS: add `A` → `217.15.166.15`. If proxied (orange cloud), set zone SSL/TLS mode = **Full (strict)** so the wildcard cert is honored.

## Current state (as of 2026-05-07)

```
Cert id=1     *.tatapai.site, tatapai.site   provider=letsencrypt   expires 2026-08-05
ProxyHost id=1   nginx.tatapai.site   -> npm:81       (this admin UI)
ProxyHost id=2   outlook.tatapai.site -> outline:3000 (Outline wiki)
```

Keep these Cloudflare records (or a single wildcard `*.tatapai.site A 217.15.166.15`):

```
nginx.tatapai.site    A   217.15.166.15
outlook.tatapai.site  A   217.15.166.15
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `502 Bad Gateway` from NPM | backend container not on the `proxy` network, or wrong forward host/port | `docker inspect <c> -f '{{json .NetworkSettings.Networks}}'`; ensure container is on `proxy` and uses the right port |
| `404` from NPM with default page | no proxy host matches the requested Host header | check `domain_names`, DNS A record, Cloudflare proxy mode |
| Cert request fails with `dns_problem` | Cloudflare token missing zone scope | token must have `Zone:Zone:Read` + `Zone:DNS:Edit` for `tatapai.site` |
| `must NOT have additional properties` on `POST /api/nginx/certificates` | extra `meta` keys (e.g. `letsencrypt_email`) | use only the allowed `meta` keys above |
| App shows redirect loop | app forces HTTPS internally AND NPM also forces SSL | turn off `FORCE_HTTPS` in the backend (TLS terminates at NPM) |
| Websocket disconnects | "Websockets Support" off on the proxy host | toggle it on (re-renders nginx conf instantly) |

## Logs

```bash
docker logs -f npm
ls /opt/stacks/npm/data/logs/                        # per-host access/error logs
docker exec npm cat /data/logs/proxy-host-2_access.log | tail
```

## Backup / restore

```bash
# Backup
ssh singapore 'tar czf /tmp/npm-backup-$(date +%F).tgz -C /opt/stacks/npm data letsencrypt'
scp singapore:/tmp/npm-backup-*.tgz ./

# Restore on a fresh host
scp npm-backup-*.tgz singapore:/tmp/
ssh singapore 'cd /opt/stacks/npm && tar xzf /tmp/npm-backup-*.tgz && docker compose up -d'
```

## Adding a new service end-to-end

1. Compose with `networks: [proxy, internal]`, `expose: ["<port>"]`, external `proxy` network at the bottom.
2. `docker compose up -d`.
3. Cloudflare DNS A record `<sub>.tatapai.site → 217.15.166.15`.
4. NPM proxy host (UI or API): `<sub>.tatapai.site` → `<container>:<port>`, attach cert id 1, force SSL, enable websockets.
5. Hit `https://<sub>.tatapai.site` and watch `docker logs -f <container>` to confirm requests land.
6. Document the new service as a child page under 资源笔记 in Outline (use the `outline` skill).
