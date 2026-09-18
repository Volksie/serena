# SPDX-License-Identifier: GPL-3.0-or-later

"""The `is_file` hint on `Project.is_ignored_path` must never change the verdict.

It exists only to let a caller that already knows whether a path is a file - a directory traversal
does - skip the `os.path.exists` / `os.path.isfile` / `os.path.isdir` calls the check would
otherwise make. A hint that could disagree with the filesystem would turn a performance change into
a correctness one, so every test here asserts equivalence rather than a specific verdict.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.project import Project
from solidlsp.ls_config import LanguageServerId


def _project(root: Path, ignored_paths: list[str] | None = None) -> Project:
    config = ProjectConfig(
        project_name="test_project",
        language_servers=[LanguageServerId.PYTHON],
        ignored_paths=ignored_paths or [],
        ignore_all_files_in_gitignore=False,
    )
    return Project(
        project_root=str(root),
        project_config=config,
        serena_config=SerenaConfig().with_headless_mode_overrides(),
    )


class TestIsFileHint:
    def setup_method(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        (self.root / "main.py").write_text("print('hello')")
        (self.root / "notes.txt").write_text("not a source file")
        os.makedirs(self.root / "build" / "nested", exist_ok=True)
        (self.root / "build" / "out.py").write_text("compiled")
        os.makedirs(self.root / "src", exist_ok=True)
        (self.root / "src" / "app.py").write_text("def app(): pass")
        # a directory whose name has a suffix, and a file without one: the two cases where guessing
        # file-ness from the name alone would go wrong
        os.makedirs(self.root / "assets.bundle", exist_ok=True)
        (self.root / "Makefile").write_text("all:\n")

    def teardown_method(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @pytest.mark.parametrize("ignored", [[], ["build"], ["*.txt"], ["src/**"], ["assets.bundle"]])
    @pytest.mark.parametrize("ignore_non_source_files", [False, True])
    def test_hint_matches_filesystem_verdict(self, ignored: list[str], ignore_non_source_files: bool) -> None:
        """For every path in the tree, the hinted call must agree with the unhinted one."""
        project = _project(self.root, ignored)
        checked = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name, is_file in [(d, False) for d in dirnames] + [(f, True) for f in filenames]:
                path = os.path.join(dirpath, name)
                without_hint = project.is_ignored_path(path, ignore_non_source_files=ignore_non_source_files)
                with_hint = project.is_ignored_path(path, ignore_non_source_files=ignore_non_source_files, is_file=is_file)
                assert with_hint == without_hint, f"hint changed the verdict for {path}"
                checked += 1
        assert checked > 0

    def test_directory_with_a_suffix_is_not_treated_as_a_file(self) -> None:
        """`assets.bundle` is a directory; the hint must be what decides that, not the name."""
        project = _project(self.root, ["assets.bundle"])
        path = str(self.root / "assets.bundle")
        assert project.is_ignored_path(path) is True
        assert project.is_ignored_path(path, is_file=False) is True

    def test_extensionless_file_is_not_treated_as_a_directory(self) -> None:
        project = _project(self.root)
        path = str(self.root / "Makefile")
        assert project.is_ignored_path(path, is_file=True) == project.is_ignored_path(path)

    def test_gather_source_files_is_unchanged_by_the_hint(self) -> None:
        """The caller that passes the hint must return exactly what it returned before."""
        project = _project(self.root, ["build"])
        gathered = sorted(project.gather_source_files())
        expected = sorted(
            os.path.relpath(os.path.join(dp, f), self.root)
            for dp, _, fs in os.walk(self.root)
            for f in fs
            if not project.is_ignored_path(os.path.join(dp, f), ignore_non_source_files=True)
        )
        assert gathered == expected
