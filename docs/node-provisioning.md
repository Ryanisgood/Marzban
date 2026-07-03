# Add Node Provisioning

This document describes the controller-managed Add Node flow.

## Target Flow

1. Open the dashboard node modal.
2. Use Add Node, not the advanced manual form.
3. Enter node name, public address, management ports, protocols, and public inbound ports.
4. The controller creates generated inbounds, proxy hosts, a panel-managed node, and a one-time install token.
5. Run the generated command on the new node:

```bash
curl -fsSL https://controller.example.com/api/node/install.sh | sudo bash -s -- --token xxx
```

The node service environment does not need `INBOUNDS`. The controller sends the selected inbound tags to the Rust node because the node is created with `inbounds_mode=panel`.

## Controller Settings

Set these on the controller before using the wizard in production:

```env
MARZBAN_NODE_BINARY_URL=https://controller.example.com/downloads/marzban-node
SING_BOX_INSTALL_SCRIPT_URL=https://controller.example.com/downloads/install-sing-box.sh
XRAY_INSTALL_SCRIPT_URL=https://github.com/XTLS/Xray-install/raw/main/install-release.sh
```

`MARZBAN_NODE_BINARY_URL` should point to the Rust `marzban-node` binary built for the target Linux architecture. A static musl build is preferred when deploying the same binary across small VPS nodes with different libc versions.

`SING_BOX_INSTALL_SCRIPT_URL` is required for HY2 or any HY2 combination unless `sing-box` is already installed on the node. Keep this script under your control or use a reviewed internal mirror. Do not depend on an unreviewed third-party shell script for production rollout.

`XRAY_INSTALL_SCRIPT_URL` has a default for Xray-only nodes. Override it if the node cannot reach GitHub or if you use an internal package mirror.

## Protocol And Core Policy

The wizard currently supports:

- HY2/Hysteria2
- VLESS TCP REALITY
- Shadowsocks TCP

Core selection is deterministic:

- any selected HY2 inbound means `sing-box`;
- Xray-only protocols mean `xray`;
- the Rust node starts one core at a time.

VLESS REALITY provisioning generates a fresh X25519 key pair with the controller's existing Xray helper and a random short ID. If key generation fails, provisioning fails before writing the config.

## Public Ports

Every node needs:

- `SERVICE_PORT/tcp`, default `62050`, for controller-to-node REST management;
- the selected proxy inbound ports, for example `8443/udp` for HY2 or `443/tcp` for VLESS/SS.

`XRAY_API_PORT/tcp`, default `62051`, is only used by Xray-selected node configs. HY2/sing-box configs do not expose an Xray API listener, but the value remains in the env file so the same service file works after a later switch to an Xray-only config.

## Installer Behavior

The installer:

- redeems the token once;
- downloads `/usr/local/bin/marzban-node` when `MARZBAN_NODE_BINARY_URL` is configured;
- installs only the required core;
- writes the controller client certificate to `/var/lib/marzban-node/ssl_client_cert.pem`;
- generates the node server certificate and private key locally;
- writes `/etc/marzban-node.env` without `INBOUNDS`;
- installs and starts `marzban-node.service`.

The token is short-lived and one-time use. The database stores only its hash.

## Pitfalls From Rollout

- Do not create DB inbound rows without writing the matching generated inbound to `XRAY_JSON`; users may see a host but the node cannot start the protocol.
- Do not keep VLESS REALITY placeholder keys. `XRayConfig` only checks that fields exist, so invalid placeholder strings can pass panel validation and then fail at runtime.
- Do not start both Xray and sing-box on a low-memory node. If HY2 is present, use sing-box for the whole selected inbound set.
- Do not rely on `INBOUNDS` for panel-managed nodes. It makes protocol switching and user provisioning harder to reason about.
- Do not deploy with an empty `MARZBAN_NODE_BINARY_URL` unless the node image already contains `/usr/local/bin/marzban-node`.
- Do not deploy HY2 with an empty `SING_BOX_INSTALL_SCRIPT_URL` unless the node image already contains `sing-box`.
- If core restart fails during provisioning, the controller restores the previous config file and in-memory config.
- If a generated install command is lost, create a new node or add a token rotation endpoint later; the plaintext token is intentionally shown only once.

## Verification

Controller:

```bash
python -m pytest tests/test_node_provisioning.py tests/test_node_active_inbounds.py tests/test_hysteria_support.py -q
bash build_dashboard.sh
XRAY_EXECUTABLE_PATH=/bin/echo alembic heads
```

Node:

```bash
sudo systemctl is-active marzban-node
sudo journalctl -u marzban-node -n 100 --no-pager
sudo ss -lntup | grep -E ':62050|:62051|:443'
sudo ss -lunp | grep -E ':8443'
sudo ps -eo pid,rss,args | grep -E 'marzban-node|sing-box run|xray run -config stdin:' | grep -v grep
```

Expected HY2 result: `marzban-node` and `sing-box` are running; Xray is not required.
