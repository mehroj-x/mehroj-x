#!/usr/bin/env bash
# Copy the staged profile to GitLab as a normal commit: the files staged for GitHub, without
# .github, and README.md rewritten for GitLab (scripts/gitlab_readme.py). Run from the
# repository root after `git add`. Needs GITLAB_TOKEN (scopes: read_api, write_repository).
set -euo pipefail

: "${GITLAB_TOKEN:?Set GITLAB_TOKEN}"
project="${GITLAB_PROJECT:-mekhrojmajidov/mekhrojmajidov}"
branch="${GITLAB_BRANCH:-main}"
export GIT_AUTHOR_NAME="${GITLAB_AUTHOR_NAME:-Mehroj Majidov}"
export GIT_AUTHOR_EMAIL="${GITLAB_AUTHOR_EMAIL:-20033602-mekhrojmajidov@users.noreply.gitlab.com}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

tree="$(git write-tree)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The token goes in a header, not the URL, so it is never written to the clone's config.
auth=(-c "http.https://gitlab.com/.extraheader=Authorization: Basic $(printf 'oauth2:%s' "$GITLAB_TOKEN" | base64 -w0)")
git "${auth[@]}" clone -q --depth 1 --branch "$branch" "https://gitlab.com/$project.git" "$work"

git -C "$work" rm -rq --ignore-unmatch -- .
git archive "$tree" | tar -x -C "$work"
rm -rf "$work/.github"
python3 scripts/gitlab_readme.py README.md > "$work/README.md"

git -C "$work" add -A
if git -C "$work" diff --cached --quiet; then
  echo "GitLab is already up to date."
  exit 0
fi
git -C "$work" commit -q -m "Refresh profile stats"
git -C "$work" "${auth[@]}" push -q origin "HEAD:$branch"
echo "Pushed the profile to GitLab."
