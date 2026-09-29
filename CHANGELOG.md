# Changelog

## Unreleased

- KNX mapper (`MAPPER_REVISION` 8): REXLiTE KNX Contract `CurtainImpulse` —
  a dry-contact curtain on a relay actuator in "switch impulse" mode. A Function
  whose members are the `開脈衝`/`關脈衝`/`停脈衝` addresses (DPT 1.001, no
  standard ETS role) becomes three KNX `button` entities (`{name} 開/關/停`, each
  sending 1). HA's KNX cover cannot drive it (it writes 0/1 on one up/down
  address); a partial set is rejected as `incompatible_function_roles`.
- Deployment accepts the `button` platform (identity: `address`).

## 0.1.21

- Read Deco tracker values with live HA report timestamps so unchanged online nodes and zero-traffic clients do not incorrectly expire.
- Preserve timestamp-based expiry for stopped reporting; do not refresh timestamps merely when diagnostics are requested.

## 0.1.20

- 待加入設備只回傳通過本次 LAN 服務連線檢查的 SSDP / mDNS 項目；不再將歷史探索紀錄、USB 或未知狀態當成在線網路設備。
- 連線檢查限制於探索封包提供的私有 IP 與服務埠，限制併發、總時間並自動關閉連線；不變更或刪除 HA 原有設定流程。
- 回傳在線確認時間，供介面過期後隱藏及同步計數。

## 0.1.19

- Prevent stale SSDP cache entries from blocking active device searches.
  Bound gateway callback setup independently, and keep native SSDP/mDNS scans
  running when optional router identification is slow.
- Verify discovery against HA 2026.6.0 and later published releases in CI.

## 0.1.18

- Add administrator-only, bounded LAN discovery through `rexlite/discovery/scan`.
  Refresh SSDP and mDNS from the HA host and return native pending setup flows.
- Identify unconfigured TP-Link Deco routers from fresh SSDP responses and
  suggest their local management address; keep existing Deco hubs out of pending
  results. Discovery never submits credentials or installs integrations.
- Coalesce concurrent scans, limit their duration and release temporary listeners.
  Powered devices must be network reachable and advertise a supported protocol.

## 0.1.17

- Add Fortinet as a managed HA traffic source provider. Interface SNMP
  counters remain interface-scoped and are never presented as per-client traffic.

- Fix the cloud-service device's firmware label, which remained at 0.1.13
  after installing newer releases. Add a release regression check to keep
  the device version and integration manifest in sync.
- Clarify that installing REXLiTE does not install or authenticate a router
  integration. Per-client traffic needs the router's HA integration or an
  explicitly configured network traffic source. Restart HA after updating.

## 0.1.16

- Add administrator-managed network traffic sources for existing Home Assistant
  rate entities/attributes and SNMP byte counters. Provider labels include
  TP-Link, VIGI, Deco, Zyxel, UniFi and generic devices; actual support depends
  on the device model, firmware and installed HA source integration.
- Expose a read-only, versioned `rexlite/network_traffic` WebSocket response
  for the operations backend. Keep device, interface and router scopes separate;
  credentials and arbitrary source attributes never enter this response.
- Persist source configuration locally through administrator-only actions.
  Protect against failed writes, stale readings, counter resets, reboots and
  duplicate samples. Counter rates are sampling-interval averages.
- Add real HA service, storage and WebSocket checks to the HA 2026.1–2026.9
  compatibility matrix. See `docs/network-traffic.md` for setup examples.
- Includes the previously merged KNX improvements documented in 0.1.13–0.1.15.
  Upgrading requires a full Home Assistant restart; network sources must be
  configured separately and the corresponding operations backend/frontend
  must support the new response.

## 0.1.15

- Fix: the installer `<unit>-Climate-*` dialect added in 0.1.14 emitted
  `climate` rows without `temperature_address` /
  `target_temperature_state_address`. Home Assistant's KNX schema requires
  both, so a single such row made the whole deployment fail schema
  validation (reproduced on a real 25-project corpus with HA 2026.9.1). The
  dialect now also recognises `-Climate-VAL` (DPT 9.001 setpoint),
  `-Climate-VAL-FB` (setpoint feedback) and `-Climate-VAL-REAL-FB` (room
  temperature) and only emits a climate entity when both required
  temperature addresses are proven on the same channel; otherwise the
  command addresses keep their plain fallbacks.
- Map ETS Function members that have no standard ETS DatapointRole by the
  exact `<Function name> <suffix>` convention (prefix must equal the Function
  name, exact DPT still required). This supports colour temperature
  (`色溫`/`色溫狀態`, DPT 7.600 absolute or 5.001 relative), absolute blind
  position and slat angle, and air-conditioner Functions (`FT-0`) with
  on/off, mode, fan speed, setpoint and room temperature.
- Map the standard `HVACMode` role (DPT 20.102) to `operation_mode_address`,
  add `運轉模式`/`運轉模式狀態` labels, and treat heating
  Function types `FT-4`/`FT-5`/`FT-9` as climate.
- Report unmapped Function members as `function_role_not_exposed` (standard
  non-entity roles such as `DimmingControl`) or `unresolved_function_role`
  instead of the generic `insufficient_supported_entity_metadata`.
- Bump the mapping revision (now 7) so imported projects are re-planned.

## 0.1.14

- Split several distinct actuator outputs (e.g. multiple DALI circuits, or
  several air-conditioners) sharing one installer name prefix into their own
  entities by proven actuator channel, instead of refusing the whole
  shared-name group as an ambiguous duplicate command role. Only individually
  distinct, individually-proven channels are split out; a command or optional
  role address without a single proven channel, or two that resolve to the
  same channel, is still never guessed.
- Recognise the installer `<unit>-Climate-SW/-MODE/-FAN(-FB)` convention seen
  on real air-conditioner exports and map it to a `climate` entity built from
  on/off, controller-mode (DPT 20.105) and fan-speed (DPT 5.001) addresses.
  HA's climate schema does not require a temperature pair, unlike the existing
  ETS-Function climate path, so a bare on/off pair with no proven mode or fan
  channel is left as a plain switch rather than a control-less climate entity.
  A non-standard fan/mode datapoint (observed as a raw byte-count 5.010 on
  some real exports, inconsistent with the 5.001 percentage HA expects) is
  left unmapped rather than guessed.
- Bump the mapping revision (now 6) so an already-imported project is
  re-planned on upgrade.

## 0.1.13

- Recognise the `Command` / `Status` (and `Brightness Command` /
  `Brightness Status`, plus the Chinese `指令` / `亮度指令`) group-address
  label convention alongside `開關` / `狀態`, so a dimmable channel exported
  with those labels maps to a single `light` instead of an unpaired `switch`,
  `binary_sensor` and `sensor` with the brightness-command address dropped.
  The exact-datapoint-type guard is unchanged: a `- Command` label that does
  not carry a 1.001 datapoint still blocks the whole name prefix rather than
  mismapping it.
- Bump the mapping revision (now 5) so an already-imported project is
  re-planned on upgrade.

## 0.1.12

- Include Nick's opt-in, reversible HACS 2.0.5 REXLiTE icon repair tool.
  It verifies the official frontend files, preserves the original brand icon,
  updates gzip copies and backs up files for restoration. Run it explicitly
  on the target host; updating the integration does not apply this repair.
- Recognise the `Brightness` / `Value` and `Color` / `Colour` group-address
  label spellings as the same abbreviated roles as `VAL` / `CT` (feedback
  variants included). The exact-datapoint-type guard is unchanged, so a
  `Value` address carrying a temperature datapoint (an air-conditioner
  setpoint) is not read as light brightness.
- Treat an abbreviated channel with a proven brightness or colour-temperature
  address as a dimmable `light` even when the terse installer name carries no
  light word; a channel that proves only on/off stays a `switch`.
- Pair a colour-temperature channel that uses DPT 5.001 (relative percentage)
  as well as DPT 7.600 (absolute kelvin), emitting the matching
  `color_temperature_mode`; drop a feedback address that disagrees with the
  command's encoding instead of pairing it.
- Accept a proven on/off feedback address that a DPT 1.002 (Bool) logic-block
  listener also subscribes to, as long as a Switch or State object drives it
  and the same producing actuator channel is proven.
- Map an "…-FB" group address that has an exact 1-bit status datapoint
  (1.001/1.011) and no communication object at all as a read-only
  `binary_sensor`; a writable object on the address still overrides the name.
- Bump the mapping revision (now 4) so reimporting an already-imported
  project applies the new rules. After upgrading and fully restarting Home
  Assistant, upload the original ETS project again in MAX’Is.

## 0.1.11

- Map the shade group-address labels `Close` (DPT 1.008 up/down), `Step`
  (DPT 1.007 step/stop) and `POS` (DPT 5.001 absolute position) to a single
  `cover`, alongside the existing `Up/Down` / `Stop/Step` / `Position`
  convention. The exact-datapoint-type guard still rejects a label that does
  not carry its role's datapoint type, so an impulse-relay `Close`/`Open`
  pair (DPT 1.001) is not turned into a cover.
- Bump the mapping revision so reimporting an already-imported project after
  upgrading and restarting Home Assistant applies the new cover mapping.

## 0.1.10

- Pair exact SW/SW-FB, VAL/VAL-FB and CT/CT-FB addresses using matching
  actuator-channel metadata, DPTs and communication-object flags. Combine
  supported light control and feedback into one entity.
- Resolve compatible switch/status feedback declarations only with proven
  channel evidence; preserve incompatible or incomplete mappings for review.
- Recover explicit scene numbers from verified ETS sender-application parameter
  layouts. Unsupported or protected layouts remain available for manual review.
- Replan previously imported files when the mapping revision changes, while
  preserving manual YAML and transactional recovery.
- Reversibly disable superseded managed entities after verification, preserving
  registry customizations; exclude restored registry ghosts from loaded counts.

## 0.1.9

- Support bounded 100 MiB ETS transfers with chunk retries, owner checks,
  SHA-256 verification and temporary-file cleanup.
- Add validated manual KNX YAML deployment with preflight checks and rollback.
- Preserve compatibility with Home Assistant 2026.1 and newer.

## 0.1.8

- Fix ETS upload failing with `Unknown error` during capability preflight:
  use Home Assistant's official WebSocket administrator decorator for all four
  KNX commands instead of calling a nonexistent connection method.
- Preserve administrator-only access before starting any import or deployment.
- Test the actual Home Assistant WebSocket dispatcher, administrator and denied
  access, schema validation, import errors and retries across the supported
  2026.1+ compatibility matrix.
- Restart Home Assistant after updating to load the corrected command handlers.

## 0.1.7

- Reconnect when the heartbeat worker fails; cancel and await pending requests
  before reconnecting or stopping so old requests cannot cross cloud sessions.
- Bound cloud writes, local stream backpressure, and stream shutdown; cover
  recovery and cleanup with real WebSocket fault-injection tests.
- Add administrator-only capability, verified ETS import, YAML deployment, and
  deployment-status WebSocket commands for one-upload KNX provisioning.
- Validate file fingerprints and actual KNX schemas, preserve manual YAML/UI
  entities, and transactionally activate a managed package with rollback.
- Verify effective configuration and registered entity states after reload;
  expose skipped mappings and unavailable state counts separately.
- Support automatic KNX deployment on every stable Core release from 2026.1.0:
  legacy address-format identities on 2026.1-7, stable native identities on
  2026.8, and custom identities for new deployments on 2026.9+.
- Preserve canonical identity metadata across Core upgrades and honor official
  registry migrations. Reject conflicting/duplicate identities and unsafe legacy
  address-format changes before changing YAML. Preserve nested manual RGB GAs.
- Support pre-May tuple KNX entity identifiers and avoid normalizing existing
  KNX schemas twice when preserving manual lights or climate entities.
- Report Core/schema incompatibility in capability preflight; the integration's
  minimum stays Core 2026.1.0.
- Restart Home Assistant after installing this release to register the new
  commands. Entry reload alone does not load newly installed integration code.

## 0.1.6

- Classify rejected service credentials separately from temporary network
  failures, stop permanent retry loops, and start Home Assistant reauthentication.
- Validate stored credentials before runtime setup while preserving Home
  Assistant's bounded setup retry for boot-time network unavailability.
- Distinguish a healthy preconfigured connection awaiting user activation from
  an active cloud service, without changing the stable service identity.
- Preserve the same authenticated agent identity across reboot and DHCP address
  changes, and re-publish the current LAN endpoint after connectivity returns.
- Redact the service identity, activation credential, and local Home Assistant
  address from diagnostics, and clean up runtime tasks during unload.

## 0.1.5

- Detect the current Home Assistant host LAN IPv4 address and report it with
  the stable tunnel agent ID.
- Reject loopback, public, link-local, and Docker bridge addresses so stale
  `127.0.0.1` and `172.17.0.1` values are never published as IPC endpoints.
- Re-publish tunnel metadata when the LAN IP changes, allowing the operation
  platform and mobile app to reconcile a moved IPC without re-pairing.

## 0.1.4

- Add compliant 256x256 and 512x512 local brand icons for supported Home
  Assistant versions.
- Consistently describe the customer-facing feature as REXLiTE AI Cloud
  Service in Traditional Chinese and English.
- Preserve existing configurations by keeping the internal service-setting
  schema unchanged.
- Add regression coverage for customer-facing cloud-service terminology.

## 0.1.3

- Rename the integration and setup experience to REXLiTE AI.
- Simplify activation by using the managed REXLiTE gateway and local Home
  Assistant defaults.
- Add local brand assets for Home Assistant 2026.3 and newer.
- Strengthen product-copy, JSON, brand-asset, and release validation checks.

## 0.1.2

- Recreate the authenticated tunnel session whenever a user enables or disables remote administration.
- Close all requests and streams from the previous access mode before reconnecting.
- Preserve health-only monitoring while remote administration is disabled.

## 0.1.1

- Fix dedicated-host Home Assistant pages remaining in a loading state.
- Close completed HTTP streams using RFC-compatible `HEAD`, `Content-Length`, and chunked response framing while preserving WebSocket upgrades.
- Prevent completed remote page requests from accumulating as active tunnel streams.

## 0.1.0

- Add HACS installation support for the REXLiTE Home Assistant integration.
- Support Home Assistant OS, Supervised, Container, and Core installations.
- Add connection status, automatic recovery, and user-manageable settings.
- Add Traditional Chinese and English localization.
- Add automated validation and release checks.
