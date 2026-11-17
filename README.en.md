# mihomo-keeper

This fork of [adevjoe/mihomo-keeper](https://github.com/adevjoe/mihomo-keeper) adds token auto-login and Bearer API authentication.

A lightweight web console for operating [mihomo](https://github.com/MetaCubeX/mihomo) (Clash.Meta core) on a Linux server with systemd:
**subscription update + local override merge, rule editing, config backup/rollback, service control, logs, kernel and GeoIP database updates.**

- Single-file backend (`manager.py`): Python 3.8+ standard library + PyYAML, no frontend build step
- Complements (does not replace) dashboards such as [metacubexd](https://github.com/MetaCubeX/metacubexd)
- The UI is in Chinese

[中文说明](README.md)

## Features

- **Status**: service state, kernel version, config/override mtime, backup count, masked subscription URL; update subscription, hot reload / restart / validate config; link to the metacubexd dashboard
- **Rules**: form editor for `rules-prepend` / `rules-append` in `override.yaml`, with validation; only the relevant YAML block is rewritten so comments are kept
- **Proxy groups**: view and switch selectors via the mihomo external controller
- **override**: edit `override.yaml`; "validate" downloads the subscription, merges, runs `mihomo -t` and shows a diff against the current `config.yaml` without writing anything
- **config**: read-only view of the effective `config.yaml`
- **Update**: install mihomo releases from GitHub (picks the amd64 v1/v2/v3 variant from CPU flags, validates, backs up, restarts, auto-rollback on failure); update `geoip.metadb` / `ASN.mmdb` from MetaCubeX/meta-rules-dat with rollback
- **Backups**: diff, validated restore and deletion of `backup/config.yaml.*`
- **Logs**: `journalctl -u mihomo` viewer with filtering and auto refresh

Merge semantics of `merge-config.py` (deep merge, glob-patched `proxy-groups`, `rules-prepend` / `rules-append` / `rules-insert`, `null` deletes a key, `x-*` keys are local) are documented in [README.md](README.md) and [`override.example.yaml`](override.example.yaml).

## Install

Requirements: Linux + systemd, root, Python 3.8+, PyYAML (`apt install python3-yaml`), curl, a mihomo binary (default `/usr/local/bin/mihomo`).

```bash
git clone https://github.com/ltinyho/mihomo-keeper.git && cd mihomo-keeper
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
`MIHOMO_DIR`, `MIHOMO_BIN`, `MIHOMO_SERVICE`, `MANAGER_LISTEN` (default `0.0.0.0:9092`), `MANAGER_USER`, `MANAGER_PASSWORD`, `MANAGER_SECRET_FILE`, `MIHOMO_SUB_URL`, `MIHOMO_SUB_URL_FILE`, `MERGE_SCRIPT`, `UPDATE_SCRIPT`, `BACKUP_KEEP`.

Token settings: `MANAGER_TOKEN` (takes precedence over the file), `MANAGER_TOKEN_FILE` (default `$MIHOMO_DIR/manager.token`), `MANAGER_SESSION_MAX_AGE` (positive seconds, default `604800`, or seven days).

## Security

- It listens on `0.0.0.0:9092` by default and runs as root (it can rewrite configs, restart services and replace the mihomo binary). **Always set a strong password or token**; without either it runs **without authentication**.
- HTTP Basic auth is not encrypted. Use it only on a trusted LAN, or bind to `127.0.0.1` and put it behind a TLS reverse proxy / VPN. Never expose it directly to the Internet.
- Keep `subscription.url`, `manager.secret`, `manager.token`, `override.yaml`, `config.yaml` and `backup/` private.

## License

[MIT](LICENSE). Not affiliated with MetaCubeX.
