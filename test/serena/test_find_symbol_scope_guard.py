# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the find_symbol scope guard (LOCAL PATCH; oraios/serena#2076, closed upstream as not
planned on 2026-09-19, so this stays fork-local).

The guard's job is to refuse a search that would not finish, and otherwise to stay entirely out of
the way. Both halves matter equally: a guard that refuses a legitimate scope is worse than no guard,
because the caller has no way to make progress.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.project import Project
from serena.repl.api import lsp_api
from serena.repl.api.lsp_api import _find_symbol_scope_refusal
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


class TestFindSymbolScopeGuard:
    def setup_method(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.root = Path(self.test_dir)
        os.makedirs(self.root / "small", exist_ok=True)
        for i in range(3):
            (self.root / "small" / f"mod{i}.py").write_text("x = 1\n")
        os.makedirs(self.root / "big", exist_ok=True)
        for i in range(40):
            (self.root / "big" / f"mod{i}.py").write_text("x = 1\n")
        os.makedirs(self.root / "vendored", exist_ok=True)
        for i in range(40):
            (self.root / "vendored" / f"mod{i}.py").write_text("x = 1\n")

    def teardown_method(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @pytest.mark.parametrize("scope", ["", ".", "   "])
    def test_unscoped_search_is_refused(self, scope: str) -> None:
        refusal = _find_symbol_scope_refusal(_project(self.root), scope)
        assert refusal is not None
        assert "relative_path" in refusal

    def test_small_directory_is_allowed(self) -> None:
        assert _find_symbol_scope_refusal(_project(self.root), "small") is None

    def test_single_file_is_allowed(self) -> None:
        assert _find_symbol_scope_refusal(_project(self.root), "small/mod0.py") is None

    def test_missing_path_is_allowed(self) -> None:
        """The language server's own error is more useful than ours for a path that is not there."""
        assert _find_symbol_scope_refusal(_project(self.root), "no/such/dir") is None

    def test_large_directory_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lsp_api, "_FIND_SYMBOL_MAX_SCOPE_FILES", 10)
        refusal = _find_symbol_scope_refusal(_project(self.root), "big")
        assert refusal is not None
        assert "more than 10 source files" in refusal

    def test_ignored_files_do_not_count_towards_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A directory that is only large because of excluded files must still be searchable."""
        monkeypatch.setattr(lsp_api, "_FIND_SYMBOL_MAX_SCOPE_FILES", 10)
        project = _project(self.root, ignored_paths=["vendored"])
        assert _find_symbol_scope_refusal(project, "vendored") is None

    def test_guard_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lsp_api, "_FIND_SYMBOL_MAX_SCOPE_FILES", 0)
        assert _find_symbol_scope_refusal(_project(self.root), "") is None
        assert _find_symbol_scope_refusal(_project(self.root), "big") is None

    def test_counting_stops_at_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The check must cost the limit, not the size of the directory.

        Asserted by counting how many paths the ignore predicate is asked about: with a limit of 5
        over 40 files, a guard that walked the whole directory before deciding would ask about all
        of them.
        """
        monkeypatch.setattr(lsp_api, "_FIND_SYMBOL_MAX_SCOPE_FILES", 5)
        project = _project(self.root)
        seen = []
        original = project.is_ignored_path

        def counting(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(path)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(project, "is_ignored_path", counting)
        assert _find_symbol_scope_refusal(project, "big") is not None
        assert len(seen) < 40, f"guard inspected {len(seen)} paths for a limit of 5"
