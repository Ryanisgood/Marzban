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

## Inbound Ownership Model

Panel-managed MarzbanX nodes use node-owned inbounds:

- `proxy_inbounds.owner_node_id` records which node owns an inbound.
- `node_inbounds_association` records which owned inbounds are currently active for that node.
- The dashboard only offers owned inbounds for a node.
- The API and runtime reject panel-managed selections that are unowned or owned by another node.
- `INBOUNDS` on the Rust node remains a legacy/manual fallback only.

This intentionally removes global inbound reuse from the normal Add Node workflow. Users are still maintained at the protocol/account level, but each node gets its own runnable inbound rows and public ports. That makes the controller able to answer simple operational questions such as "what is rn1c1g actually running?" without asking the operator to SSH into the node and inspect environment variables.

Legacy unowned inbound rows can remain in the database for migration or manual nodes, but they are invalid choices for panel-managed nodes. Generated tags matching `node-{id}-{protocol}-{port}` should be backfilled to the matching node when upgrading old data. The generated tag matcher includes HY2/Hysteria2, VLESS, VMess, Shadowsocks, Trojan, and AnyTLS.

## Controller Settings

Set these on the controller before using the wizard in production:

```env
MARZBAN_NODE_BINARY_URL=https://controller.example.com/downloads/marzban-node
SING_BOX_INSTALL_SCRIPT_URL=https://sing-box.app/install.sh
SING_BOX_VERSION=1.13.14
SING_BOX_DOWNLOAD_URL_TEMPLATE=https://github.com/SagerNet/sing-box/releases/download/v{version}/sing-box-{version}-linux-{arch}.tar.gz
XRAY_INSTALL_SCRIPT_URL=https://github.com/XTLS/Xray-install/raw/main/install-release.sh
```

`MARZBAN_NODE_BINARY_URL` should point to the Rust `marzban-node` binary built for the target Linux architecture. A static musl build is preferred when deploying the same binary across small VPS nodes with different libc versions.

`SING_BOX_VERSION` defaults to `1.13.14`. HY2 and AnyTLS Add Node provisioning installs sing-box automatically when it is missing or when the installed version differs from the pinned version. By default the installer downloads the fixed release archive from `SING_BOX_DOWNLOAD_URL_TEMPLATE`; `SING_BOX_INSTALL_SCRIPT_URL` is kept as a fallback or mirror hook. Override these only when the node cannot reach the official endpoint or when you provide an internal reviewed mirror.

`XRAY_INSTALL_SCRIPT_URL` has a default for Xray-only nodes. Override it if the node cannot reach GitHub or if you use an internal package mirror.

## Protocol And Core Policy

The wizard currently supports:

- HY2/Hysteria2
- AnyTLS
- VLESS TCP REALITY
- Shadowsocks TCP

Core selection is deterministic:

- any selected HY2 or AnyTLS inbound means `sing-box`;
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

- reads the install payload without consuming the token;
- downloads `/usr/local/bin/marzban-node` when `MARZBAN_NODE_BINARY_URL` is configured;
- installs or corrects sing-box to `SING_BOX_VERSION` for HY2/AnyTLS nodes from the fixed release archive;
- installs only the required core;
- writes the controller client certificate to `/var/lib/marzban-node/ssl_client_cert.pem`;
- generates the node server certificate and private key locally;
- writes `/etc/marzban-node.env` without `INBOUNDS`;
- installs and starts `marzban-node.service`.
- consumes the token after `systemctl enable --now marzban-node` succeeds.

The token is short-lived and one-time use. The database stores only its hash. Installer failures before service startup do not consume the token, so the same generated command can be retried after fixing local package, network, or permission issues.

## Pitfalls From Rollout

- Do not reuse one node's generated inbound on another panel-managed node. Create a new inbound for the target node instead.
- Do not treat unowned/global inbound rows as normal MarzbanX node choices. They are legacy/manual data unless ownership is explicitly assigned after verification.
- Do not create DB inbound rows without writing the matching generated inbound to `XRAY_JSON`; users may see a host but the node cannot start the protocol.
- Do not keep VLESS REALITY placeholder keys. `XRayConfig` only checks that fields exist, so invalid placeholder strings can pass panel validation and then fail at runtime.
- Do not start both Xray and sing-box on a low-memory node. If HY2 is present, use sing-box for the whole selected inbound set.
- Do not rely on `INBOUNDS` for panel-managed nodes. It makes protocol switching and user provisioning harder to reason about.
- Do not deploy with an empty `MARZBAN_NODE_BINARY_URL` unless the node image already contains `/usr/local/bin/marzban-node`.
- Do not remove both `SING_BOX_DOWNLOAD_URL_TEMPLATE` and `SING_BOX_INSTALL_SCRIPT_URL`; HY2/AnyTLS provisioning needs one install source.
- If core restart fails during provisioning, the controller restores the previous config file and in-memory config.
- If a generated install command is lost, create a new node or add a token rotation endpoint later; the plaintext token is intentionally shown only once.
- If the installer fails after starting the service but before the final consume request, rerunning the command is still idempotent: it rewrites the same env/service files and then consumes the token.

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
