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

## Patch status against `main`

Checked 2026-09-18 against `bd2712fd`. "Applies" means `patch --dry-run` succeeds; it does **not**
mean the patch is still *correct*, because the surrounding code has moved in every case.

| Patch | Component | Applies to 2.x? | Upstream route |
|---|---|---|---|
| C#: open only non-ignored `.csproj` | **MIT** (`solidlsp`) | **Yes**, offset 1 line | Direct PR — a small bug fix under CONTRIBUTING's scope rules |
| `find_symbol` scope guard | GPL | **No** — both hunks fail | Rewrite by hand, then open an issue first: it changes an existing tool's behaviour |
| `find_symbol_indexed` (new tool) | GPL | n/a — a new class, not a diff | Open an issue first |
| Freshness poll: skip the engine | GPL | **Yes**, offset 7 lines — but `poll_and_notify` was rewritten upstream and now iterates `gather_source_files()`, so it already honours ignore rules | **Re-measure before writing anything.** The remaining cost is one `os.stat` per tracked file before every symbolic call; whether that still hurts is an open question on this tree |

### Why each one exists

- **C# `.csproj`.** `CSharpLanguageServer._open_projects` scans the repository root and opens every
  `.csproj` it finds, without consulting the project's own `ignored_paths`. On the Unreal tree that is
  245 projects, 53 under `Engine/Source/ThirdParty`, which Roslyn cannot build; they emit thousands of
  NuGet advisory lines per restart and end in `The "Csc" task could not be initialized`. The base class
  already exposes `is_ignored_path()`; the patch just applies the one to the other.
- **`find_symbol` scope guard.** `FindSymbolTool` accepts an empty `relative_path`, documented as
  "searches entire codebase", and reaches it through `request_full_symbol_tree`, which walks the
  directory tree and requests document symbols file by file. Scoped to one file that is instant.
  Unscoped on a tree this size it outlives the client timeout by hours and has crashed clangd.
- **`find_symbol_indexed`.** `SolidLanguageServer.request_workspace_symbol()` — a complete
  `workspace/symbol` request that clangd answers from its background index — exists in
  `src/solidlsp/ls.py` and, as of `bd2712fd`, **nothing in `src/serena` calls it**. This adds the
  missing caller. It introduces no capability the language server does not already have.
- **Freshness poll.** `poll_and_notify` runs before every symbolic tool call. Upstream has since
  narrowed what it walks; the original problem was that it walked everything.

## Contributing upstream from here

Upstream requires a CLA (`CLA.md`), accepted once via the CLA assistant bot on your first PR, and it
applies repository-wide including SolidLSP-only changes. Each PR wants a single logical change, a
`CHANGELOG.md` entry in the matching section, an SPDX header on any new file, and `poe format` plus
`poe type-check` clean. Do not open PRs against the REPL — it is a beta feature and they have asked
for issues instead.
