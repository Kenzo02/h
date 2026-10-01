#!/usr/bin/env bash
set -euo pipefail

check_only=false
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--check" ) ]]; then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi
if [[ $# -eq 1 ]]; then
  check_only=true
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"
upstream_ref="kurigram/main"
branch="$(git branch --show-current)"
status_output="$(git status --short)"
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
if [[ "$git_user_name" != "Kenzo02" || "$git_user_email" != "Kenzo02@users.noreply.github.com" ]]; then
  echo "error: repo-local identity must be Kenzo02 / Kenzo02@users.noreply.github.com" >&2
  exit 1
fi
if [[ "$origin_fetch_url" != "$expected_origin_url" || "$origin_push_url" != "$expected_origin_url" ]]; then
  echo "error: origin fetch and push URLs must be ${expected_origin_url}" >&2
  exit 1
fi

if ! "$check_only"; then
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
fi

git rev-parse --verify "${upstream_ref}^{commit}" >/dev/null || {
  echo "error: ${upstream_ref} is missing; fetch the upstream main branch" >&2
  exit 1
}
echo "==> upstream commits not yet in dev (${upstream_ref})"
git log --oneline "dev..${upstream_ref}"
if "$check_only"; then
  if git merge-base --is-ancestor "$upstream_ref" dev; then
    echo "Upstream main is already included in dev."
  else
    echo "Upstream main has unmerged commits; review the delta and conflicts before applying."
  fi
  echo "Local check complete. No SSH, fetch, merge, backup ref or push."
  exit 0
fi

backup_branch="backup/dev-before-kurigram-$(date +%Y-%m-%d-%H%M%S)"
if git show-ref --verify --quiet "refs/heads/${backup_branch}"; then
  echo "error: backup branch ${backup_branch} already exists" >&2
  exit 1
fi
echo "==> creating backup branch ${backup_branch}"
git branch "$backup_branch"
echo "==> merging ${upstream_ref} into dev"
if ! git merge --no-ff "$upstream_ref"; then
  if [[ -n "$(git diff --name-only --diff-filter=U)" ]]; then
    echo "merge has conflicts; resolve them manually, run tests, then complete the merge" >&2
  else
    echo "error: git merge failed before producing merge conflicts" >&2
  fi
  exit 1
fi
echo "merge completed without conflicts"
echo "next: run tests, inspect history, then push only when authorized: git push origin dev"
