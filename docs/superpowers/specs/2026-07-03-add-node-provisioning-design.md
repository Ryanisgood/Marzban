# Add Node Provisioning Design

Date: 2026-07-03

## Goal

The Add Node flow should become a controller-owned provisioning workflow:

1. Admin opens Add Node in the dashboard.
2. Admin fills node name, address, node service port, node API port, protocols, and public inbound ports.
3. The controller creates the required inbound entries.
4. The controller creates the required hosts for only those generated inbounds.
5. The controller creates the node in panel-managed mode and binds the generated inbound tags.
6. The controller returns a one-command installer:

```bash
curl -fsSL https://controller.example.com/api/node/install.sh | sudo bash -s -- --token xxx
```

7. The new machine runs that command and the node comes online without manually editing `INBOUNDS`.

## Existing Constraints

Marzban has two sources of inbound-related state:

- `XRAY_JSON` is the source of truth for protocol, tag, port, listen address, transport, and TLS settings. `xray.config.inbounds_by_tag` and `xray.config.inbounds_by_protocol` are derived from it.
- The database `ProxyInbound` table stores only tags and host relationships. It cannot create a functional inbound by itself.

Therefore provisioning must create generated inbounds in `XRAY_JSON`, then run the same effective lifecycle as `PUT /api/core/config`: validate with `XRayConfig`, update `xray.config`, persist the JSON file, restart the main core and connected nodes, then refresh `xray.hosts`.

The Rust node already supports controller-managed inbounds. In panel mode the controller sends selected inbound tags to the node, and the node can prefer those over the environment `INBOUNDS` value. The provisioning installer should not require `INBOUNDS` in `/etc/marzban-node.env`.

## First Version Scope

Supported provisioning templates:

- `hy2`: Hysteria2 over UDP, requires sing-box.
- `vless-reality`: VLESS TCP REALITY, uses Xray when provisioned without HY2 and sing-box when combined with HY2.
- `shadowsocks`: Shadowsocks TCP, uses Xray when provisioned without HY2 and sing-box when combined with HY2.
- `hy2 + vless-reality + shadowsocks`: allowed as one sing-box node because the Rust translator supports Hysteria2, VLESS, and Shadowsocks.

Explicitly out of scope for the first version:

- VMess provisioning.
- Trojan provisioning.
- Arbitrary custom stream settings.
- WS, gRPC, h2, HTTPUpgrade, SplitHTTP, and other complex transports.
- A full DB-backed inbound overlay that replaces `XRAY_JSON`.
- Public reachability guarantees. The controller may report local or configured ports, but firewall and provider security group state remain unknown unless a later probe feature is added.

## Runtime Core Policy

The controller chooses the expected node core from the generated protocol set:

- If any selected protocol requires sing-box, the node is expected to run sing-box.
- Otherwise the node is expected to run Xray.
- A node runs one core at a time.
- The first version rejects combinations that require sing-box but include a protocol not supported by the Rust sing-box translator, such as HY2 plus VMess.

This keeps the panel policy aligned with the Rust node behavior and avoids silently dropping unsupported inbounds.

## Generated Inbounds

The provisioning service generates inbound tags server-side. Users do not type tags.

Tag shape:

```text
node-{node_id}-{protocol}-{public_port}
```

Examples:

```text
node-42-hy2-8443
node-42-vless-443
node-42-shadowsocks-8388
```

The service validates before writing:

- tag does not already exist;
- public port is a valid TCP/UDP port;
- generated transport and bind address do not conflict with an existing generated or selected inbound;
- the protocol combination is supported by the chosen core;
- the resulting complete config passes `XRayConfig(payload)`.

Generated template defaults:

- HY2: `protocol=hysteria`, `settings.version=2`, `settings.users=[]`, `streamSettings.network=hysteria`, TLS enabled, ALPN `h3`.
- VLESS REALITY: `protocol=vless`, `settings.clients=[]`, TCP transport, REALITY enabled with generated short ID and key material from the existing Xray helper path.
- Shadowsocks: `protocol=shadowsocks`, `settings.clients=[]`, TCP network.

If a template cannot be generated safely, provisioning fails before DB commit and before replacing the live config.

## Hosts

Provisioning creates hosts only for the generated inbound tags, not for every inbound in the system.

Default host:

- `remark`: `{NODE_NAME} ({USERNAME}) [{PROTOCOL} - {TRANSPORT}]`
- `address`: the provided public node address
- `port`: the protocol public port
- `security`: inbound default

This avoids polluting existing inbound host lists.

## Node Creation

Provisioning creates a regular node record with:

- `name`
- `address`
- `port`
- `api_port`
- `inbounds_mode=panel`
- `active_inbounds=[generated tags]`
- `usage_coefficient`
- initial status `connecting`

After commit, the controller schedules the existing node connect flow. If the machine has not installed the service yet, the node may remain connecting or error until the installer runs.

## Install Token

Provisioning creates a DB-backed install token record.

Table: `node_provision_tokens`

Fields:

- `id`
- `node_id`
- `token_hash`
- `created_by`
- `created_at`
- `expires_at`
- `redeemed_at`
- `revoked_at`
- `active_inbounds_json`
- `core_kind`

Rules:

- The plaintext token is shown only once in the provisioning response.
- The database stores only a hash.
- Tokens are high entropy.
- Tokens are short-lived.
- Tokens are one-time use.
- Redeemed or revoked tokens cannot fetch config again.
- Token validation logs must not print the plaintext token.
- Only sudo admins can create provisioning tokens.

The `curl | sudo bash` command exposes the token through shell history and process arguments. Short TTL, one-time redemption, and immediate invalidation are required mitigations.

## Installer API

Public endpoint:

```http
GET /api/node/install.sh
```

This returns a fixed shell script. It does not embed token-specific secrets.

Token-protected endpoints:

```http
POST /api/node/provision
POST /api/node/provision/redeem
```

`POST /api/node/provision` requires sudo admin auth and creates config, node, and token.

`POST /api/node/provision/redeem` accepts the install token and returns the install payload once.

The redeem payload includes:

- node service host and port;
- node API host and port;
- active inbound tags for diagnostics only;
- expected core kind;
- controller client public certificate required by `SSL_CLIENT_CERT_FILE`;
- node service certificate subject details so the installer can generate the node's local server certificate and key;
- binary download sources or package install strategy for `marzban-node` and the required core.

It must not return the controller private key. The node server certificate and key are generated locally on the node machine. The controller learns the node server certificate through the existing connection flow that calls `ssl.get_server_certificate((address, port))`.

## Installer Behavior

The installer runs as root and performs these steps:

1. Parse `--token`.
2. Call the redeem endpoint over HTTPS.
3. Install or update `/usr/local/bin/marzban-node`.
4. Install only the required core:
   - `sing-box` for HY2 or HY2 combinations.
   - `xray` for Xray-only templates.
5. Write `/var/lib/marzban-node/ssl_client_cert.pem`.
6. Generate and write node service certificate and key.
7. Write `/etc/marzban-node.env`.
8. Write or install `marzban-node.service`.
9. Run `systemctl daemon-reload`.
10. Enable and restart `marzban-node`.

The environment file does not set `INBOUNDS`; the controller manages selected inbounds through panel mode.

## Failure Handling

Provisioning has two transactional boundaries:

1. Config mutation.
2. Database mutation.

The service should build the full candidate config in memory first. It validates it with `XRayConfig` before writing the file. File write should use an atomic replace strategy and a lock to avoid concurrent edits.

If DB creation fails after config validation, the service rolls back DB changes and restores the previous config. If restart fails after a valid config write, provisioning returns an error that includes the node was not fully provisioned and leaves enough diagnostic state for admin recovery.

The implementation should not directly append text to `XRAY_JSON`.

## Dashboard Flow

The existing Add Node form becomes a provisioning wizard for new nodes:

1. Node identity: name, public address, service port, API port, usage coefficient.
2. Protocol selection: HY2, VLESS TCP REALITY, Shadowsocks TCP.
3. Per-protocol public ports.
4. Review generated protocols and expected core.
5. Submit and display generated install command.

The legacy manual node form can remain available as an advanced path if needed, but the default Add Node path should be provisioning.

## Tests

Backend tests:

- provisioning creates generated inbounds in the candidate `XRAY_JSON`;
- provisioning creates hosts only for generated inbounds;
- provisioning creates a panel-mode node with matching `active_inbounds`;
- provisioning runs the core config lifecycle equivalent to `PUT /core/config`;
- tag conflict returns 400;
- port conflict returns 400 with TCP/UDP awareness;
- HY2-only expects sing-box;
- VLESS-only expects Xray;
- Shadowsocks-only expects Xray;
- HY2 + VLESS + Shadowsocks expects sing-box;
- HY2 + VMess returns 400;
- token is stored hashed, not plaintext;
- expired token cannot redeem;
- redeemed token cannot redeem again;
- revoked token cannot redeem;
- non-sudo admin cannot provision;
- redeem response does not expose controller private key.

Installer tests:

- install script fails without `--token`;
- install script fails on invalid token;
- install script writes env without `INBOUNDS`;
- install script selects sing-box for HY2;
- install script selects Xray for VLESS-only and Shadowsocks-only.

Rust node regression tests:

- controller-provided inbounds still override environment `INBOUNDS`;
- HY2 combinations select sing-box;
- Xray-only generated templates select Xray.

Dashboard tests or build checks:

- provisioning form posts to the provisioning endpoint;
- generated command is shown after success;
- unsupported protocol combinations are blocked in the UI and still validated by the API.

## Rollout

Commit order:

1. Design spec.
2. Backend provisioning models, migrations, services, and tests.
3. Installer script endpoint and installer tests.
4. Dashboard provisioning wizard and build output.
5. Documentation update and final verification.

Each implementation commit should be independently reviewable and should not mix unrelated refactors.
