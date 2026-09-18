"""
Language server-related tools
"""
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlparse

from serena.symbol import SymbolDictGrouper
from serena.tools import (
    EditingToolWithDiagnostics,
    Tool,
    ToolMarkerSymbolicEdit,
    ToolMarkerSymbolicRead,
)
from serena.tools.file_tools import EditApiMixin
from serena.tools.tools_base import ToolMarkerOptional
from solidlsp.lsp_protocol_handler.lsp_types import SymbolKind

if TYPE_CHECKING:
    from serena.repl.api.lsp_api import LspApi


class LspApiMixin:
    """
    Mixin for tools which delegate to the language server API.
    The API is imported locally, since the API module refers to the tools (as corresponding tools).
    """

    def _api(self) -> "LspApi":
        from serena.repl.api.lsp_api import LspApi

        tool = cast(Tool, cast(object, self))
        return LspApi(tool.agent)


class RestartLanguageServerTool(Tool, ToolMarkerOptional, LspApiMixin):
    """Restarts the language server(s)."""

    def apply(self) -> str:
        """Use this tool only on explicit user request or after confirmation.
        It may be necessary to restart the language server if it hangs.
        """
        return self._api().restart_language_server()


class GetSymbolsOverviewTool(Tool, ToolMarkerSymbolicRead, LspApiMixin):
    """
    Gets an overview of the top-level symbols defined in a given file.
    """

    @property
    def symbol_dict_grouper(self) -> SymbolDictGrouper:
        from serena.repl.api.lsp_api import LspApi

        return LspApi.overview_grouper_

    def apply(self, relative_path: str, depth: int = -1, max_answer_chars: int = -1) -> str:
        """
        Use this tool to get a high-level understanding of the code symbols in a file.
        This should be the first tool to call when you want to understand a new file, unless you already know
        what you are looking for.

        :param relative_path: the relative path to the file to get the overview of
        :param depth: depth up to which descendants shall be retrieved.
            Default (-1) results in a language specific choice: 1 for java and kotlin and 0 for other languages
        :param max_answer_chars: if the overview is longer than this number of characters,
            no content will be returned. -1 means the default value from the config will be used.
            Don't adjust unless there is really no other way to get the content required for the task.
        :return: a JSON object containing symbols grouped by kind in a compact format.
        """
        return self._api().get_symbols_overview(relative_path, depth=depth, max_answer_chars=max_answer_chars).represent()


class FindSymbolTool(Tool, ToolMarkerSymbolicRead, LspApiMixin):
    """
    Performs a global (or local) search using the language server backend.
    """

    @property
    def symbol_dict_grouper(self) -> SymbolDictGrouper:
        from serena.repl.api.lsp_api import LspApi

        return LspApi.find_symbol_dict_grouper_

    def apply(
        self,
        name_path_pattern: str,
        depth: int = 0,
        relative_path: str = "",
        include_body: bool = False,
        include_info: bool = False,
        include_kinds: list[int] = [],  # noqa: B006
        exclude_kinds: list[int] = [],  # noqa: B006
        substring_matching: bool = False,
        max_matches: int = -1,
        max_answer_chars: int = -1,
    ) -> str:
        """
        Finds symbols and code entities (classes, methods, etc.) based on the given name path pattern.
        The returned symbol information can be used for edits or further queries.
        Specify `depth > 0` to also retrieve children/descendants (e.g., methods of a class).

        A name path is a path in the symbol tree *within a source file*.
        For example, the method `my_method` defined in class `MyClass` would have the name path `MyClass/my_method`.
        If a symbol is overloaded (e.g., in Java), a 0-based index is appended (e.g. "MyClass/my_method[0]") to
        uniquely identify it.

        To search for a symbol, you provide a name path pattern that is used to match against name paths.
        It can be
         * a simple name (e.g. "method"), which will match any symbol with that name
         * a relative path like "class/method", which will match any symbol with that name path suffix
         * an absolute name path "/class/method" (absolute name path), which requires an exact match of the full name path within the source file.
        Append an index `[i]` to match a specific overload only, e.g. "MyClass/my_method[1]".

        :param name_path_pattern: the name path matching pattern (see above)
        :param depth: depth up to which descendants shall be retrieved (e.g. use 1 to also retrieve immediate children;
            for the case where the symbol is a class, this will return its methods).
            Ignored if `include_body=True`. Default 0.
        :param relative_path: (optional) restrict search to this file or directory. If None, searches entire codebase.
            If a directory is passed, the search will be restricted to the files in that directory.
            If a file is passed, the search will be restricted to that file.
            If you have some knowledge about the codebase, you should use this parameter, as it will significantly
            speed up the search as well as reduce the number of results.
        :param include_body: If True, include the symbol's source code. Use judiciously.
        :param include_info: whether to include additional info (hover-like, typically including docstring and signature),
            about the symbol (ignored if include_body is True). Info is never included for child symbols.
            Note: Depending on the language, this can be slow (e.g., C/C++).
        :param include_kinds: (optional) limits results to the given LSP symbol kinds (integers)
        :param exclude_kinds: (optional) list of LSP symbol kinds (integers) to exclude.
        :param substring_matching: If True, use substring matching for the last segment of `name_path_pattern`
            (i.e. the name of the symbol, e.g. "foo" in "Class/foo" or "my_method" in "my_method").
        :param max_matches: Maximum number of permitted matches. If exceeded, a shortened result is returned
             which allows refining the search. -1 (default) means no limit. Set to 1 to search for a unique symbol.
        :param max_answer_chars: max result length; -1 for default
        :return: symbols (with locations) matching the name.
        """
        return (
            self._api()
            .find_symbol(
                name_path_pattern,
                depth=depth,
                relative_path=relative_path,
                include_body=include_body,
                include_info=include_info,
                include_kinds=include_kinds,
                exclude_kinds=exclude_kinds,
                substring_matching=substring_matching,
                max_matches=max_matches,
                max_answer_chars=max_answer_chars,
            )
            .represent()
        )

    @classmethod
    def get_param_aliases(cls) -> dict[str, str]:
        return {"name_path": "name_path_pattern"}


# LOCAL PATCH (CodeMem) - proposed upstream as oraios/serena#2075.
#
# SolidLanguageServer.request_workspace_symbol() is a complete `workspace/symbol` implementation and
# nothing under src/serena calls it. Every symbol lookup instead goes through request_full_symbol_tree,
# which walks the directory tree and asks for document symbols file by file - so Serena redoes work the
# language server has already done. clangd, rust-analyzer, gopls and jdtls all maintain a background
# index exactly so that `workspace/symbol` can answer "where is X" without touching the filesystem.
#
# This adds the missing caller. It introduces no capability the language server does not already have.
class FindSymbolIndexedTool(Tool, ToolMarkerSymbolicRead):
    """
    Finds symbols by name across the whole workspace from the language server's own index
    (LSP workspace/symbol). Use this to locate a class, function or type when you do not know which
    file it is in: it answers from the index instead of walking every file, so it stays fast on very
    large trees. Returns each match's name, kind, container and file:line; follow up with find_symbol
    and relative_path set to that file to read the body.
    """

    def apply(
        self,
        query: str,
        language: str = "",
        max_matches: int = 50,
        max_answer_chars: int = -1,
    ) -> str:
        """
        Looks symbols up by name in the language server's workspace index.

        :param query: the symbol name, or a fragment of it. Matching is the language server's own;
            clangd matches fuzzily, so a partial name works.
        :param language: which language server to ask, by its language id (e.g. "cpp", "csharp",
            "python"). Empty (the default) asks every running language server.
        :param max_matches: maximum number of matches to return; -1 for no limit.
        :param max_answer_chars: if the output is longer than this many characters it is shortened;
            -1 uses the configured default.
        :return: a JSON list of matches, each with name, kind, container, relative_path and line.
        """
        ls_manager = self.project.get_language_server_manager_or_raise()
        servers = [ls for ls in ls_manager.iter_language_servers() if not language or ls.language_id == language]
        if not servers:
            # An empty list would read as "no such symbol", which is a different and wrong answer.
            return self._to_json(
                {"error": f"no {language!r} language server is running for this project" if language else "no language server is running"}
            )

        matches: list[dict[str, Any]] = []
        unsupported: list[str] = []
        for ls in servers:
            symbols = ls.request_workspace_symbol(query)
            if symbols is None:
                # workspace/symbol is optional in LSP. Saying so beats implying the symbol is absent.
                unsupported.append(ls.language_id)
                continue
            for sym in symbols:
                location = sym.get("location") or {}
                uri = location.get("uri", "")
                path = unquote(urlparse(uri).path) if uri else ""
                if len(path) > 2 and path[0] == "/" and path[2] == ":":  # file:///C:/... on Windows
                    path = path[1:]
                try:
                    relative = os.path.relpath(path, ls.repository_root_path) if path else ""
                except ValueError:  # a different drive on Windows
                    relative = path
                start = (location.get("range") or {}).get("start") or {}
                try:
                    kind_name = SymbolKind(sym.get("kind")).name
                except ValueError:
                    kind_name = str(sym.get("kind"))
                matches.append(
                    {
                        "name": sym.get("name"),
                        "kind": kind_name,
                        "container": sym.get("containerName") or "",
                        "relative_path": relative.replace("\\", "/"),
                        "line": (start.get("line", -1) + 1) if start else None,
                    }
                )
                if 0 <= max_matches <= len(matches):
                    break
            if 0 <= max_matches <= len(matches):
                break

        if not matches and unsupported:
            return self._to_json({"error": f"no index available: {', '.join(unsupported)} does not implement workspace/symbol"})
        if not matches:
            # An empty list here reads as "no such symbol", and on a freshly started server that is
            # simply false. Measured on clangd 22.1.3, 2026-09-18: a `workspace/symbol` sent before
            # any file has been opened returns 0 results in 0.00s, because clangd does not load its
            # compilation database until the first didOpen - its own log prints "Loaded compilation
            # database" and "Enqueueing 25887 commands for indexing" only after that point. Opening
            # one file and asking again returned 14 results within 15s.
            #
            # The warm-up is deliberately NOT done here: it makes the language server index the whole
            # project, which is a large side effect for a lookup, and the caller may not want it yet.
            return self._to_json(
                {
                    "matches": [],
                    "caveat": (
                        "No matches, but this may not mean the symbol is absent: a language server "
                        "that has not yet opened a file has no compilation database loaded and its "
                        "index is empty, so it answers instantly with nothing. Run any scoped query "
                        "first (find_symbol with relative_path set to a source file), then retry this."
                    ),
                }
            )
        return self._limit_length(self._to_json(matches), max_answer_chars)


class FindReferencingSymbolsTool(Tool, ToolMarkerSymbolicRead, LspApiMixin):
    """
    Finds symbols that reference the given symbol
    """

    @property
    def symbol_dict_grouper(self) -> SymbolDictGrouper:
        from serena.repl.api.lsp_api import LspApi

        return LspApi.references_grouper_

    def apply(
        self,
        name_path: str,
        relative_path: str,
        include_kinds: list[int] = [],  # noqa: B006
        exclude_kinds: list[int] = [],  # noqa: B006
        max_answer_chars: int = -1,
    ) -> str:
        """
        Finds references to the symbol at the given `name_path`. The result will contain metadata about the referencing symbols
        as well as a short code snippet around the reference.

        :param name_path: name path of the symbol
        :param relative_path: the relative path to the file containing the symbol for which to find references.
        :param include_kinds: (optional) limits results to the given LSP symbol kinds (integers)
        :param exclude_kinds: optional list of LSP symbol kinds (integers) to exclude.
        :param max_answer_chars: max result length; -1 for default
        :return: a list of JSON objects with the symbols referencing the requested symbol
        """
        return (
            self._api()
            .find_referencing_symbols(
                name_path, relative_path, include_kinds=include_kinds, exclude_kinds=exclude_kinds, max_answer_chars=max_answer_chars
            )
            .represent()
        )


class FindImplementationsTool(Tool, ToolMarkerSymbolicRead, LspApiMixin):
    """
    Finds the implementations of a symbol
    """

    def apply(
        self,
        name_path: str,
        relative_path: str,
        include_info: bool = False,
        include_kinds: list[int] = [],  # noqa: B006
        exclude_kinds: list[int] = [],  # noqa: B006
        max_answer_chars: int = -1,
    ) -> str:
        """
        Finds implementations of the symbol at the given `name_path`.

        :param name_path: the symbol's name path
        :param relative_path: the relative path to the file containing the symbol for which to find implementations.
            Note that here you can't pass a directory but must pass a file.
        :param include_info: whether to include additional info (hover-like, typically including docstring and signature),
            about the implementing symbols.
        :param include_kinds: (optional) limits results to the given LSP symbol kinds (integers)
        :param exclude_kinds: (optional) list of LSP symbol kinds (integers) to exclude.
        :param max_answer_chars: max result length; -1 for default
        :return: a list of JSON objects with the symbols implementing the requested symbol
        """
        return (
            self._api()
            .find_implementations(
                name_path,
                relative_path,
                include_info=include_info,
                include_kinds=include_kinds,
                exclude_kinds=exclude_kinds,
                max_answer_chars=max_answer_chars,
            )
            .represent()
        )


class FindDeclarationTool(Tool, ToolMarkerSymbolicRead, LspApiMixin):
    """
    Finds the declaration/definition of a symbol
    """

    def apply(
        self,
        relative_path: str,
        regex: str,
        containing_symbol_name_path: str | None = None,
        include_body: bool = False,
        include_info: bool = False,
    ) -> str:
        r"""
        Finds the declaration of a symbol.

        :param relative_path: the relative path to the source file containing the symbol for which to find the declaration.
        :param regex: a regular expression with one group, where the group matches the symbol for which to perform the lookup.
            For example, to find the declaration of the `process` method in a call like `obj.process()`,
            pass an expression like "obj\.(process)\(process_input_arg=37\)".
            Prefer regexes with sufficiently large context around the group to render the match unambiguous.
            Uses Python syntax with MULTILINE and DOTALL flags enabled.
        :param containing_symbol_name_path: optional name path of a containing symbol whose body shall be searched instead of the full file.
        :param include_body: whether to include the symbol's body in the result. Default False.
        :param include_info: whether to include additional info (hover-like). Default False.
        """
        relative_path = self._sanitize_input_param(relative_path)
        regex = self._sanitize_input_param(regex)
        return (
            self._api()
            .find_declaration(
                relative_path,
                regex,
                containing_symbol_name_path=containing_symbol_name_path,
                include_body=include_body,
                include_info=include_info,
            )
            .represent()
        )


class GetDiagnosticsForFileTool(Tool, ToolMarkerSymbolicRead, LspApiMixin):
    """
    Gets diagnostics for a file, grouped by symbol.
    """

    def apply(
        self,
        relative_path: str,
        start_line: int = 0,
        end_line: int = -1,
        min_severity: int = 4,
        max_answer_chars: int = -1,
    ) -> str:
        """
        Gets diagnostics for a file. Diagnostics are grouped as `relative_path -> severity -> name_path -> diagnostics_results`.
        If a diagnostic cannot be mapped to a symbol, it is grouped under the special name path `<file>`.

        :param relative_path: the relative path to the file to inspect.
        :param start_line: the first 0-based line to include. Defaults to 0.
        :param end_line: the last 0-based line to include. Defaults to -1, which means until the end of the file.
        :param min_severity: minimum LSP severity to include, where 1=Error, 2=Warning, 3=Information, 4=Hint.
            Diagnostics with lower-or-equal numeric severity are returned.
        :param max_answer_chars: max result length; -1 for default
        :return: grouped diagnostics for the requested file.
        """
        return (
            self._api()
            .get_diagnostics_for_file(
                relative_path, start_line=start_line, end_line=end_line, min_severity=min_severity, max_answer_chars=max_answer_chars
            )
            .represent()
        )


class GetDiagnosticsForSymbolTool(Tool, ToolMarkerSymbolicRead, ToolMarkerOptional, LspApiMixin):
    """
    Gets diagnostics for a symbol and, optionally, for symbols that reference it.
    """

    def apply(
        self,
        name_path: str,
        reference_file: str = "",
        check_symbol_references: bool = False,
        min_severity: int = 4,
        max_answer_chars: int = -1,
    ) -> str:
        """
        Gets diagnostics for the specified symbol. When `check_symbol_references` is true, diagnostics for all
        referencing symbols are also included. The result is grouped as
        `relative_path -> severity -> name_path -> diagnostics_results`.

        :param name_path: the name path of the symbol to inspect.
        :param reference_file: optional file path used to disambiguate the symbol search.
        :param check_symbol_references: whether to additionally collect diagnostics for symbols that reference the symbol.
        :param min_severity: minimum LSP severity to include, where 1=Error, 2=Warning, 3=Information, 4=Hint.
            Diagnostics with lower-or-equal numeric severity are returned.
        :param max_answer_chars: max result length; -1 for default
        :return: grouped diagnostics for the requested symbol and, optionally, its referencing symbols.
        """
        return (
            self._api()
            .get_diagnostics_for_symbol(
                name_path,
                reference_file=reference_file,
                check_symbol_references=check_symbol_references,
                min_severity=min_severity,
                max_answer_chars=max_answer_chars,
            )
            .represent()
        )


class ReplaceSymbolBodyTool(EditingToolWithDiagnostics, EditApiMixin):
    """
    Replaces the full definition of a symbol using the language server backend.
    """

    def apply(
        self,
        name_path: str,
        relative_path: str,
        body: str,
    ) -> str:
        r"""
        Replaces the body of the given symbol.

        IMPORTANT: Only replace symbol bodies if you have previously made a retrieval with include_body=True and thus know what
        constitutes the body!

        :param name_path: name path of the symbol whose body to replace
        :param relative_path: the relative path to the file containing the symbol
        :param body: the new symbol body. The symbol body is the definition of a symbol
            in the programming language, including e.g. the signature line for functions.
            Depending on the language, it may or may not include a preceding docstring or other preceding annotations.
        """
        with self.diagnostics_context(relative_path) as diagnostics_context:
            result = self._api().replace_symbol_body(name_path, relative_path, body)
            return diagnostics_context.format_result(result)


class InsertAfterSymbolTool(EditingToolWithDiagnostics, EditApiMixin):
    """
    Inserts content after the end of the definition of a given symbol.
    """

    def apply(
        self,
        name_path: str,
        relative_path: str,
        body: str,
    ) -> str:
        """
        Use this to insert code after a class/method/function definition.
        Don't use to insert after assignments (constants, fields).

        :param name_path: name path of the symbol after which to insert content
        :param relative_path: the relative path to the file containing the symbol
        :param body: the body/content to be inserted. The inserted code shall begin with the next line after
            the symbol.
        """
        with self.diagnostics_context(relative_path) as diagnostics_context:
            result = self._api().insert_after_symbol(name_path, relative_path, body)
            return diagnostics_context.format_result(result)


class InsertBeforeSymbolTool(EditingToolWithDiagnostics, EditApiMixin):
    """
    Inserts content before the beginning of the definition of a given symbol.
    """

    def apply(
        self,
        name_path: str,
        relative_path: str,
        body: str,
    ) -> str:
        """
        Inserts the given content before the beginning of the definition of the given symbol (via the symbol's location).
        A typical use case is to insert a new class, function, method, field or variable assignment; or
        a new import statement before the first symbol in the file.

        :param name_path: name path of the symbol before which to insert content
        :param relative_path: the relative path to the file containing the symbol
        :param body: the body/content to be inserted before the line in which the referenced symbol is defined
        """
        with self.diagnostics_context(relative_path) as diagnostics_context:
            result = self._api().insert_before_symbol(name_path, relative_path, body)
            return diagnostics_context.format_result(result)


class RenameSymbolTool(Tool, ToolMarkerSymbolicEdit, LspApiMixin):
    """
    Renames a symbol throughout the codebase using language server refactoring capabilities.
    For JB, we use a separate tool.
    """

    def apply(
        self,
        name_path: str,
        relative_path: str,
        new_name: str,
    ) -> str:
        """
        Renames the symbol with the given `name_path` to `new_name` throughout the entire codebase.
        Note: for languages with method overloading, like Java, name_path may have to include a method's
        signature to uniquely identify a method.

        :param name_path: name path of the symbol to rename
        :param relative_path: the relative path to the file containing the symbol to rename
        :param new_name: the new name for the symbol
        :return: result summary indicating success or failure
        """
        return self._api().rename_symbol(name_path, relative_path, new_name)


class SafeDeleteSymbol(Tool, ToolMarkerSymbolicEdit, LspApiMixin):
    def apply(
        self,
        name_path_pattern: str,
        relative_path: str,
    ) -> str:
        """
        Deletes the symbol if it is safe to do so (i.e., if there are no references to it)
        or returns a list of references to it.

        :param name_path_pattern: name path of the symbol to delete
        :param relative_path: the relative path to the file containing the symbol to delete
        """
        return self._api().safe_delete_symbol(name_path_pattern, relative_path)
