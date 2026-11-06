#!/usr/bin/env bash
# The image settings of a release tag, as key=value lines for $GITHUB_OUTPUT, from the checkout of
# that tag given as the argument. A tag with the locks gives its own scripts/lock.sh; a tag from
# before them installs its own image-requirements.txt, and takes the platforms of this script's
# scripts/lock.sh, the release workflow's.
set -euo pipefail

tag=${1:?usage: bash scripts/release-image.sh <checkout of the tag>}
if [ -f "$tag/requirements/image.txt" ]; then
  bash "$tag/scripts/lock.sh" outputs | grep '^platforms='
elif [ -f "$tag/image-requirements.txt" ]; then
  echo "::notice::this tag predates requirements/image.txt: its Dockerfile installs its own image-requirements.txt" >&2
  bash "$(dirname "$0")/lock.sh" outputs | grep '^platforms='
else
  echo "release-image.sh: the tag holds neither requirements/image.txt nor image-requirements.txt" >&2
  exit 1
fi
