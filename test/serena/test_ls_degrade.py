# SPDX-License-Identifier: GPL-3.0-or-later

"""A/B test for per-language degradation on startup (LOCAL PATCH, see serena/ls_manager.py).

Upstream joins every language-server startup thread without a timeout and fails the whole set if any
one raises. Both halves bite on a large repository: a server that never returns hangs the manager for
ever, and a server that cannot load takes the working languages down with it. Observed on the CodeMem
tree, where the C# server stalled loading 238 `.csproj` under an engine checkout and C++ queries then
timed out at 45s with clangd sitting at 0.0s CPU, never having been asked anything.

The patch promotes what the Pyright server already did for its own initial analysis - wait, warn,
proceed - to the rule for every server.

Three things are asserted, and the third is the one that makes the first two safe:

1. a server that never returns does not prevent the others being served;
2. a server that raises does not stop the servers that started;
3. a query for a language whose server is missing RAISES, rather than falling back to whichever
   server happens to be first. An empty result from the wrong server reads as "no such symbol", and
   absence must not be indistinguishable from ignorance.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from serena.ls_manager import (
    LanguageServerManager,
    LanguageServerManagerInitialisationError,
    LanguageServerUnavailableError,
)
from solidlsp.ls_config import FilenameMatcher


class _FakeId:
    """Stands in for a LanguageServerId: a key, and the extensions it claims."""

    def __init__(self, key: str, *extensions: str):
        self._key = key
        self._matcher = FilenameMatcher(*extensions)

    def get_key(self) -> str:
        return self._key

    def get_source_fn_matcher(self) -> FilenameMatcher:
        return self._matcher


class _FakeFactory:
    """Creates servers that start fine, raise, or never return - one per behaviour."""

    def __init__(self, behaviour: dict[str, str], hang: threading.Event):
        self._behaviour = behaviour
        self._hang = hang

    def create_language_server(self, ls_id):  # noqa: ANN001
        how = self._behaviour[ls_id.get_key()]
        ls = MagicMock()
        ls.ls_id = ls_id

        def start() -> None:
            if how == "raise":
                raise RuntimeError("synthetic start failure")
            if how == "hang":
                # Released in the test's finally, so the thread cannot outlive the test.
                self._hang.wait(timeout=60)

        ls.start.side_effect = start
        ls.is_running.return_value = True
        # Only the healthy server claims anything; the unavailable ones are never asked, which is the
        # point - their claim has to come from the ID, not from an instance that does not exist.
        ls.is_ignored_path.side_effect = lambda p, **kw: not p.endswith(".cpp")
        return ls


CPP = _FakeId("cpp", ".cpp", ".h")
CSHARP = _FakeId("csharp", ".cs")
PYTHON = _FakeId("python", ".py")


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch):
    import serena.ls_manager as m

    monkeypatch.setattr(m, "LS_STARTUP_TIMEOUT_SECONDS", 1.0)
    return m


def _make(monkeypatch: pytest.MonkeyPatch, behaviour: dict[str, str], ids: list[_FakeId]):
    hang = threading.Event()
    factory = _FakeFactory(behaviour, hang)
    project = MagicMock()
    monkeypatch.setattr("serena.ls_manager.LanguageServerFileChangeNotifier", lambda *a, **kw: MagicMock())
    return LanguageServerManager.from_languages(ids, factory, project), hang


def test_a_hanging_server_does_not_block_the_others(short_timeout, monkeypatch: pytest.MonkeyPatch) -> None:
    started = time.monotonic()
    mgr, hang = _make(monkeypatch, {"cpp": "ok", "csharp": "hang"}, [CPP, CSHARP])
    try:
        elapsed = time.monotonic() - started
        # Upstream would still be inside join() here, for ever.
        assert elapsed < 30, f"startup took {elapsed:.1f}s; the bounded join did not apply"
        assert "cpp" not in mgr.unavailable_languages
        assert "csharp" in mgr.unavailable_languages
        assert "within" in mgr.unavailable_languages["csharp"]
    finally:
        hang.set()


def test_a_failing_server_does_not_take_down_the_working_ones(short_timeout, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, hang = _make(monkeypatch, {"cpp": "ok", "csharp": "raise"}, [CPP, CSHARP])
    try:
        assert "csharp" in mgr.unavailable_languages
        assert "synthetic start failure" in mgr.unavailable_languages["csharp"]
        # The working server is still there. Upstream stops it and raises.
        assert mgr.unavailable_languages.keys() == {"csharp"}
    finally:
        hang.set()


def test_a_query_for_a_missing_language_raises_rather_than_falling_back(
    short_timeout, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, hang = _make(monkeypatch, {"cpp": "ok", "csharp": "raise"}, [CPP, CSHARP])
    try:
        # The dangerous case: a .cs file with no C# server. Falling back to clangd returns nothing,
        # and nothing reads as "no such symbol".
        with pytest.raises(LanguageServerUnavailableError) as excinfo:
            mgr.get_language_server("Engine/Source/Programs/UnrealBuildTool/Foo.cs")
        assert "csharp" in str(excinfo.value)
        assert "no server looked" in str(excinfo.value).lower()
        # A file the healthy server claims still resolves normally.
        assert mgr.get_language_server("MemBench/Source/Foo.cpp") is not None
    finally:
        hang.set()


def test_the_deadline_is_shared_not_per_server(short_timeout, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two hanging servers must not cost twice the timeout.

    The first version of this patch put the timeout on each join, and because the joins run in
    sequence that made the real bound N x timeout - a number nobody chose and which, at five
    configured languages, turned a 300s limit into a 25-minute worst case.
    """
    started = time.monotonic()
    mgr, hang = _make(monkeypatch, {"cpp": "ok", "csharp": "hang", "python": "hang"}, [CPP, CSHARP, PYTHON])
    try:
        elapsed = time.monotonic() - started
        # One shared 1s deadline. Per-join would be ~2s here and would scale with the language count.
        assert elapsed < 1.8, f"two hanging servers took {elapsed:.2f}s; the deadline is still per-join"
        assert set(mgr.unavailable_languages) == {"csharp", "python"}
    finally:
        hang.set()


def test_everything_failing_is_still_an_error(short_timeout, monkeypatch: pytest.MonkeyPatch) -> None:
    # Degrading per language must not degrade into serving nothing silently.
    with pytest.raises(LanguageServerManagerInitialisationError):
        _make(monkeypatch, {"python": "raise"}, [PYTHON])
