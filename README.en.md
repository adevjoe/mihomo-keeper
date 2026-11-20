# mihomo-keeper

A lightweight web console for [mihomo](https://github.com/MetaCubeX/mihomo) (Clash.Meta core) on a Linux server with systemd:
**a built-in dashboard (live traffic, proxy switching and latency tests, connections, rules, live logs, core settings) plus subscription update + local override merge, rule editing, config backup/rollback, service control, kernel and GeoIP database updates.**

- Single-file backend (`manager.py`): Python 3.8+ standard library + PyYAML, no frontend build step
- Built-in equivalent of the everyday [metacubexd](https://github.com/MetaCubeX/metacubexd) dashboard features, so no separate dashboard is needed; the browser only talks to the manager and never sees the controller secret
- The UI is in Chinese

[中文说明](README.md)

## Features

- **Overview**: service state, kernel version, config/override mtime, backup count, masked subscription URL; live upload/download speed, totals, active connections, memory and 60-second charts; proxy mode switch (rule / global / direct); update subscription, hot reload / restart / validate config
- **Proxies**: groups with their nodes, click to switch (Selector, plus fixed selection for URLTest / Fallback); per-node, per-group and test-all latency checks with colour coding; search, sort by latency or name, hide timed-out nodes; configurable test URL / timeout, optional closing of old connections after switching; proxy providers with subscription usage / expiry, update and health check
- **Connections**: live table (host, type, rule, chain, speed, traffic, source, process, duration) with sorting, filtering, pause, details, closing one / filtered / all connections, and recently closed connections
- **Rules**: effective rules of the running core (filterable) and rule providers with update
- **Logs**: live core log stream with level selection, filter highlighting and pause; `journalctl -u mihomo` viewer with filtering and auto refresh
- **Custom rules**: form editor for `rules-prepend` / `rules-append` in `override.yaml`, with validation; only the relevant YAML block is rewritten so comments are kept
- **Core settings**: runtime changes to mode, log level, ports, allow-lan, IPv6, sniffing, TCP concurrency, TUN enable / stack, outbound interface; flush FakeIP / DNS cache; DNS lookup (values revert to `config.yaml` on reload / subscription update / restart)
- **override**: edit `override.yaml`; "validate" downloads the subscription, merges, runs `mihomo -t` and shows a diff against the current `config.yaml` without writing anything
- **config**: read-only view of the effective `config.yaml`
- **Update**: install mihomo releases from GitHub (picks the amd64 v1/v2/v3 variant from CPU flags, validates, backs up, restarts, auto-rollback on failure); update `geoip.metadb` / `ASN.mmdb` from MetaCubeX/meta-rules-dat with rollback
- **Backups**: diff, validated restore and deletion of `backup/config.yaml.*`

Dashboard features go through the manager backend to the mihomo external controller (`/api/mihomo/*`, live data as NDJSON streams), using the manager's authentication; the backend adds the controller secret. Only the endpoints the dashboard needs are allowed (no `/restart`, `/upgrade`, `/debug`). The `external-controller` can therefore listen on `127.0.0.1` only, and `external-ui` is no longer needed.

Merge semantics of `merge-config.py` (deep merge, glob-patched `proxy-groups`, `rules-prepend` / `rules-append` / `rules-insert`, `null` deletes a key, `x-*` keys are local) are documented in [README.md](README.md) and [`override.example.yaml`](override.example.yaml).

## Install

Requirements: Linux + systemd, root, Python 3.8+, PyYAML (`apt install python3-yaml`), curl, a mihomo binary (default `/usr/local/bin/mihomo`).

```bash
git clone https://github.com/adevjoe/mihomo-keeper.git && cd mihomo-keeper
sudo MIHOMO_SUB_URL='https://your-subscription-link' ./deploy/install.sh [--enable-timer] [--no-start]
```

The installer copies the scripts to `/etc/mihomo`, creates `override.yaml`, `subscription.url`, a random `manager.secret` and an independent `manager.token` (credentials printed once) if they do not exist, installs the systemd units and starts `mihomo-manager.service`. Then open `http://<server>:9092` (user `admin`).

## Token auto-login

Open `http://<server>:9092/login#token=<token>` using the value in `/etc/mihomo/manager.token`. The login page removes the fragment from the address bar, submits the token as JSON and opens the console with an HttpOnly, SameSite=Strict session cookie. Sessions last seven days by default and survive service restarts. The fragment is never sent in the page request.

`http://<server>:9092/?token=<token>` also works: the server sets the session cookie and immediately redirects to `/`. Query strings are redacted in manager access logs. Prefer fragment links to keep tokens out of reverse proxy logs.

APIs accept `Authorization: Bearer <token>`. Existing HTTP Basic authentication remains available. `POST /api/auth/logout` with a JSON body of `{}` clears the session cookie; it does not clear cached Basic credentials.

On an existing manual deployment, create `/etc/mihomo/manager.token` with a random value (`secrets.token_urlsafe(32)`), set permissions to `600` and restart `mihomo-manager.service`. The token is independent of both the password and the mihomo controller secret. Replacing it and restarting the manager revokes all previous tokens and sessions. If `MANAGER_TOKEN` is set, rotate that environment variable instead. Treat auto-login links as passwords.

## Configuration

Environment variables, typically in `/etc/default/mihomo-manager` (see [`deploy/manager.env.example`](deploy/manager.env.example)):
`MIHOMO_DIR`, `MIHOMO_BIN`, `MIHOMO_SERVICE`, `MANAGER_LISTEN` (default `0.0.0.0:9092`), `MANAGER_USER`, `MANAGER_PASSWORD`, `MANAGER_SECRET_FILE`, `MIHOMO_SUB_URL`, `MIHOMO_SUB_URL_FILE`, `MERGE_SCRIPT`, `UPDATE_SCRIPT`, `BACKUP_KEEP`, `MIHOMO_APPLY_MODE`, `MANAGER_DASHBOARD_URL`, `MANAGER_DASHBOARD_NAME`.

### How a subscription update is applied (`MIHOMO_APPLY_MODE`)

Default `reload`: after the new `config.yaml` is validated and put in place, `update-subscription.sh` asks the running mihomo to load it through `PUT /configs?force=false` instead of restarting the process.

This matters on hosts that run `tun`. `systemctl restart` destroys and recreates the tun device, so the routes and policy rules installed by `tun.auto-route` are rebuilt, and other tunnels on the same host see that as a link change. Tailscale, for instance, logs `LinkChange: major, rebinding` and resets every peer path, dropping the connections it was forwarding (exit node / subnet router) — with hourly subscription updates that is an hourly outage. A hot reload leaves the tun device alone.

If the reload cannot be done (controller unreachable, service not running, …) the script falls back to `systemctl restart`, and logs which of the two actually applied the config. Set `MIHOMO_APPLY_MODE=restart` to keep the previous behaviour.

An optional external dashboard link in the header is hidden unless `MANAGER_DASHBOARD_URL` is set. It is a template supporting `{host}` (hostname used by the browser), `{port}` (controller port) and `{secret}` (controller secret), all URL-encoded, e.g.
`MANAGER_DASHBOARD_URL='http://{host}:{port}/ui/#/setup?hostname={host}&port={port}&secret={secret}'`.
If the template contains `{secret}`, the secret is sent to the (authenticated) browser.

Token settings: `MANAGER_TOKEN` (takes precedence over the file), `MANAGER_TOKEN_FILE` (default `$MIHOMO_DIR/manager.token`), `MANAGER_SESSION_MAX_AGE` (positive seconds, default `604800`, or seven days).

## Security

- It listens on `0.0.0.0:9092` by default and runs as root (it can rewrite configs, restart services and replace the mihomo binary). **Always set a strong password or token**; without either it runs **without authentication**.
- HTTP Basic auth is not encrypted. Use it only on a trusted LAN, or bind to `127.0.0.1` and put it behind a TLS reverse proxy / VPN. Never expose it directly to the Internet.
- Keep `subscription.url`, `manager.secret`, `manager.token`, `override.yaml`, `config.yaml` and `backup/` private.
- A logged-in manager session can switch proxies, close connections and change runtime settings through the built-in dashboard; treat the manager password as sensitive as the controller secret.
- Behind Nginx or another reverse proxy, live data uses long-lived streaming responses (sent with `X-Accel-Buffering: no`); do not enable response buffering and keep read timeouts at 60 seconds or more.

## License

[MIT](LICENSE). Not affiliated with MetaCubeX.
