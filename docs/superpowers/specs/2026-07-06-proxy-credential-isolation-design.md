# Proxy Credential Isolation Design

## Problem

Deleting a user currently invalidates that user's subscription URL because the subscription token resolves to a database user that no longer exists. That does not by itself invalidate proxy credentials already downloaded into a client.

Node access is denied only after the deleted user's proxy credential is no longer accepted by the running core. If two users share the same effective proxy credential, deleting one user can leave the shared credential accepted through the remaining user. The removed user's old client config can still authenticate with that shared credential.

The problem is not limited to Hysteria2. It applies to every supported protocol whose client-facing authentication credential can be duplicated across users.

## Current Behavior

The panel stores one proxy settings object per user/protocol. When node configs are built, active and on-hold users are appended as inbound users with `email = "{id}.{username}"` plus protocol settings. For Xray API protocols, user removal deletes by that email. For config-reload protocols, node config is rebuilt and affected nodes are restarted.

The `email` or sing-box `name` field identifies the user for config and usage attribution, but the client authenticates with protocol credentials:

| Protocol | Client credential | Node credential field | Shared credential risk |
| --- | --- | --- | --- |
| VMess | `id` UUID | `clients[].id` / sing-box `users[].uuid` | Yes, if two users share UUID |
| VLESS | `id` UUID | `clients[].id` / sing-box `users[].uuid` | Yes, if two users share UUID |
| Trojan | `password` | `clients[].password` / sing-box `users[].password` | Yes |
| Shadowsocks | `method + password` | `clients[].password` / sing-box `users[].password` | Yes |
| Hysteria2 / HY2 | `auth` exposed as password | `users[].auth` / sing-box `users[].password` | Yes |
| AnyTLS | `password` | `users[].password` / sing-box `users[].password` | Yes |

For sing-box conversion, MarzbanX-node maps controller user `email` into sing-box `name`, and maps the credential into `uuid` or `password`. `name` is not a second authentication factor.

Shadowsocks has one additional risk to verify during implementation: generated sing-box inbounds currently include an inbound-level `password` copied from inbound settings, and may also include per-user `users`. If sing-box accepts the inbound-level password in addition to per-user passwords, a deleted user could bypass per-user removal by using the shared inbound password. The design treats this as a high-priority implementation check.

There is a related runtime deletion gap outside single-user deletion: bulk deletion of expired or limited users must also remove those users from running Xray API inbounds and restart affected config-reload nodes. Otherwise users can remain in memory until the next full core/node restart even when credentials are unique.

## Goals

1. Ensure deleting a user invalidates that user's already-downloaded client config after node update completes.
2. Apply the protection consistently across VMess, VLESS, Trojan, Shadowsocks, HY2, and AnyTLS.
3. Preserve existing valid users' downloaded configs when deleting an unrelated user.
4. Provide a clear operator path for existing installations that already contain duplicated credentials.
5. Avoid relying on subscription URL revocation as a node-access security boundary.
6. Ensure bulk deletion paths apply the same runtime removal semantics as single-user deletion.

## Non-Goals

1. Do not require remaining users to pull new subscriptions when deleting an unrelated user.
2. Do not change upstream sing-box or Xray protocol authentication semantics.
3. Do not add username+password authentication to protocols that do not support it as a client-facing contract.
4. Do not silently rewrite credentials for unrelated users during user deletion.

## Design Decision

Use per-user credential isolation as the security boundary.

Every user/protocol credential that can authenticate to a given inbound must be unique among runnable database proxy records for the same effective inbound and credential type. Runnable means any user status that is included in runtime config, currently `active` and `on_hold`. User deletion then removes the only accepted copy of the deleted user's credential from each inbound that user could access. Existing unrelated users keep their credentials and do not need to re-pull subscriptions.

This is preferable to rotating all users who share a credential on delete because forced rotation creates a service interruption for users who were not deleted. It is also preferable to adding protocol-specific derived credentials only for HY2, because the same risk exists for Trojan, Shadowsocks, AnyTLS, VMess, and VLESS.

## Credential Identity Rules

Credential uniqueness is evaluated by proxy type, effective inbound tag, and effective client credential:

| Proxy type | Unique key |
| --- | --- |
| VMess | `("vmess", inbound_tag, id)` |
| VLESS | `("vless", inbound_tag, id)` |
| Trojan | `("trojan", inbound_tag, password)` |
| Shadowsocks | `("shadowsocks", inbound_tag, method, password)` |
| Hysteria2 / HY2 | `("hysteria", inbound_tag, auth)` |
| AnyTLS | `("anytls", inbound_tag, password)` |

The check is inbound-scoped. A credential reused on a disjoint inbound does not keep the deleted user's old inbound usable, because the remaining user is not present in that inbound's runtime user list. A Trojan password may also equal an AnyTLS password without creating cross-protocol access, because those protocols are separate inbound implementations. Within the same protocol and inbound, duplicates must be rejected or repaired.

For Shadowsocks, `method` is part of the key because clients authenticate with method and password together. During implementation, if node conversion still exposes an inbound-level password that is accepted independently of per-user passwords, that inbound-level password must be removed from user-managed sing-box Shadowsocks inbounds or replaced with a non-client credential that cannot authenticate normal users.

## Effective Inbound Algorithm

The credential isolation module must use the same inbound selection semantics as runtime config generation:

1. For create request models, the effective inbound set is `user.inbounds[proxy_type]` after model validation has filled missing protocol inbounds with every configured inbound for that protocol.
2. For modify requests, the effective inbound set must be computed from the post-update user state. `UserModify` may omit either `proxies` or `inbounds`, so the implementation must merge request fields with the existing database user before extracting credential keys.
3. For existing database rows, the effective inbound set is every configured inbound for `proxy.type`, minus that proxy's `excluded_inbounds`.
4. For update operations that modify only proxies or only inbounds, validation must compare the post-update effective inbound set, not just the fields present in the request.
5. For status changes, credentials must be checked when a user enters a runnable status (`active` or `on_hold`), because only runnable users are accepted by nodes.
6. For repair/audit, duplicates are reported only when two runnable users have overlapping effective inbound tags for the same protocol credential key.

This algorithm should be implemented once and reused by create, update, revoke, repair, audit, status-change, and config-build safety checks.

## User-Facing Behavior

### Create User

When creating a user, generated defaults remain random. If an admin provides explicit proxy settings and a credential duplicates an existing user for the same unique key, the API rejects the request with HTTP 409 and a protocol-specific message.

### Modify User

When modifying proxy settings, the API rejects a credential that duplicates another user. Keeping the same credential on the same user is allowed.

### Delete User

Deleting a user does not rotate any remaining user's credentials. It removes the user from Xray API inbounds or rebuilds config-reload inbounds, as today. Because duplicates are no longer allowed after migration, the deleted user's old client credential is no longer present after propagation.

Bulk deletion paths must use the same runtime removal semantics. They must snapshot the runtime removal plan before deleting database rows. That plan must include each removed user's `id`, `username`, proxy types, effective inbounds, and config-reload inbound tags. After the database delete commits, the runtime step may remove users one by one or rebuild a single post-delete config and restart affected nodes, but it must not only delete database rows.

### Revoke Subscription

Revoking a user's subscription continues to rotate that user's protocol credentials. The new credentials must pass the same uniqueness check before they are committed. Other users are not rotated.

### Existing Duplicate Credentials

Existing installations may already contain duplicates. The implementation must include an operator-visible audit and repair path before enforcing uniqueness as a hard gate on all writes.

The minimum acceptable migration behavior is:

1. Add a credential audit helper that lists duplicates by protocol, credential fingerprint, and affected usernames.
2. Add a repair helper that rotates duplicate credentials for all but one user in each duplicate group.
3. Run the repair helper through tests and document the operational effect: repaired users must re-pull subscriptions because their credentials changed.
4. Enforce uniqueness on create/modify/revoke after the repair path exists.

## API And Internal Structure

Add a focused credential isolation module rather than scattering ad hoc checks:

- `app/xray/credential_isolation.py`
  - Extract inbound-scoped protocol credential keys from DB `Proxy` rows and Pydantic proxy settings.
  - Compute effective inbound tags using the same semantics as runtime config generation.
  - Find duplicate credential groups.
  - Validate a pending create or update against existing DB users.
  - Rotate duplicates using each protocol settings model's existing `revoke()` method.

Integrate the module in:

- `crud.create_user`
- `crud.update_user`
- `crud.revoke_user_sub`
- `crud.update_user_status` and bulk status transitions that move users into `active` or `on_hold`
- router or job paths that bulk-delete users

The module should avoid importing router code. It may depend on DB models, proxy models, and SQLAlchemy sessions.

## Node Configuration Rules

The controller remains the source of user credentials. For Xray API protocols, `email` remains the removal key. For config-reload protocols, `include_db_users()` remains the source for rebuilt node configs.

MarzbanX-node conversion must preserve per-user identity fields for usage attribution, but must not introduce an additional shared credential that can authenticate deleted users. Shadowsocks conversion is the only known place that needs explicit verification because it currently writes both inbound-level `password` and optional per-user `users`.

If implementation confirms sing-box requires inbound-level Shadowsocks `password` even when per-user `users` exists, the design must be adjusted before implementation continues. If implementation confirms it is optional or acts as a fallback credential, remove it for user-managed inbounds.

## Testing Strategy

### Unit Tests

Add tests for credential key extraction:

- VMess and VLESS use UUID keys.
- Trojan, HY2, and AnyTLS use password/auth keys.
- Shadowsocks uses method plus password.
- Every key includes the effective inbound tag.
- Existing DB rows derive effective inbounds from all protocol inbounds minus `excluded_inbounds`.

Add tests for duplicate detection:

- Duplicate HY2 auth across two users on the same inbound is detected.
- Duplicate AnyTLS password across two users on the same inbound is detected.
- Duplicate VMess/VLESS UUID on the same inbound is detected.
- Duplicate Trojan password on the same inbound is detected.
- Duplicate Shadowsocks password with same method on the same inbound is detected.
- Same raw secret across different protocols is not treated as a duplicate.
- Same credential on disjoint inbound tags is allowed.
- Same user's unchanged credential is allowed during modify.
- Duplicate credentials are detected when one user uses default all-inbounds behavior and another overlaps only one inbound.
- Updating only inbound selections can be rejected when it creates a duplicate overlap.
- `on_hold` users participate in duplicate detection.
- A disabled, expired, or limited user does not block a runnable user until it is moved back into `active` or `on_hold`.

### API / CRUD Tests

Add tests that:

- Creating a user with duplicate credentials fails.
- Modifying a user to another user's credential fails.
- Revoking a user's credentials produces values that do not collide.
- Moving a disabled, expired, or limited user into `active` or `on_hold` fails when its credentials conflict with an existing runnable user.
- Repair rotates duplicate credentials and leaves one representative unchanged.

### Node Conversion Tests

In MarzbanX-node tests or controller-side conversion expectations, verify:

- HY2 converts `auth` to sing-box `users[].password`.
- AnyTLS converts `password` to sing-box `users[].password`.
- VMess/VLESS/Trojan/Shadowsocks per-user credentials map to sing-box `users`.
- Shadowsocks does not expose an accepted shared inbound-level password when per-user users are present.

### End-to-End Behavior Test

Add a controller-level test that builds a config with user A and user B, deletes A, rebuilds config, and asserts A's credential no longer appears in any inbound while B's credential remains.

Add a bulk deletion test that removes expired or limited users and verifies the runtime removal/restart path is scheduled for all affected users and config-reload inbounds.

Add a deletion-plan test that verifies bulk deletion captures user id, username, proxy settings, and affected reload inbounds before deleting database rows.

## Rollout Plan

1. Add duplicate detection helpers and tests without changing write behavior.
2. Add repair helper and tests.
3. Complete the Shadowsocks inbound-level password behavior check. If the top-level password is an accepted fallback credential when per-user `users` exist, fix node conversion before claiming Shadowsocks deletion safety.
4. Add write-time enforcement for create, modify, revoke, and transitions into runnable statuses.
5. Fix bulk deletion runtime synchronization for expired/limited user removal paths.
6. Update documentation explaining that per-user credentials must be unique per inbound and that repaired users must re-pull subscriptions.

Each step should be committed separately.

## Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Existing duplicate users break after hard enforcement | Provide repair helper before enforcement |
| Repair rotates active users unexpectedly | Make repair an explicit operator action, not automatic during delete |
| Shadowsocks inbound-level password remains a bypass | Verify sing-box behavior and remove fallback credential if needed |
| Random credential collision | Validate generated credentials on create/revoke and retry generation if needed |
| Cross-repo node changes needed | Keep controller enforcement independent; isolate node conversion changes in a separate commit if writable |

## Open Implementation Checks

These are checks to complete before coding the enforcement tasks:

1. Confirm sing-box Shadowsocks behavior when both inbound-level `password` and `users` are present. This is a precondition for write-time enforcement.
2. Confirm whether the implementation scope can edit MarzbanX-node in this workspace. If not, controller-side uniqueness enforcement still proceeds, and node conversion changes require explicit filesystem approval before implementation.
3. Use a CLI command as the first operator surface for audit/repair. Add an admin API only if a separate requirement requests dashboard or API-driven repair.
4. Continue implementation in the `proxy-auth-isolation` branch and `/private/tmp/MarzbanX-proxy-auth-isolation` worktree; keep the original checkout untouched except for explicit user-approved operations.
