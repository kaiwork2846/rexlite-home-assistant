# KNX ETS automatic deployment

## Runtime and release requirements

- REXLiTE AI integration 0.1.8 or newer, loaded after a Home Assistant restart.
- Home Assistant Core 2026.1.0 or newer stable release, matching HACS' minimum.
- The official KNX integration must be set up by the cloud commissioning flow
  before processing the upload. The KNX IP interface still needs a reachable,
  valid connection. ETS parsing alone does not prove bus connectivity.
- Use a matching cloud backend implementing protocol version 1. Preflight calls
  `rexlite/knx/project_capabilities` before importing; a response must explicitly
  contain `version: 1` and `supported: true`. Older integration versions have no
  such command and must be updated through the normal integration release path.

A capability failure returns an explicit `reason` and human-readable `message`,
plus `homeAssistantVersion`, `requiredHomeAssistantVersion`, and
`integrationVersion`. Fresh installations do not need optional KNX Python
requirements just to pass capability discovery. A loaded KNX component is schema
checked during discovery; deployment always checks the actual KNX schema before
changing any configuration.

Core 2026.1-2026.7 creates native YAML identities in the imported project's
address format (three-level, two-level or free). Core 2026.8 changes native
identities to stable three-level addresses and provides an official registry
migration that preserves entity IDs. Core 2026.9+ additionally accepts custom
YAML `unique_id` values for new deployments.

The writer stores canonical identity metadata as well as the actual deployed
identity and format. Status and ownership verification use the current Core's
behavior, so a normal Core upgrade can use the official migration without losing
the original entity IDs. An established native deployment stays native after an
upgrade to 2026.9+. On 2026.1-2026.7, changing the project's global address format
after deployment is rejected before saving/loading the new parsed project or
updating YAML, so the previous project, global address format and import binding
remain unchanged. This avoids duplicate registry entries. Downgrading a host with custom identities below 2026.9 is similarly
rejected. These guards preserve the previously active YAML and do not prevent
initial imports or same-format updates on older supported Core versions.

The oldest supported Core uses tuple runtime entity identifiers; newer versions
use objects with a UI marker. Both are supported, and the official configuration
store is always checked so disabled UI entities keep ownership of their group
addresses. Manual nested RGB group addresses are also preserved. Schema validation
runs on raw configuration; normalized enum values are only used for comparisons
and are never validated a second time.

## One-upload transaction

1. Cloud uploads the ETS file to the selected HA host, then calls
   `rexlite/knx/process_project` with `file_id`, optional `password`, and the
   SHA-256 `projectFingerprint` of the uploaded bytes.
2. Under the host deployment lock, HA checks the file hash and size, parses it
   with its installed `xknxproject`, saves through the official KNX project store,
   and persists the association between file hash and parsed project digest.
3. Cloud calls `rexlite/knx/deploy_project` with the same fingerprint. The host
   rejects a missing association or a project changed through another import.
4. The host maps supported metadata, skips uncertain mappings and addresses owned
   by manual YAML or UI entities, and validates the actual HA KNX schema.
5. The host journals originals, writes `.rexlite_knx/entities.yaml`, activates its
   reserved package, resolves HA's actual YAML/packages, and reloads KNX.
6. Success requires the effective disk configuration, runtime KNX configuration,
   current project digest, entity registry and state machine to agree. Registered
   but unavailable entities are counted separately. Failed activation/reload
   restores the journaled configuration; interrupted transactions recover on boot.

All four WebSocket commands are administrator-only. The status command
`rexlite/knx/project_deployment_status` accepts an optional fingerprint. It does
not treat an existing generated file or stale states as proof of deployment.

`rexlite/knx/gateway_scan` (also administrator-only) runs a three-second
KNXnet/IP search with xknx's `GatewayScanner` and returns every interface and
router that answers: `name`, `ip`, `port`, `individualAddress`,
`supportsTunnelling`, `supportsTunnellingTcp`, `supportsRouting`,
`tunnellingRequiresSecure` and `routingRequiresSecure`. It does not start HA's KNX
config flow, so it works whether or not KNX is configured and changes nothing.
Errors are `gateway_scan_unavailable` (xknx cannot be imported) and
`gateway_scan_failed`.

## Configuration ownership

The reserved package is `rexlite_knx_auto`; generated YAML and transactional
metadata live under `/config/.rexlite_knx/`. Do not edit these managed files.
Manual KNX includes remain in their existing files and are not serialized or
rewritten. Existing manual or UI entities using the same group addresses take
precedence and are reported as skipped.

Supported package activation forms include no `homeassistant` block, ordinary
mapping/empty `homeassistant.packages`, `!include_dir_named`, and
`!include_dir_merge_named`. Unsupported flow mappings, merge keys or an indirect
single-file packages include fail safely in preflight. Symlinks, hardlinks,
external concurrent edits, modified managed files, and conflicting reserved
package names are rejected.

## Acceptance evidence

Unit tests cover legacy/native/custom identities, repeated upload, Core upgrade,
manual-entity preservation, configuration conflicts, disabled UI entries,
file fingerprint mismatch, rollback, interrupted recovery and status verification.
The runtime scripts under `tests/knx_*_runtime_check.py` use official HA APIs in
an isolated temporary config directory. Identity checks instantiate real KNX
entities in every address format and verify official registry migration. The
deployment runtime check uses the real schema with manual light/climate YAML and
a controlled reload boundary. Passing these checks proves software
compatibility and file/configuration behavior, not physical device actuation.

The supplied REXLiTE-KNX-Demo project has 14 group addresses. The current strict
mapper recognizes two lights, one cover, and two sensors; the relative dimming
address lacks a safe standalone mapping and the scene address lacks an explicit
scene number. These are reported as skipped rather than inferred. A complete
41-entity mapping requires the corresponding ETS metadata; it is not inferred
from another host's manually named YAML files.

## ETS Function members (REXLiTE KNX Contract v1)

ETS Functions are the preferred mapping source. Members with a standard ETS
DatapointRole (`SwitchOnOff`, `InfoOnOff`, `DimmingValue`, `InfoDimmingValue`,
`MoveUpDown`, `StopStepUpDown`, `CurrentAbsolutePosition…Percentage`,
`TempRoom`, `HVACMode`, …) map by role. ETS has no standard role for colour
temperature, absolute blind position, slat angle or air-conditioner
mode/fan/setpoint, so such members may leave the role empty and use the exact
name `<Function name> <suffix>` (`色溫`, `色溫狀態`, `位置`, `葉片角度`, `模式`,
`風速`, `設定溫度`, `設定溫度狀態`, `室溫`, `運轉模式狀態`, …). The prefix must equal
the Function's own name and the exact-DPT guard still applies.

Unmapped Function members report a specific reason: `function_role_not_exposed`
for standard roles that are never an HA entity field (e.g. `DimmingControl`
relative dimming) and `unresolved_function_role` for members with neither a
supported role nor a matching name. The full producer-side specification lives
in the `rexlite-ets-addin` repository (`contract/knx-contract-v1.md`).
