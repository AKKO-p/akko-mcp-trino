#!/usr/bin/env bash
# The release tag to build, checked before anything is checked out or run from it: a version tag
# (v<major>.<minor>.<patch>, with an optional pre-release), present in the repository and
# reachable from the main branch. Prints tag= and sha= (the commit it names) for $GITHUB_OUTPUT.
# Run in this workflow's own checkout, with the history and the tags (fetch-depth: 0).
set -euo pipefail

tag=${1:?usage: bash scripts/release-tag.sh TAG [MAIN]}
main=${2:-origin/main}
if ! [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.]+)?$ ]]; then
  echo "release-tag.sh: '$tag' is not a version tag (v1.2.3)" >&2
  exit 1
fi
sha=$(git rev-parse --quiet --verify "refs/tags/$tag^{commit}") \
  || { echo "release-tag.sh: no tag $tag in the repository" >&2; exit 1; }
git merge-base --is-ancestor "$sha" "$main" \
  || { echo "release-tag.sh: $tag is not reachable from $main" >&2; exit 1; }
echo "tag=$tag"
echo "sha=$sha"
