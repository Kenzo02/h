#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

branch="$(git branch --show-current)"
status_output="$(git status --short)"
backup_suffix="$(date +%Y-%m-%d-%H%M%S)"
backup_branch="backup/dev-before-kurigram-${backup_suffix}"
git_user_name="$(git config --local --get user.name || true)"
git_user_email="$(git config --local --get user.email || true)"
expected_origin_url="git@github-kenzo02:Kenzo02/h.git"
origin_fetch_url="$(git config --local --get remote.origin.url || true)"
origin_push_url="$(git config --local --get remote.origin.pushurl || true)"

if [[ "$branch" != "dev" ]]; then
  echo "error: current branch is '$branch', expected 'dev'" >&2
  exit 1
fi

if [[ -n "$status_output" ]]; then
  echo "error: working tree is not clean" >&2
  git status --short
  exit 1
fi

if [[ "$git_user_name" != "Kenzo02" ]]; then
  echo "error: repo-local git user.name is not Kenzo02" >&2
  exit 1
fi

if [[ "$git_user_email" != "Kenzo02@users.noreply.github.com" ]]; then
  echo "error: repo-local git user.email is not Kenzo02@users.noreply.github.com" >&2
  exit 1
fi

if [[ "$origin_fetch_url" != "$expected_origin_url" ]]; then
  echo "error: remote.origin.url must be ${expected_origin_url} (got ${origin_fetch_url:-<unset>})" >&2
  exit 1
fi

if [[ "$origin_push_url" != "$expected_origin_url" ]]; then
  echo "error: remote.origin.pushurl must be ${expected_origin_url} (got ${origin_push_url:-<unset>})" >&2
  exit 1
fi

echo "==> verifying GitHub alias"
ssh_output="$(ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github-kenzo02 2>&1 || true)"
printf '%s\n' "$ssh_output"

if [[ "$ssh_output" != *"Hi Kenzo02!"* ]]; then
  echo "error: github-kenzo02 did not authenticate as Kenzo02" >&2
  exit 1
fi

echo "==> fetching remotes"
git fetch origin --prune
git fetch kurigram --prune

echo "==> upstream commits not yet in dev"
git log --oneline dev..kurigram/dev || true

if git show-ref --verify --quiet "refs/heads/${backup_branch}"; then
  echo "error: backup branch ${backup_branch} already exists" >&2
  exit 1
fi

echo "==> creating backup branch ${backup_branch}"
git branch "$backup_branch"

echo "==> merging kurigram/dev into dev"
if ! git merge --no-ff kurigram/dev; then
  if git diff --name-only --diff-filter=U | grep -q .; then
    echo "merge has conflicts; resolve them manually, run tests, then complete the merge" >&2
    exit 1
  fi

  echo "error: git merge failed before producing merge conflicts" >&2
  exit 1
fi

echo "merge completed without conflicts"
echo "next: run tests, inspect history, then push with: git push origin dev"