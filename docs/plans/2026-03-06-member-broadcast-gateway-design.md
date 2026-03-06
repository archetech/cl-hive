# Member Broadcast Gateway Design

**Problem**

`cl-hive.py` has many independent member-broadcast paths. They currently vary in how they:
- select broadcast targets
- attach relay metadata
- serialize messages
- use direct `sendcustommsg` versus reliable delivery
- handle enqueue/send failures
- log results

That inconsistency creates correctness gaps. Some paths do not attach `_relay` metadata. Some paths use best-effort transport where missing a broadcast changes correctness. The transport policy is encoded ad hoc in each caller instead of one auditable place.

**Goal**

Introduce one internal relay-aware broadcast gateway for all member-broadcast paths in `cl-hive.py`, with explicit per-call-site transport policy:
- `reliability`: `reliable` or `direct`
- `failure_policy`: `fail_closed` or `best_effort`

**Non-Goals**

- Changing domain message schemas
- Moving signing logic out of message-specific callers
- Large transport-module extraction outside `cl-hive.py`
- Rewriting all non-broadcast peer-to-peer send paths

## Architecture

Add a single internal helper in `cl-hive.py` responsible for member broadcast transport normalization, tentatively named `_broadcast_member_message(...)`.

The helper owns:
- target resolution through `_get_broadcast_targets()` unless a caller provides an override
- transport metadata injection via `_prepare_broadcast_payload()`
- serialization or deserialize/normalize/reserialize for legacy byte-oriented callers
- dispatch mode selection: reliable/outbox versus direct `sendcustommsg`
- failure policy enforcement
- per-broadcast result accounting and logging

Callers keep responsibility for domain behavior only:
- gather data
- construct the signed domain payload
- choose transport policy
- invoke the helper

This preserves the existing signature boundary. `_relay` stays transport-only and is added after signing.

## Policy Model

The gateway supports a hybrid policy per call site.

`fail_closed + reliable`
- membership and state coordination broadcasts
- governance votes and proposals
- intent or coordination traffic where missing the broadcast changes correctness

`best_effort + reliable`
- non-critical broadcasts that still benefit from persistence and retry
- advisory or planner outputs where drops are undesirable but should not abort the originating action

`best_effort + direct`
- high-frequency telemetry and learning snapshots
- fee intelligence, pheromones, stigmergic markers, yield metrics, temporal patterns, corridor values, coverage analysis, similar fleet-learning broadcasts

`fail_closed + direct` is intentionally disallowed by design. If a path is correctness-critical, it should use the reliable/outbox mechanism.

## Data Flow

1. Caller builds the domain payload.
- validates prerequisites
- computes canonical signing payload
- signs the message
- does not attach `_relay`

2. Caller invokes `_broadcast_member_message(...)`.
- supplies `msg_type`
- supplies signed `payload`, or raw `message_bytes` only for legacy callers not yet converted
- supplies `reliability`, `failure_policy`, `log_label`, and optional target override

3. Gateway normalizes the wire payload.
- resolve targets
- if input is payload:
  - add `_relay` through `_prepare_broadcast_payload()`
  - serialize with `serialize(msg_type, payload)`
- if input is bytes:
  - deserialize
  - add `_relay`
  - reserialize
  - fail if deserialize/serialize cannot complete

4. Gateway dispatches.
- `reliable`: enqueue through the existing reliable/outbox path
- `direct`: send via `sendcustommsg`
- record per-target outcome counts

5. Gateway applies failure policy.
- `fail_closed`: caller sees failure if any required enqueue/send step fails
- `best_effort`: gateway logs and returns partial-failure stats without aborting caller

6. Gateway returns a structured result.
- attempted
- queued or sent
- failed
- mode
- policy
- targets

## Migration Strategy

Use an incremental migration, but classify all current broadcast sites first.

Phase 1: Introduce the gateway and migrate the already-touched fee-coordination broadcasters.
- fee intelligence
- stigmergic markers
- pheromones
- yield metrics
- temporal patterns
- corridor values
- positioning proposals
- physarum recommendations
- coverage analysis
- close proposals

Phase 2: Migrate the remaining member-broadcast paths in `cl-hive.py`.
- governance/member lifecycle broadcasts
- state and coordination broadcasts
- MCF coordination broadcasts where target fan-out is to members
- any remaining `_get_broadcast_targets()` or member-iteration send loops

Phase 3: Remove dead transport duplication.
- replace local send loops with gateway calls
- keep lower-level direct send helpers only for non-broadcast or non-member-specific cases

## Error Handling

The gateway should standardize transport failures.

For `best_effort` callers:
- log failures with `log_label`, target count, and failure count
- continue on per-target failures
- return result stats to the caller

For `fail_closed` callers:
- if zero eligible targets, return success with `attempted=0` only when that is semantically acceptable for the caller
- if reliable enqueue fails for any required target, return a failure result and let the caller abort or surface error
- if serialize/deserialize normalization fails, fail immediately before any send attempt

The gateway should also reject invalid policy combinations such as `fail_closed + direct`.

## Testing Strategy

Add regression tests around the gateway rather than only around callers.

Core transport tests:
- payload input gets `_relay` metadata before dispatch
- bytes input is deserialized, normalized, and reserialized with `_relay`
- `best_effort + direct` continues past per-target send failures
- `fail_closed + reliable` reports failure when enqueue fails
- invalid policy combinations are rejected

Migration tests:
- migrated intelligence broadcasters route through the gateway and preserve current message type/payload semantics
- correctness-critical broadcasters are classified `reliable`
- high-frequency telemetry broadcasters are classified `best_effort + direct`

Integration guardrails:
- existing focused fee-coordination tests remain green
- targeted tests cover at least one representative caller from each policy bucket

## Tradeoffs

This design is intentionally contained inside `cl-hive.py`.

Pros:
- one auditable transport policy surface
- consistent relay metadata handling
- consistent failure semantics
- lower risk than a full transport-layer extraction

Cons:
- `cl-hive.py` remains large
- some legacy callers may need deserialize/normalize/reserialize bridging until they are converted to payload-first calls
- reliable/outbox integration details may expose existing inconsistencies that need small follow-up fixes

## Recommended Outcome

Implement a unified member-broadcast gateway in `cl-hive.py`, classify every member-broadcast call site by transport policy, migrate all member broadcasts onto the gateway, and leave domain signing logic in the callers.
