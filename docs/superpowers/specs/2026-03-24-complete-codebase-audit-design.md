# Complete cl-hive Codebase Audit Design

## Goal

Perform a comprehensive audit of the cl-hive codebase covering runtime correctness, architecture alignment, and security. Find issues, fix critical/high severity immediately, defer medium/low for operator review.

## Context

cl-hive has been through a major trusted-fleet simplification. ~150K lines were deleted across dozens of commits. The remaining codebase is ~32K lines across 32 modules + a 4K line main plugin file. Several runtime bugs have already been found in production (missing DB methods, undefined variables, stale dict key access). A systematic audit is needed to find remaining issues before the codebase is considered stable.

## Prerequisites

Before starting the audit:
1. Establish a passing test baseline — identify and fix any currently-failing tests
2. Verify CLAUDE.md module list matches actual modules/ contents (known stale: lists deleted modules, wrong counts)

## Approach

Three sequential sub-audits. Sub-audit 1 fixes land before Sub-audit 2 begins (fixes change the call graph, affecting dead-code analysis). Each sub-audit produces a findings table and immediate fixes for critical/high issues.

### Sub-audit 1: Runtime Correctness

Find code that will crash or silently fail at runtime.

**What to check:**

**Database call audit:**
- Every `database.method_name()` call — verify method exists in database.py
- Cross-reference caller arguments (count, types, keyword names) against actual method signatures, not just existence

**Dict key access:**
- Every `dict["key"]` access on data from RPC, protocol, or DB — should use `.get()` with safe defaults

**Removed references:**
- Every reference to variables/functions/imports that may have been removed during simplification
- RPC methods that reference deleted modules or return stub responses for removed features

**Dependency injection completeness:**
- Cross-reference every module-level `variable = None` declaration in `protocol_handlers.py` and `background_loops.py` against the actual dicts passed by `init_protocol_handlers()` and `init_background_loops()` in cl-hive.py
- Verify no injected-but-unused names remain (leftover from simplification)
- Verify no referenced-but-not-injected names exist (the class of bug behind the `initial_tier` NameError)

**Background loops:**
- Exception handling — one unhandled exception kills the entire loop thread
- Loop iteration duration — identify loops that do many RPC-blocking operations in a single iteration (starvation risk)

**Error handling:**
- Try/except blocks that swallow *crashable* errors (hiding NameError, AttributeError, etc.)
- Leave intentional fail-open patterns (e.g., "if manager unavailable, skip") to Sub-audit 2

**Method:** Automated grep + manual review of each module. Cross-reference callers vs definitions.

### Sub-audit 2: Architecture Alignment

Verify the codebase matches the lean hint-only trusted fleet product.

**What to check:**
- Dead functions (defined but never called from any code path)
- Unused imports
- RPCs that duplicate each other or serve removed functionality
- Config keys that control nothing in the current code
- CLAUDE.md accuracy — module list, table counts, pattern descriptions, test file references
- Docs/comments describing removed features (governance modes, admin roles, CLBOSS, comms, archon, anticipatory, routing_intelligence, etc.)
- Test files testing removed functionality or importing deleted modules
- Modules that are disproportionately large vs their actual purpose
- Stale TODO/FIXME/HACK comments
- Try/except blocks that swallow no-op errors (intentional but potentially masking drift)

**Method:** Static analysis (grep for function defs vs call sites), doc review, module-by-module purpose check.

### Sub-audit 3: Security

Review attack surface and trust boundaries.

**What to check:**
- Protocol message validation — can malformed payloads crash the node?
- Membership flow — HELLO/CHALLENGE/ATTEST/WELCOME chain completeness
- Outbound HELLO tracking — verify the unsolicited-WELCOME fix is complete
- Signature verification — are all state-changing messages signed and verified?
- Rate limiting — are all inbound message types rate-limited?
- Resource exhaustion — unbounded dicts, lists, or caches that grow without limit
- Input sanitization — RPC parameters passed to SQL or protocol without validation
- Relay amplification — can an attacker cause message multiplication?
- Ban bypass — can a banned peer rejoin through protocol-level tricks?
- `globals().update(deps)` trust assumption — verify this is purely startup-time, document the pattern
- Protocol version compatibility — can mixed v1/v2 nodes cause state divergence?

**Method:** Code path tracing through protocol_handlers.py, handshake.py, and message dispatch in cl-hive.py.

## Output Format

Each sub-audit produces:

| Column | Description |
|--------|-------------|
| File | Module or file path |
| Line(s) | Approximate line numbers |
| Issue | Concrete description |
| Severity | Critical / High / Medium / Low |
| Action | Fix now / Defer / Document |

**Severity definitions:**
- **Critical:** Will crash the plugin or corrupt state in normal operation. Includes silent data corruption that could propagate across fleet via gossip.
- **High:** Will crash under specific but realistic conditions, or silently produces wrong results
- **Medium:** Code smell, dead code, or minor inconsistency that doesn't affect runtime
- **Low:** Cosmetic, naming, comment staleness

**Actions:**
- **Fix now:** Critical and High issues are fixed immediately with commits
- **Defer:** Medium and Low issues are documented for operator review
- **Document:** Issues that are known limitations, not bugs (e.g., "gossip is best-effort")

**Commit batching:** Group fixes by module or functional area (e.g., "all protocol_handlers.py fixes" as one commit).

## Constraints

- Do not refactor working code during the audit — fix bugs, don't redesign
- Do not add features — this is a quality pass
- Commit fixes in small batches per sub-audit, not one giant commit
- Run tests after each batch of fixes
- Do not touch test files unless they test removed functionality or import deleted modules

## Success Criteria

After all three sub-audits:
- Zero known critical/high runtime issues
- No references to deleted functions, methods, or variables
- All dependency injection dicts are complete and accurate
- All background loops have proper exception handling
- All protocol messages are validated before processing
- CLAUDE.md and other docs match the actual codebase
- Security findings documented with severity assessment
