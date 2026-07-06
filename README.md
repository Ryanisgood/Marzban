# MarzbanX

Controller-first proxy management panel based on [Gozargah/Marzban](https://github.com/Gozargah/Marzban), with focused work on node provisioning, panel-managed node protocols, HY2/sing-box support, and Rust node diagnostics.

> Runtime compatibility note: this fork still keeps many upstream runtime names such as `marzban`, `marzban-cli`, `/var/lib/marzban`, and the existing service names for upgrade compatibility. The public project name is MarzbanX.

## Languages

[English](./README.md) / [简体中文](./README-zh-cn.md) / [فارسی](./README-fa.md) / [Русский](./README-ru.md)

## Project Links

- Current fork: [Ryanisgood/MarzbanX](https://github.com/Ryanisgood/MarzbanX)
- Original upstream: [Gozargah/Marzban](https://github.com/Gozargah/Marzban)
- Rust node runtime: [MarzbanX-node](../MarzbanX-node)
- Node provisioning notes: [docs/node-provisioning.md](./docs/node-provisioning.md)
- CLI notes: [cli/README.md](./cli/README.md)

## What MarzbanX Changes

MarzbanX keeps the Marzban controller model, REST API, dashboard, subscription templates, user management, Telegram bot, and CLI, then extends the node workflow so node protocol selection is owned by the panel instead of by manual SSH edits.

Recent changes in this fork focus on:

- panel-managed node inbound selection through `node.active_inbounds`;
- node-owned inbound rows through `inbounds.owner_node_id`, so a panel-managed node can only select inbounds that belong to that node;
- Add Node provisioning that generates inbounds, hosts, a panel-mode node, and a one-time install command;
- deterministic core selection: sing-box for sing-box-only protocols, Xray for Xray-compatible selections;
- Rust node support for controller-managed inbounds;
- runtime status reporting, including node version, installed cores, current core, memory, listening ports, configured inbound ports, and last restart time;
- HY2/Hysteria2 and AnyTLS user changes that rebuild sing-box config and restart only affected nodes where possible;
- safer provisioning lifecycle with validation, rollback, token retry behavior, and port conflict checks.

## Features

- Web dashboard and REST API for users, admins, nodes, hosts, and subscriptions.
- Multi-node deployment through the Rust [MarzbanX-node](../MarzbanX-node) runtime.
- Panel-managed node mode: the controller sends selected inbound tags to the node, so new nodes do not need manual `INBOUNDS` editing.
- Add Node wizard that creates generated inbounds and shows a one-command installer:

```bash
curl -fsSL https://controller.example.com/api/node/install.sh | sudo bash -s -- --token xxx
```

- Supported provisioning templates in this fork: HY2/Hysteria2, AnyTLS, VLESS TCP REALITY, and Shadowsocks TCP.
- Core policy visibility: expected core, actual core, reason, Xray API availability, and whether restart is required.
- Node diagnostics: installed Xray/sing-box versions, local sockets, configured inbound ports, memory use, node version, and restart timestamp.
- Traffic and expiry limits, periodic traffic reset, multi-user and multi-protocol account support.
- Subscription output for V2Ray-compatible clients, sing-box, Clash, Clash Meta, and related clients.
- TLS and REALITY support through the inherited Marzban configuration model.
- Telegram bot, webhook notifications, backup helpers, and `marzban-cli`.

## Installation

MarzbanX can be installed in two practical ways today:

- build a local Docker image from this repository;
- run from source with a systemd service.

The checked-in `docker-compose.yml` still points to the original upstream image for compatibility with the inherited project layout. Until MarzbanX images are published under the final repository name, prefer the local-build Docker flow below.

### Requirements

- A Linux server with root access.
- Python 3.12 for source installs, or Docker for container installs.
- Node.js 16+ only when rebuilding the dashboard assets.
- A domain and TLS certificate for public dashboard access.
- Open firewall ports for the dashboard/API and every proxy inbound you create.

### Docker: Local Image

Clone the repository and prepare the env file:

```bash
git clone https://github.com/Ryanisgood/MarzbanX.git
cd MarzbanX
cp .env.example .env
nano .env
```

Build the dashboard assets before building the image:

```bash
cd app/dashboard
npm ci
VITE_BASE_API=/api/ npm run build --if-present -- --outDir build --assetsDir statics
cp ./build/index.html ./build/404.html
cd ../..
```

Build and run the controller image:

```bash
docker build -t marzbanx:local .
docker run -d \
  --name marzbanx \
  --restart always \
  --network host \
  --env-file .env \
  -v /var/lib/marzban:/var/lib/marzban \
  marzbanx:local
```

Create the first sudo admin:

```bash
docker exec -it marzbanx python /code/marzban-cli.py admin create --sudo
```

Some packaged installs expose a `marzban cli` wrapper, but the source tree always includes `marzban-cli.py`.

The dashboard is available on the configured Uvicorn port, default:

```text
http://SERVER_IP:8000/dashboard/
```

For production, put Nginx, Caddy, or another reverse proxy with TLS in front of the controller.

### Source Install

Install Xray on the controller host:

```bash
bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
```

Clone and install Python dependencies:

```bash
git clone https://github.com/Ryanisgood/MarzbanX.git
cd MarzbanX
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools
python -m pip install -r requirements.txt
cp .env.example .env
nano .env
alembic upgrade head
```

Build dashboard assets:

```bash
cd app/dashboard
npm ci
VITE_BASE_API=/api/ npm run build --if-present -- --outDir build --assetsDir statics
cp ./build/index.html ./build/404.html
cd ../..
```

Start the controller:

```bash
python main.py
```

Create the first sudo admin from another shell in the same virtualenv:

```bash
. .venv/bin/activate
python marzban-cli.py admin create --sudo
```

To install a simple systemd service from the source checkout:

```bash
sudo ./install_service.sh
sudo systemctl enable --now marzban
```

The helper service keeps the inherited runtime name `marzban`. Check logs with:

```bash
sudo journalctl -u marzban -f
```

### Reverse Proxy Example

Example Nginx configuration for the dashboard and API:

```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name panel.example.com;

    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Set `XRAY_SUBSCRIPTION_URL_PREFIX=https://panel.example.com` in `.env` when subscription links must use the public domain.

## Node Provisioning Flow

The intended MarzbanX node flow is:

1. Open the dashboard node modal.
2. Use Add Node instead of the advanced manual form.
3. Enter node name, address, management ports, protocol selection, and public inbound ports.
4. The controller creates generated inbounds, matching hosts, a panel-managed node, and a short-lived install token.
5. Run the generated command on the new server.
6. The Rust node installs or uses the required core, starts `marzban-node.service`, and receives active inbound tags from the controller.

### Node-Owned Inbounds

MarzbanX no longer treats global inbound reuse as the normal node-management path. The Add Node wizard creates inbound rows owned by the new node. The dashboard then filters protocol choices to those owned inbounds, and the runtime validates the same rule before starting or restarting a node.

In practical terms:

- users are still managed by protocol, so enabling VLESS/HY2/SS for a user updates that protocol across the selected hosts;
- a node's runnable protocols come from that node's owned inbound rows;
- selecting another node's inbound is rejected for panel-managed nodes;
- unowned inbound rows are legacy/manual migration data and should not be used for normal MarzbanX Add Node flows;
- the Rust node's `INBOUNDS` environment variable is only for legacy/manual nodes. Panel-managed nodes receive active inbound tags from the controller.

When upgrading older data, generated tags in the form `node-{id}-{protocol}-{port}` are migration candidates for ownership backfill. If the dashboard shows an invalid selected inbound after upgrade, remove that selection and recreate the node protocol through Add Node, or assign `owner_node_id` only after confirming that inbound is truly dedicated to that node.

For production provisioning, configure these controller variables:

```env
MARZBAN_NODE_BINARY_URL=https://controller.example.com/downloads/marzban-node
SING_BOX_INSTALL_SCRIPT_URL=https://sing-box.app/install.sh
SING_BOX_VERSION=1.13.14
SING_BOX_DOWNLOAD_URL_TEMPLATE=https://github.com/SagerNet/sing-box/releases/download/v{version}/sing-box-{version}-linux-{arch}.tar.gz
XRAY_INSTALL_SCRIPT_URL=https://github.com/XTLS/Xray-install/raw/main/install-release.sh
```

`MARZBAN_NODE_BINARY_URL` should point to the Rust `MarzbanX-node` binary built for the target Linux architecture. `SING_BOX_VERSION` defaults to the pinned stable version `1.13.14`, and `SING_BOX_DOWNLOAD_URL_TEMPLATE` defaults to the official GitHub release archive pattern, so HY2 and AnyTLS Add Node provisioning can install sing-box without extra controller environment configuration. `SING_BOX_INSTALL_SCRIPT_URL` remains available as a fallback or mirror hook.

Before using the Add Node wizard in production:

1. Build or download the `MarzbanX-node` Linux binary.
2. Host it at the HTTPS URL configured in `MARZBAN_NODE_BINARY_URL`.
3. Override `SING_BOX_DOWNLOAD_URL_TEMPLATE` or `SING_BOX_INSTALL_SCRIPT_URL` only if the node cannot reach the default source or you use an internal reviewed mirror.
4. Make sure new nodes can reach the controller API over HTTPS.
5. Open `SERVICE_PORT/tcp`, default `62050`, from the controller to the node.
6. Open every selected public proxy port, for example `8443/udp` for HY2.

## Core Policy

MarzbanX starts one proxy core per node config:

- if active inbounds contain HY2/Hysteria2 or AnyTLS, the expected core is `sing-box`;
- otherwise the expected core is `xray`;
- Xray API is injected only for Xray-selected configs;
- sing-box nodes do not expose the Xray API, so usage collection skips Xray usage API calls for those nodes.

The dashboard exposes the decision instead of hiding it behind ports and logs:

- current core;
- expected core;
- reason, such as `INBOUNDS contains sing-box-only protocol`;
- Xray API availability;
- restart-required state;
- active inbound details with public ports and user counts.

## Protocol Switching

Panel-managed nodes can be edited from the dashboard by changing their active inbound selection. The controller validates:

- selected inbound tags exist;
- required hosts exist when needed;
- selected public ports do not conflict on the same bind/transport;
- the selected protocol set is supported by the required core;
- the node has reported whether the required core is installed.

VMess/Trojan provisioning and a fuller protocol-switching assistant remain roadmap items. The Rust node already contains sing-box translation support for several protocols, but the dashboard wizard currently exposes the provisioning templates listed above.

## HY2 And AnyTLS User Updates

HY2 and AnyTLS are config-reload protocols in this fork. Creating, editing, removing, or migrating users for these protocols rebuilds the sing-box config and restarts affected nodes. The dashboard warns that active connections can briefly interrupt.

## Dashboard Development

Run the dashboard dev server when working on frontend code:

```bash
cd app/dashboard
npm ci
npm run dev
```

## Configuration

Most upstream Marzban settings still apply. Important MarzbanX node-related settings:

| Variable | Description |
| --- | --- |
| `MARZBAN_NODE_BINARY_URL` | Download URL for the Rust node binary used by the one-command installer. |
| `SING_BOX_INSTALL_SCRIPT_URL` | Install script URL for sing-box nodes. Defaults to `https://sing-box.app/install.sh`. |
| `SING_BOX_VERSION` | Pinned sing-box version required by the one-command installer. Defaults to `1.13.14`. |
| `SING_BOX_DOWNLOAD_URL_TEMPLATE` | Release archive URL template used before the install script fallback. Supports `{version}` and `{arch}`. |
| `XRAY_INSTALL_SCRIPT_URL` | Install script URL for Xray nodes. Defaults to the upstream Xray installer. |
| `XRAY_JSON` | Controller core config file where generated inbounds are written. |
| `XRAY_EXECUTABLE_PATH` | Local Xray binary path for the controller. |
| `XRAY_ASSETS_PATH` | Local Xray assets path for the controller. |
| `XRAY_SUBSCRIPTION_URL_PREFIX` | Public base URL used in generated subscription links. |
| `SQLALCHEMY_DATABASE_URL` | Database URL. Defaults to SQLite when not configured. |
| `UVICORN_HOST` | Controller bind host. Defaults to `0.0.0.0`. |
| `UVICORN_PORT` | Controller bind port. Defaults to `8000`. |
| `DOCS` | Set to `True` to expose Swagger/ReDoc at `/docs` and `/redoc`. |

For the full inherited configuration surface, inspect [.env.example](./.env.example) and [config.py](./config.py).

## API

Set `DOCS=True`, then open:

- `/docs`
- `/redoc`

Node provisioning endpoints include:

- `POST /api/node/provision`
- `GET /api/node/install.sh`
- `POST /api/node/provision/redeem`

## Verification

Useful checks for the current node work:

```bash
python -m pytest tests/test_node_provisioning.py tests/test_node_active_inbounds.py tests/test_hysteria_support.py -q
bash build_dashboard.sh
XRAY_EXECUTABLE_PATH=/bin/echo alembic heads
```

## Roadmap

- Rename remaining runtime/package/image names from Marzban to MarzbanX where it can be done without breaking upgrades.
- Expand the Add Node wizard to VMess and Trojan where controller validation and node translation are complete.
- Add richer firewall/reachability checks before applying protocol switches.
- Add token rotation or reissue flow for provisioned nodes.
- Publish MarzbanX and MarzbanX-node release artifacts under the final repository names.

## Attribution

MarzbanX is a fork of [Gozargah/Marzban](https://github.com/Gozargah/Marzban). The upstream project, contributors, and AGPL-3.0 license remain the foundation of this fork.

Donation addresses from the upstream README were intentionally removed from this fork README.

## License

Published under [AGPL-3.0](./LICENSE).
