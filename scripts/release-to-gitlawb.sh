#!/usr/bin/env bash
# Mirror a tagged release to gitlawb. Run locally — your gitlawb Ed25519
# identity key never leaves your machine. See CONTRIBUTING.md for the rationale.
#
# Usage: scripts/release-to-gitlawb.sh v0.2.0
set -euo pipefail

TAG="${1:?usage: $(basename "$0") <tag>   e.g. v0.2.0}"

# Sanity-check the gitlawb remote exists before attempting the push.
if ! git remote get-url gitlawb >/dev/null 2>&1; then
    echo "error: no 'gitlawb' remote configured." >&2
    echo "       Add it with: git remote add gitlawb gitlawb://<YOUR_DID>/tckr" >&2
    echo "       See CONTRIBUTING.md > gitlawb mirror setup for the full one-time flow." >&2
    exit 1
fi

# Confirm the tag exists locally.
if ! git rev-parse --verify "refs/tags/${TAG}" >/dev/null 2>&1; then
    echo "error: tag ${TAG} not found locally." >&2
    echo "       Did you forget to 'git tag ${TAG}' before running this?" >&2
    exit 1
fi

echo "mirroring ${TAG} to gitlawb..."
git push gitlawb main
git push gitlawb "${TAG}"
echo "done."
