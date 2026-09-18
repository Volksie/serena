# This fork

A fork of [oraios/serena](https://github.com/oraios/serena) carrying performance fixes for very large
monorepos, found while running Serena against an Unreal Engine 5.8 source tree (~107,852 tracked
source files after ignore rules, 187,590 before).

**Licence.** Upstream is multi-licensed: `src/solidlsp` is MIT, the Serena application
(`src/serena`, `src/interprompt`, everything else) is **GPL-3.0-or-later** from v2 onward. This fork
inherits that unchanged. The last MIT-licensed upstream release is `v1.7.0`, tagged `mit-final`
(`74c38a65`); anything derived from `main` is GPL.

Nothing here is a fork *of the project's direction* — every patch is intended to go upstream, and
this branch should shrink toward empty as they land.

## Branch layout

| Branch | Role |
|---|---|
| `main` | A clean mirror of `upstream/main`. **Never commit to it.** Resync, don't merge. |
| `codemem` | Integration branch. Everything not yet upstreamed, plus anything local that never will be. This is what gets installed. |
| `fix/*`, `feat/*` | One per pull request, always cut fresh from `main`, never from `codemem`. |

`upstream`'s push URL is deliberately set to `DISABLED_use_origin` so `git push upstream` cannot
succeed by accident.

Resync `main`:

    git fetch upstream --tags
    git checkout main && git reset --hard upstream/main && git push origin main

Rebase the integration branch onto it:

    git checkout codemem && git rebase main

Install from the fork rather than editing an installed package in place:

    uv tool install --reinstall -p 3.13 git+https://github.com/Volksie/serena@codemem

## REQUIRED: launcher setting

Set this wherever Serena is started for CodeMem (the `Serena MCP (CodeMem)` scheduled task):

    SERENA_FRESHNESS_SKIP=UnrealEngine

Without it the freshness poll walks the engine on **every symbolic tool call**: measured 24.3s
against 3.5s, out of a 45s tool timeout. The code deliberately defaults to stock behaviour rather
than to this value, because a project-specific default does not belong in code meant to go upstream.
Forgetting it is noisy rather than silent - the poll logs a warning above 5s naming the variable.

Optional: `SERENA_FIND_SYMBOL_MAX_SCOPE_FILES` (default 1000, 0 disables the `find_symbol` guard)
and `SERENA_FRESHNESS_SLOW_WARN_S` (default 5).

## What this branch carries

Ported to 2.x and committed on `codemem`. Every one was rewritten rather than rebased, because the
surrounding code moved in all four cases; each commit message records what changed and why.

| Commit | What | Upstream |
|---|---|---|
| `58ded458` | `is_ignored_path` takes an `is_file` hint from the traversal that already knows. Walk **67.8s -> 11.6s**, byte-identical output | **[PR #2078](https://github.com/oraios/serena/pull/2078)** |
| `bee600fe` | `find_symbol` scope guard, moved to `LspApi` so the REPL is covered too, counting by the project's own ignore rules rather than a hardcoded extension list | [#2076](https://github.com/oraios/serena/issues/2076) |
| `446066ec` | `find_symbol_indexed`, the missing caller for `request_workspace_symbol` | [#2075](https://github.com/oraios/serena/issues/2075) |
| `51296ff0` | Freshness-poll skip list plus a slow-poll warning. **24.3s -> 3.5s** per call | [#2077](https://github.com/oraios/serena/issues/2077) |

| `68bf8d7a` | C#: open only non-ignored `.csproj` files. **MIT component** | **[PR #2074](https://github.com/oraios/serena/pull/2074)** |

Everything not yet merged upstream is on this branch, including the two that are already submitted as
pull requests - so installing `codemem` gives the whole set, and each one drops off on the next
rebase after it lands.

## Measurements, for whoever revisits this

All against the CodeMem tree: Unreal Engine 5.8 source plus three game projects, 707,889 files on
disk, 97,549 tracked as source, five language servers. Windows 11, Python 3.13.

| | |
|---|---|
| bare `os.walk` of the whole tree | 10.7s |
| `gather_source_files()` before the hint | 67.8s |
| `gather_source_files()` after the hint | 11.6s |
| freshness poll, full tree | 24.3s per symbolic call |
| freshness poll, engine skipped | 3.5s per symbolic call |
| tracked files outside `UnrealEngine/` | **1,309 of 97,549** |

The last row is why skipping the engine costs nothing: 98.7% of the tracked set is an engine the
project treats as read-only, so polling it detects edits that cannot happen.
