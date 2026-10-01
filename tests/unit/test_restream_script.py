#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.
#
#  Pyrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations as _annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/restream_kurigram_dev.sh"
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not SCRIPT.is_file() or shutil.which("git") is None,
        reason="Repository-only restream checks require the non-packaged script and git",
    ),
]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "fork"
    root.mkdir()
    (root / "scripts").mkdir()
    shutil.copy2(SCRIPT, root / "scripts" / SCRIPT.name)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    git("init", "-b", "dev")
    git("config", "user.name", "Kenzo02")
    git("config", "user.email", "Kenzo02@users.noreply.github.com")
    git("remote", "add", "origin", "git@github-kenzo02:Kenzo02/h.git")
    git("config", "remote.origin.pushurl", "git@github-kenzo02:Kenzo02/h.git")
    git("add", ".")
    git("commit", "-m", "fixture")
    git("update-ref", "refs/remotes/kurigram/main", git("rev-parse", "HEAD"))
    # Old remote HEAD/dev deliberately do not point to the selected main revision.
    git("update-ref", "refs/remotes/kurigram/dev", "HEAD")
    git("symbolic-ref", "refs/remotes/kurigram/HEAD", "refs/remotes/kurigram/dev")
    blockers = tmp_path / "bin"
    blockers.mkdir()
    ssh = blockers / "ssh"
    ssh.write_text("#!/bin/sh\nprintf 'UNEXPECTED_SSH\\n' >&2\nexit 99\n")
    ssh.chmod(0o755)
    env = dict(os.environ, PATH=str(blockers) + os.pathsep + os.environ["PATH"])
    return root, git, env


def invoke(repo, *args):
    root, _, env = repo
    return subprocess.run(
        ["bash", str(root / "scripts" / SCRIPT.name), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_check_is_local_read_only_and_targets_main(repo):
    root, git, _ = repo
    before = (git("show-ref"), git("status", "--short"), git("rev-parse", "HEAD"))
    result = invoke(repo, "--check")
    assert result.returncode == 0, result.stderr
    assert "already included" in result.stdout
    assert "kurigram/main" in result.stdout
    assert "No SSH, fetch, merge, backup ref or push" in result.stdout
    assert "UNEXPECTED_SSH" not in result.stderr
    assert before == (git("show-ref"), git("status", "--short"), git("rev-parse", "HEAD"))
    assert not (root / ".git/FETCH_HEAD").exists()
    assert not (root / ".git/MERGE_HEAD").exists()


def test_unknown_argument_never_starts_apply(repo):
    _, git, _ = repo
    before = git("show-ref")
    for args in [("--dry-run",), ("--check", "unexpected")]:
        result = invoke(repo, *args)
        assert result.returncode == 2
        assert "Usage:" in result.stderr
        assert "UNEXPECTED_SSH" not in result.stderr
    assert git("show-ref") == before


def test_dirty_tree_and_wrong_branch_fail_before_network(repo):
    root, git, _ = repo
    (root / "untracked.txt").write_text("preserve")
    result = invoke(repo, "--check")
    assert result.returncode == 1
    assert "not clean" in result.stderr
    assert "UNEXPECTED_SSH" not in result.stderr
    (root / "untracked.txt").unlink()
    git("checkout", "-b", "not-dev")
    result = invoke(repo, "--check")
    assert result.returncode == 1
    assert "expected 'dev'" in result.stderr
    assert "UNEXPECTED_SSH" not in result.stderr


def test_missing_main_does_not_silently_use_stale_dev(repo):
    _, git, _ = repo
    git("update-ref", "-d", "refs/remotes/kurigram/main")
    result = invoke(repo, "--check")
    assert result.returncode == 1
    assert "kurigram/main is missing" in result.stderr
    assert "UNEXPECTED_SSH" not in result.stderr
