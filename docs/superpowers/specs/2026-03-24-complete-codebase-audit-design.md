# Complete cl-hive Codebase Audit Design

## Goal

Perform a comprehensive audit of the cl-hive codebase covering runtime correctness, architecture alignment, and security. Find issues, fix critical/high severity immediately, defer medium/low for operator review.

## Context

cl-hive has been through a major trusted-fleet simplification. ~150K lines were deleted across dozens of commits. The remaining codebase is ~32K lines across 32 modules + a 4K line main plugin file. Several runtime bugs have already been found in production (missing DB methods, undefined variables, stale dict key access). A systematic audit is needed to find remaining issues before the codebase is considered stable.

## Approach

Three sequential sub-audits, each producing a findings table and immediate fixes for critical/high issues.

### Sub-audit 1: Runtime Correctness

Find code that will crash or silently fail at runtime.

**What to check:**
- Every `database.method_name()` call — verify method exists in database.py
- Every `dict["key"]` access on data from RPC, protocol, or DB — should use `.get()`
- Every reference to variables/functions/imports that may have been removed during simplification
- Background loop exception handling — one unhandled exception kills the entire loop thread
- Try/except blocks that swallow errors silently (hiding real bugs vs intentional fail-open)
- Global variable references in protocol_handlers.py init injection — verify all injected names are still set

**Method:** Automated grep + manual review of each module. Cross-reference callers vs definitions.

### Sub-audit 2: Architecture Alignment

Verify the codebase matches the lean hint-only trusted fleet product.

**What to check:**
- Dead functions (defined but never called from any code path)
- Unused imports
- RPCs that duplicate each other or serve removed functionality
- Config keys that control nothing in the current code
- Docs/comments describing removed features (governance modes, admin roles, CLBOSS, comms, archon, etc.)
- Test files testing removed functionality
- Modules that are disproportionately large vs their actual purpose
- Stale TODO/FIXME/HACK comments

**Method:** Static analysis (grep for function defs vs call sites), doc review, module-by-module purpose check.

### Sub-audit 3: Security

Review attack surface and trust boundaries.

**What to check:**
- Protocol message validation — can malformed payloads crash the node?
- Membership flow — HELLO/CHALLENGE/ATTEST/WELCOME chain completeness
- Signature verification — are all state-changing messages signed and verified?
- Rate limiting — are all inbound message types rate-limited?
- Resource exhaustion — unbounded dicts, lists, or caches that grow without limit
- Input sanitization — RPC parameters passed to SQL or protocol without validation
- Relay amplification — can an attacker cause message multiplication?
- Ban bypass — can a banned peer rejoin through protocol-level tricks?

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
- **Critical:** Will crash the plugin or corrupt state in normal operation
- **High:** Will crash under specific but realistic conditions, or silently produces wrong results
- **Medium:** Code smell, dead code, or minor inconsistency that doesn't affect runtime
- **Low:** Cosmetic, naming, comment staleness

**Actions:**
- **Fix now:** Critical and High issues are fixed immediately with commits
- **Defer:** Medium and Low issues are documented for operator review
- **Document:** Issues that are known limitations, not bugs (e.g., "gossip is best-effort")

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
- All background loops have proper exception handling
- All protocol messages are validated before processing
- Docs/config match the actual codebase
- Security findings documented with severity assessment
