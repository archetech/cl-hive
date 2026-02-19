#!/usr/bin/env bash
set -euo pipefail

# Export a subtree with history to seed an external docs repository.
#
# Usage:
#   scripts/docs/export-docs-subtree.sh <remote_or_url> [target_branch] [prefix] [--push]
#
# Examples:
#   scripts/docs/export-docs-subtree.sh git@github.com:lightning-goats/hive-docs.git
#   scripts/docs/export-docs-subtree.sh origin main docs --push

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <remote_or_url> [target_branch] [prefix] [--push]" >&2
  exit 1
fi

REMOTE_OR_URL="$1"
TARGET_BRANCH="${2:-main}"
PREFIX="${3:-docs}"
PUSH_FLAG="${4:-}"

if [[ "${PUSH_FLAG:-}" != "" && "${PUSH_FLAG}" != "--push" ]]; then
  echo "Invalid 4th argument: ${PUSH_FLAG}. Expected '--push' or omitted." >&2
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not inside a git repository." >&2
  exit 1
fi

if ! git ls-tree -d --name-only HEAD "${PREFIX}" | grep -qx "${PREFIX}"; then
  echo "Prefix not found in HEAD: ${PREFIX}" >&2
  exit 1
fi

STAMP="$(date +%Y%m%d%H%M%S)"
TMP_BRANCH="docs-export-${STAMP}"

echo "Creating subtree branch '${TMP_BRANCH}' from prefix '${PREFIX}'..."
git subtree split --prefix "${PREFIX}" -b "${TMP_BRANCH}"

echo
if [[ "${PUSH_FLAG}" == "--push" ]]; then
  echo "Pushing ${TMP_BRANCH} -> ${TARGET_BRANCH} to ${REMOTE_OR_URL}..."
  git push "${REMOTE_OR_URL}" "${TMP_BRANCH}:${TARGET_BRANCH}"
  echo "Push complete."
else
  echo "Dry-run complete. To push:"
  echo "  git push \"${REMOTE_OR_URL}\" \"${TMP_BRANCH}:${TARGET_BRANCH}\""
fi

echo
echo "Cleanup temporary branch when done:"
echo "  git branch -D ${TMP_BRANCH}"
