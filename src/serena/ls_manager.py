# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os.path
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from sensai.util.logging import LogTime

from serena.config.serena_config import ProjectConfig, SerenaPaths
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerIdLike
from solidlsp.lsp_protocol_handler.lsp_types import DidChangeWatchedFilesParams, FileChangeType, FileEvent
from solidlsp.settings import SolidLSPSettings

if TYPE_CHECKING:
    from .project import Project

log = logging.getLogger(__name__)

# How long a single language server gets to finish starting before the others stop waiting for it.
#
# Generous on purpose: a cold clangd over a large tree, or a C# server restoring packages, legitimately
# takes minutes, and killing those off early would trade a hang for a different kind of wrong answer.
# What this bounds is the pathological case - a server that will never return - so the point is that the
# number exists at all, not that it is tight. Overridable for a machine where a server is simply slower.
LS_STARTUP_TIMEOUT_SECONDS = float(os.environ.get("SERENA_LS_STARTUP_TIMEOUT", "300"))


# Startup is the one phase where nothing is visible and everything can go wrong: servers are spawned in
# parallel, some take minutes, and the default log level on a long-lived server is WARNING, so INFO
# milestones are invisible exactly when someone is trying to work out where a start got to. On this tree
# that cost hours - a C# server that was slow rather than dead looked identical to a wedge, because the
# last line in the log was an unrelated project-load error and nothing said which phase was running.
#
# So the startup trace is emitted at WARNING by default. It is roughly a dozen lines per process start,
# it is not repeated per request, and being able to read "which language, which phase, how long" off a
# log the operator already has beats being tidy about levels. Set SERENA_STARTUP_TRACE=0 to drop it to
# INFO once a deployment is boring.
_STARTUP_TRACE_LEVEL = logging.INFO if os.environ.get("SERENA_STARTUP_TRACE", "1") in ("0", "false", "no") else logging.WARNING


def _trace(msg: str, *args: object) -> None:
    log.log(_STARTUP_TRACE_LEVEL, "[startup] " + msg, *args)


def _stop_quietly(ls: SolidLanguageServer, key: str) -> None:
    """Stop a language server that timed out, without letting its failure propagate.

    A server we gave up waiting for is by definition misbehaving, so its `stop()` may hang or raise
    too. Neither must reach the caller: at this point the manager has already decided this language is
    unavailable, and the only thing left is to avoid leaking the subprocess it spawned.
    """
    try:
        ls.stop()
        log.info("Stopped the language server for %s after it failed to finish starting", key)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not stop the stuck language server for %s: %s", key, e)


def _ls_id_claims_path(ls_id: LanguageServerIdLike, relative_path: str) -> bool:
    """Would this language server have served this file, had it started?

    Answered from the id's own filename matcher rather than from a server instance, because the whole
    point is that no instance exists. A matcher that cannot be obtained means "do not claim it" - a
    wrong claim here turns a working fallback into a spurious error.
    """
    try:
        return ls_id.get_source_fn_matcher().is_relevant_filename(relative_path)
    except Exception:  # noqa: BLE001 - a broken matcher must not break routing
        return False


class LanguageServerManagerInitialisationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class LanguageServerUnavailableError(Exception):
    """Raised when a query routes to a language whose server did not start.

    Deliberately an error rather than an empty result: absence and ignorance must not look the same.
    An empty list means the server looked and found nothing; this means nobody looked.
    """

    def __init__(self, ls_key: str, reason: str):
        super().__init__(
            f"The {ls_key} language server did not start, so this query cannot be answered for that "
            f"language: {reason}. This is NOT a statement that the symbol does not exist - no server "
            f"looked. Other languages are unaffected; see the startup warnings in the server log."
        )
        self.ls_key = ls_key
        self.reason = reason


class LanguageServerFactory:
    def __init__(
        self,
        project_root: str,
        project_config: ProjectConfig,
        project_data_path: str,
        encoding: str,
        ignored_patterns: list[str],
        ls_timeout: float | None = None,
        ls_specific_settings: dict | None = None,
        trace_lsp_communication: bool = False,
    ):
        self.project_root = project_root
        self.project_config = project_config
        self.project_data_path = project_data_path
        self.encoding = encoding
        self.ignored_patterns = ignored_patterns
        self.ls_timeout = ls_timeout
        self.ls_specific_settings = ls_specific_settings
        self.trace_lsp_communication = trace_lsp_communication

    def create_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        ls_config = LanguageServerConfig(
            workspace_folders=self.project_config.ls_workspace_folders,
            additional_workspace_folders=self.project_config.ls_additional_workspace_folders,
            ls_id=ls_id,
            ignored_paths=self.ignored_patterns,
            trace_lsp_communication=self.trace_lsp_communication,
            encoding=self.encoding,
        )

        log.info(f"Creating language server instance for {self.project_root}, language={ls_id}.")
        return SolidLanguageServer.create(
            ls_config,
            self.project_root,
            timeout=self.ls_timeout,
            solidlsp_settings=SolidLSPSettings(
                solidlsp_dir=SerenaPaths().serena_user_home_dir,
                project_data_path=self.project_data_path,
                ls_specific_settings=self.ls_specific_settings or {},
            ),
        )


class LanguageServerManager:
    """
    Manages one or more language servers for a project.
    """

    def __init__(
        self,
        language_servers: dict[LanguageServerIdLike, SolidLanguageServer],
        language_server_factory: LanguageServerFactory,
        project: "Project",
        unavailable: dict[LanguageServerIdLike, str] | None = None,
    ) -> None:
        """
        :param language_servers: a mapping from language to language server; the servers are assumed to be already started.
            The first server in the iteration order is used as the default server.
            All servers are assumed to serve the same project root.
        :param language_server_factory: factory for language server creation; if None, dynamic (re)creation of language servers
            is not supported
        """
        self._language_servers = language_servers
        self._language_server_factory = language_server_factory
        # Languages whose server did not start, kept so a query for one can say WHY rather than
        # falling through to whichever server happens to be first.
        self._unavailable: dict[LanguageServerIdLike, str] = dict(unavailable or {})
        self._file_change_notifier = LanguageServerFileChangeNotifier(project, self)

    @property
    def unavailable_languages(self) -> dict[str, str]:
        """Language key -> why its server is not serving. Empty when everything started."""
        return {ls_id.get_key(): reason for ls_id, reason in self._unavailable.items()}

    @property
    def _default_language_server(self) -> SolidLanguageServer:
        if len(self._language_servers) == 0:
            raise ValueError("No language servers available in the manager")
        return next(iter(self._language_servers.values()))

    @staticmethod
    def from_languages(
        languages: list[LanguageServerIdLike], factory: LanguageServerFactory, project: "Project"
    ) -> "LanguageServerManager":
        """
        Creates a manager with language servers for the given languages using the given factory.
        The language servers are started in parallel threads.

        :param languages: the languages for which to spawn language servers
        :param factory: the factory for language server creation
        :param project: the project for which the language servers are created
        :return: the instance
        """

        class StartLSThread(threading.Thread):
            def __init__(self, ls_id: LanguageServerIdLike):
                super().__init__(target=self._start_language_server, name="StartLS:" + ls_id.get_key())
                self.ls_id = ls_id
                self.language_server: SolidLanguageServer | None = None
                self.exception: Exception | None = None

            def _start_language_server(self) -> None:
                key = self.ls_id.get_key()
                t0 = time.monotonic()
                try:
                    with LogTime(f"Language server startup (ls_id={key})"):
                        _trace("%s: creating", key)
                        self.language_server = factory.create_language_server(self.ls_id)
                        _trace("%s: created in %.1fs, calling start()", key, time.monotonic() - t0)
                        self.language_server.start()
                        if not self.language_server.is_running():
                            raise RuntimeError(f"Failed to start the language server {key}")
                        _trace("%s: START COMPLETE after %.1fs", key, time.monotonic() - t0)
                except Exception as e:
                    # Named, timed and typed. "Error starting language server" on its own does not say
                    # how far it got or how long it spent getting there, which is the first thing anyone
                    # asks when a start goes wrong.
                    log.error(
                        "[startup] %s: FAILED after %.1fs with %s: %s",
                        key,
                        time.monotonic() - t0,
                        type(e).__name__,
                        e,
                        exc_info=e,
                    )
                    self.exception = e

        # start language servers in parallel threads
        t_start = time.monotonic()
        _trace("starting %d language server(s): %s", len(languages), ", ".join(x.get_key() for x in languages))
        threads = []
        for language in languages:
            thread = StartLSThread(language)
            thread.start()
            threads.append(thread)

        # collect language servers and exceptions
        language_servers: dict[LanguageServerIdLike, SolidLanguageServer] = {}
        unavailable: dict[LanguageServerIdLike, str] = {}
        # ONE DEADLINE FOR THE WHOLE SET, not one per server.
        #
        # Upstream joins without a timeout, so a server that never returns from start() blocks the
        # manager's creation for ever - and because the servers share one activation, every OTHER
        # language hangs with it. Observed on this tree 2026-09-20: the C# server walked 238 .csproj
        # under an engine checkout, stalled inside project loading, and C++ queries timed out at 45s on
        # a C# problem, with clangd at 0.0s CPU having never been asked anything.
        #
        # The first version of this patch put the timeout on each join separately, which is the obvious
        # thing to write and is wrong: the joins run in sequence, so N hanging servers cost N x timeout
        # and the bound on activation is a multiple nobody chose. Measured here at five configured
        # languages: a 300s per-join limit meant a 25-minute worst case, and the warning that should
        # have arrived after five minutes had still not appeared after nine. A shared deadline makes
        # the number mean what it says.
        deadline = time.monotonic() + LS_STARTUP_TIMEOUT_SECONDS
        for thread in threads:
            key = thread.ls_id.get_key()
            budget = max(0.0, deadline - time.monotonic())
            _trace("waiting for %s (%.0fs of the shared deadline left)", key, budget)
            thread.join(timeout=budget)
            if thread.is_alive():
                unavailable[thread.ls_id] = f"did not finish starting within {LS_STARTUP_TIMEOUT_SECONDS}s (shared startup deadline)"
                # Best-effort: stop the process the stuck server already spawned. `language_server` is
                # assigned before `start()` is called, so it is available even mid-hang. Done on a
                # daemon thread because stop() talks to the same wedged server and must not be allowed
                # to re-block the startup we just escaped - the whole point of the timeout.
                stuck = thread.language_server
                if stuck is not None:
                    threading.Thread(
                        target=lambda ls=stuck, key=thread.ls_id.get_key(): _stop_quietly(ls, key),
                        name="StopStuckLS:" + thread.ls_id.get_key(),
                        daemon=True,
                    ).start()
            elif thread.exception is not None:
                unavailable[thread.ls_id] = str(thread.exception)
            elif thread.language_server is not None:
                language_servers[thread.ls_id] = thread.language_server

        # DEGRADE PER LANGUAGE rather than failing the whole set.
        #
        # Upstream deliberately fails fast here, reasoning that a user who asked for N languages is
        # better served by a loud failure than by a silent subset. The first half of that is right and
        # is kept: every unavailable language is logged at WARNING, recorded, and a query routed to one
        # RAISES rather than quietly returning nothing. What does not survive contact with a large
        # repository is the second half - refusing to serve C++ because C# could not load a vendored
        # .csproj turns one language's problem into every language's outage.
        #
        # This is the behaviour the Pyright server already had for its own initial-analysis wait
        # ("Timeout waiting for Pyright analysis completion, proceeding anyway"), promoted from one
        # server's local workaround to the rule for all of them.
        for ls_id, reason in unavailable.items():
            log.warning(
                "Language server %s is UNAVAILABLE; that language will not be served: %s. "
                "%d other language server(s) started normally and are unaffected.",
                ls_id.get_key(),
                reason,
                len(language_servers),
            )
        if not language_servers:
            failure_messages = "\n".join([f"{ls_id.get_key()}: {reason}" for ls_id, reason in unavailable.items()])
            raise LanguageServerManagerInitialisationError(
                f"No language server started; all {len(unavailable)} failed:\n{failure_messages}"
            )

        # The line to look for when someone asks "did it start, and what is serving?". It is the only
        # one that states the OUTCOME rather than a step, so a log that ends without it says the start
        # did not finish - which is exactly the question that was unanswerable before this existed.
        _trace(
            "all servers resolved in %.1fs: serving %s%s",
            time.monotonic() - t_start,
            ", ".join(sorted(x.get_key() for x in language_servers)) or "(none)",
            ("; UNAVAILABLE " + ", ".join(sorted(x.get_key() for x in unavailable))) if unavailable else "",
        )
        return LanguageServerManager(language_servers, factory, project, unavailable=unavailable)

    def _ensure_functional_ls(self, ls: SolidLanguageServer) -> SolidLanguageServer:
        if not ls.is_running():
            # A server dying mid-session is the failure that used to be silent: the next call simply
            # took longer and then answered from a fresh server, with nothing saying the old one had
            # gone. Say which language, and say whether the restart worked - a restart that fails here
            # is how a language quietly stops being served for the rest of the session.
            key = ls.ls_id.get_key() if hasattr(ls.ls_id, "get_key") else str(ls.ls_id)
            log.warning("Language server %s HAS DIED (process is no longer running); restarting it now", key)
            t0 = time.monotonic()
            try:
                ls = self.restart_language_server(ls.ls_id)
            except Exception as e:
                log.error(
                    "Language server %s died and could NOT be restarted after %.1fs (%s: %s); "
                    "that language is now unserved for this session",
                    key,
                    time.monotonic() - t0,
                    type(e).__name__,
                    e,
                    exc_info=e,
                )
                self._unavailable[ls.ls_id] = f"died mid-session and the restart failed: {e}"
                raise
            log.warning("Language server %s restarted in %.1fs", key, time.monotonic() - t0)
        return ls

    def _get_suitable_language_server(self, relative_path: str) -> SolidLanguageServer | None:
        """:param relative_path: relative path to a file"""
        for candidate in self._language_servers.values():
            if not candidate.is_ignored_path(relative_path, ignore_unsupported_files=True):
                return candidate
        return None

    def get_language_server(self, relative_path: str) -> SolidLanguageServer:
        """:param relative_path: relative path to a file"""
        ls: SolidLanguageServer | None = None
        if len(self._language_servers) > 1:
            if os.path.isdir(relative_path):
                raise ValueError(f"Expected a file path, but got a directory: {relative_path}")
            ls = self._get_suitable_language_server(relative_path)
        if ls is None:
            # Before falling back to the default server, check whether a server that WOULD have served
            # this file simply did not start. Falling back silently is the dangerous case: a .cs file
            # routed to clangd comes back empty, and an empty result reads as "no such symbol" rather
            # than as "the server for this language is not running".
            for ls_id, reason in self._unavailable.items():
                if _ls_id_claims_path(ls_id, relative_path):
                    raise LanguageServerUnavailableError(ls_id.get_key(), reason)
            ls = self._default_language_server
        return self._ensure_functional_ls(ls)

    def _create_and_start_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        if self._language_server_factory is None:
            raise ValueError(f"No language server factory available to create language server for {ls_id}")
        language_server = self._language_server_factory.create_language_server(ls_id)
        language_server.start()
        self._language_servers[ls_id] = language_server
        return language_server

    def restart_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        """
        Forces recreation and restart of the language server for the given language.
        It is assumed that the language server for the given language is no longer running.

        :param ls_id: the language server identifier
        :return: the newly created language server
        """
        if ls_id not in self._language_servers:
            raise ValueError(f"No language server for language {ls_id.get_key()} present; cannot restart")
        return self._create_and_start_language_server(ls_id)

    def add_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        """
        Dynamically adds a new language server for the given language.

        :param ls_id: the language server to add
        :return: the newly created language server
        """
        if ls_id in self._language_servers:
            raise ValueError(f"Language server {ls_id.get_key()} already present")
        return self._create_and_start_language_server(ls_id)

    def remove_language_server(self, ls_id: LanguageServerIdLike, save_cache: bool = False) -> None:
        """
        Removes the language server for the given language, stopping it if it is running.

        :param ls_id: the language
        """
        if ls_id not in self._language_servers:
            raise ValueError(f"No language server for language {ls_id.get_key()} present; cannot remove")
        ls = self._language_servers.pop(ls_id)
        self._stop_language_server(ls, save_cache=save_cache)

    def get_active_language_server_ids(self) -> list[LanguageServerIdLike]:
        """
        Returns the list of languages for which language servers are currently managed.

        :return: list of languages
        """
        return list(self._language_servers.keys())

    @staticmethod
    def _stop_language_server(ls: SolidLanguageServer, save_cache: bool = False, timeout: float = 2.0) -> None:
        if ls.is_running():
            if save_cache:
                ls.save_cache()
            log.info(f"Stopping language server for language {ls.ls_id} ...")
            ls.stop(shutdown_timeout=timeout)

    def iter_language_servers(self) -> Iterator[SolidLanguageServer]:
        for ls in self._language_servers.values():
            yield self._ensure_functional_ls(ls)

    def stop_all(self, save_cache: bool = False, timeout: float = 2.0) -> None:
        """
        Stops all managed language servers.

        :param save_cache: whether to save the cache before stopping
        :param timeout: timeout for shutdown of each language server
        """
        for ls in self.iter_language_servers():
            self._stop_language_server(ls, save_cache=save_cache, timeout=timeout)

    def save_all_caches(self) -> None:
        """
        Saves the caches of all managed language servers.
        """
        for ls in self.iter_language_servers():
            if ls.is_running():
                ls.save_cache()

    def has_suitable_ls_for_file(self, relative_file_path: str) -> bool:
        return self._get_suitable_language_server(relative_file_path) is not None

    def sync_file_system_changes(self) -> int:
        """
        Polls the file system for changes to source files and notifies the language servers of any changes
        (particularly changes that happened outside of Serena's own file tools, which are not covered by
        the notifications sent by those tools/CodeEditor).

        :return: the number of individual file change events detected (0 if nothing changed).
        """
        log.info("Polling file system for changes to source files ...")
        num_changes = self._file_change_notifier.poll_and_notify()
        log.info(f"File system polling complete; {num_changes} change events sent to language servers.")
        return num_changes


class LanguageServerFileChangeNotifier:
    """
    Detects changes to source files on disk and notifies language servers of those changes.
    """

    def __init__(self, project: "Project", language_server_manager: LanguageServerManager, initial_poll: bool = True) -> None:
        self._project = project
        self._language_server_manager = language_server_manager
        self._freshness_last_seen_mtimes: dict[str, float] | None = None
        self._freshness_lock = threading.Lock()

        if initial_poll:
            # Establish the baseline for the first poll; no notifications are sent on the first call.
            #
            # Traced because this runs AFTER every language server has started, so a start that reaches
            # here has already survived the part everyone suspects - and on a large tree this walk is
            # itself minutes long. Without a line on each side of it, a start stuck here is
            # indistinguishable from a start stuck in a language server.
            _trace("file change notifier: establishing baseline over the project's source files")
            t0 = time.monotonic()
            with LogTime("Initialising file change notifier (polling for baseline)"):
                self.poll_and_notify()
            _trace("file change notifier: baseline established in %.1fs", time.monotonic() - t0)

    # LOCAL PATCH (CodeMem) - see oraios/serena#2077.
    #
    # poll_and_notify runs before symbolic tool calls, and its cost scales with the tracked file
    # count rather than with the number of changes. Measured on the CodeMem tree (97,549 tracked
    # files) with the is_ignored_path hint already applied: 14.5s walk + 9.8s stat = 24.3s PER CALL.
    # Against a 45s tool timeout that spends half the budget before the language server is asked.
    #
    # Top-level directories listed here are not polled. The case for excluding one is that nothing
    # edits it outside Serena - a vendored dependency or, here, an engine the project's own CLAUDE.md
    # makes read-only. On this tree 96,240 of the 97,549 tracked files are under UnrealEngine/, so
    # excluding it costs nothing real and takes the poll to 3.5s.
    #
    # Defaults to EMPTY, i.e. stock behaviour: a project-specific default does not belong in code
    # that is meant to go upstream, and silently skipping a directory the user did not ask to skip
    # would be worse than being slow. CodeMem sets SERENA_FRESHNESS_SKIP=UnrealEngine at the
    # launcher; see CODEMEM-FORK.md. _SLOW_POLL_WARN_S exists so that forgetting to set it is
    # noisy rather than silent.
    _FRESHNESS_SKIP_TOP_LEVEL = frozenset(p.strip() for p in os.environ.get("SERENA_FRESHNESS_SKIP", "").split(",") if p.strip())
    _SLOW_POLL_WARN_S = float(os.environ.get("SERENA_FRESHNESS_SLOW_WARN_S", "5"))

    def _freshness_source_files(self) -> list[str]:
        """The files the freshness poll considers, honouring _FRESHNESS_SKIP_TOP_LEVEL."""
        if not self._FRESHNESS_SKIP_TOP_LEVEL:
            return self._project.gather_source_files()
        root = self._project.project_root
        files: list[str] = []
        for entry in sorted(os.listdir(root)):
            if entry in self._FRESHNESS_SKIP_TOP_LEVEL:
                continue
            abs_entry = os.path.join(root, entry)
            is_file = os.path.isfile(abs_entry)
            if self._project.is_ignored_path(abs_entry, ignore_non_source_files=is_file, is_file=is_file):
                continue
            if is_file:
                files.append(entry)
            else:
                files.extend(self._project.gather_source_files(entry))
        return files

    def poll_and_notify(self) -> int:
        """
        Detects source files that were changed, created or deleted on disk since the last call
        and notifies every language server managed for this project via the LSP
        ``workspace/didChangeWatchedFiles`` notification.

        This exists because Serena's own file and symbol tools notify the language server inline
        (via didOpen/didChange/didClose) when they edit a file, but edits made through any other
        channel (another editor, a second agent, a git checkout, a build step) are otherwise
        invisible to a warm language server, causing symbolic queries to answer from a stale index.

        The set of files considered is exactly the set Serena itself tracks (see
        :meth:`gather_source_files`), so no separate file-discovery logic has to be kept in sync.
        The dominant cost is the directory walk plus one ``os.stat`` per tracked file; this is
        intended to be called before symbolic tool invocations rather than on a timer.

        :return: the number of change events sent (0 if nothing changed, if no language server is
            running yet, or on the first call, which only establishes the baseline).
        """
        poll_started = time.monotonic()
        current: dict[str, float] = {}
        for rel_path in self._freshness_source_files():
            try:
                current[rel_path] = os.stat(os.path.join(self._project.project_root, rel_path)).st_mtime
            except OSError:
                continue
        poll_s = time.monotonic() - poll_started
        if poll_s > self._SLOW_POLL_WARN_S:
            # This runs before symbolic tool calls, so a slow poll is spent out of every one of their
            # timeouts. Warn rather than let it look like the language server being slow.
            log.warning(
                "File system freshness poll took %.1fs for %d tracked files, and runs before symbolic tool calls. "
                "Narrow the project's ignored_paths, or set SERENA_FRESHNESS_SKIP to a comma-separated list of "
                "top-level directories nothing edits outside Serena.",
                poll_s,
                len(current),
            )

        # Read-diff-swap under the lock only; the filesystem walk above and the LSP notifications
        # below stay outside it so concurrent callers do not serialize on I/O.
        with self._freshness_lock:
            previous = self._freshness_last_seen_mtimes
            self._freshness_last_seen_mtimes = current

            if previous is None:
                return 0

            # compute the set of individual events (created, changed, deleted)
            events: list[tuple[str, FileChangeType]] = []
            for rel_path, mtime in current.items():
                prev_mtime = previous.get(rel_path)
                if prev_mtime is None:
                    events.append((rel_path, FileChangeType.Created))
                elif mtime > prev_mtime:
                    events.append((rel_path, FileChangeType.Changed))
            events.extend((rel_path, FileChangeType.Deleted) for rel_path in previous if rel_path not in current)

        if not events:
            return 0

        # create the change didChangeWatchedFiles notification
        changes: list[FileEvent] = [
            {"uri": Path(self._project.project_root, rel_path).resolve().as_uri(), "type": change_type} for rel_path, change_type in events
        ]
        params: DidChangeWatchedFilesParams = {"changes": changes}
        created_paths = [rel_path for rel_path, change_type in events if change_type == FileChangeType.Created]

        for ls in self._language_server_manager.iter_language_servers():
            # send the didChangeWatchedFiles notification to the language server
            try:
                ls.server.notify.did_change_watched_files(params)
            except Exception as e:
                log.error("Failed to notify language server of watched file changes", exc_info=e)

            # A didChangeWatchedFiles(Created) notification alone is not enough for every backend
            # (observed with pyright) to fold a brand-new file into its cross-file reference graph;
            # an open/close cycle forces the parse+bind that Serena's own file tools trigger via
            # SolidLanguageServer.open_file().
            for rel_path in created_paths:
                if ls.is_ignored_path(rel_path, ignore_unsupported_files=True):
                    continue
                try:
                    with ls.open_file(rel_path):
                        pass
                except Exception as e:
                    log.error(f"Failed to refresh newly created file {rel_path!r} in language server", exc_info=e)

        return len(events)
