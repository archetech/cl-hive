# Documentation Externalization Plan

**Status:** Proposed / Ready to execute  
**Last Updated:** 2026-02-19

---

## 1. Goal

Move the documentation corpus out of the `cl-hive` code repository into a dedicated docs repository so:

- docs can evolve independently from code release cadence
- large spec/planning changes do not create noisy code PRs
- contributors can collaborate on docs without touching runtime branches
- versioned docs can map cleanly to code release tags

Target canonical docs repo:

- `lightning-goats/hive-docs`

---

## 2. Scope

### Move to docs repo (canonical)

- `docs/planning/**`
- `docs/plugins/**`
- `docs/design/**`
- `docs/specs/**`
- `docs/security/**`
- `docs/testing/**`
- long-form reference guides in `docs/*.md`

### Keep in code repo (minimal local docs)

- short operator quickstart pointers
- immediate runtime setup references needed during clone/install
- changelog/release notes tied directly to code

Policy: code repo keeps concise "how to run this repo" docs; architecture/spec content lives in `hive-docs`.

---

## 3. Migration Strategy

### Phase A: Create and seed docs repo

1. Create `lightning-goats/hive-docs`.
2. Export `docs/` subtree from `cl-hive` with full history:
   - `scripts/docs/export-docs-subtree.sh <hive-docs-remote> main docs --push`
3. In `hive-docs`, add top-level landing page and navigation.

### Phase B: Switch canonical links

1. Update `cl-hive` `README.md` and `docs/README.md`:
   - point to canonical docs repo/site.
2. Keep in-repo docs temporarily as a transition mirror.
3. Add deprecation notice to local planning index pointing to canonical location.

### Phase C: Reduce local mirror

1. After 1-2 release cycles, remove duplicated long-form docs from `cl-hive`.
2. Keep only minimal operational docs + pointers.
3. Enforce docs policy via PR checklist.

---

## 4. Versioning Model

Docs should track code release boundaries explicitly:

- `hive-docs/main` -> latest
- `hive-docs/releases/vX.Y` (or tagged snapshots) -> frozen release docs
- each `cl-hive` release notes entry links to matching docs version

---

## 5. CI / Process

Recommended checks:

1. Link checker on docs repo.
2. PR template in code repos requiring:
   - docs impact assessment
   - linked docs PR when behavior/config/rpc changes.
3. Optional mirror sync job (docs repo -> code repo pointer updates only).

---

## 6. Rollback and Safety

- Migration is non-destructive: `git subtree split` preserves history.
- Keep local docs mirror during transition until docs repo stability is confirmed.
- Do not delete local docs until:
  - docs repo branch protections are in place
  - docs publishing pipeline is green
  - operator runbooks have validated links.

---

## 7. Execution Checklist

1. Create `hive-docs` repo and protections.
2. Run subtree export + push.
3. Open PR in `hive-docs` to set docs navigation.
4. Update `cl-hive` pointers (`README.md`, `docs/README.md`, planning index note).
5. Announce canonical docs URL to contributors.
6. Start transition period (mirror mode).
7. Prune local duplicate docs when criteria are met.
