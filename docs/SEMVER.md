# How often does a patch release break public API?

[<- back to README](../README.md)

Semantic versioning makes two promises. A **patch** release changes nothing a caller can
see. A **minor** release only adds.

This is a measurement of both, across 27 real upgrade pairs of widely-pinned packages.
Reproduce it with `blast-radius check <package> <old> <new> --no-behaviour`.

## The result

| Bump | Pairs | Broke exported API | Exported symbols removed or reshaped |
|---|---:|---:|---:|
| **patch** | 15 | **2 (13%)** | 3 |
| **minor** | 9 | **4 (44%)** | 16 |
| major | 3 | 2 (67%) | 47 |

Semver says the patch row should be 0% and the minor row should be 0%.

Both numbers are small enough to name every instance, which is the point - a percentage
with no names behind it is not checkable.

## Every patch-release break, in full

### `urllib3` 2.2.1 → 2.2.2 - a new **required** parameter

The clearest violation found. `BaseHTTPResponse.__init__` gained a keyword-only parameter
with **no default**, inserted between `version` and `reason`:

```
2.2.1  (*, headers=None, status, version,                 reason, decode_content, ...)
2.2.2  (*, headers=None, status, version, version_string, reason, decode_content, ...)
```

Two things break:

- **Any subclass** of `BaseHTTPResponse` that calls `super().__init__(...)` now raises
  `TypeError: missing a required keyword-only argument: 'version_string'`.
- `HTTPResponse` took the same parameter positionally, so any caller passing arguments by
  position has `reason` silently land in `version_string` and every argument after it shift
  by one.

Neither a changelog entry nor a version number tells you this. A type checker would catch
the first if you run one; nothing catches the second.

### `coverage` 7.5.0 → 7.5.4 - a signature change on the data API

`coverage.CoverageData.update` changed shape. One exported symbol, and ten further changes
in internal modules (`coverage.parser`, `coverage.phystokens`, `coverage.regions`) that are
**not** counted above - see the next section for why that distinction matters so much.

### The other 13 patch pairs

`beautifulsoup4`, `charset-normalizer`, `click`, `filelock`, `httpx`, `jinja2`,
`markupsafe`, `platformdirs`, `pytest`, `requests`, `rich`, `tqdm`, `typer` - all clean on
exported API. `beautifulsoup4` and `typer` changed internals only.

## Three corrections, each of which lowered the number

The first version of this sweep reported **38% of patch releases break public API**. Every
correction below moved it down, and the final figure is 13%.

### 1. The same object was counted once per module that imported it

`coverage.CoverageData` is imported into `collector`, `control`, `data`, `html` and
`sqldata`. The probe walked every module, so one signature change to `update` was recorded
as **six** reshaped symbols under six different paths.

Deduplicating by where an object is *defined*, and reporting it under the shortest path it
can be reached by, took `coverage` 7.5.0's surface from 1,409 symbols to **547** - the
original count was inflated 2.6x by aliases alone.

### 2. Moving a class between internal modules read as a breaking change

`coverage` moved `PathAliases` out of `coverage.sqldata`. Keyed on the definition site that
is four removals. Keyed on the name a user writes it is nothing, because
`from coverage import PathAliases` still works and no caller can tell the difference.

### 3. Internal churn counted the same as published breakage

`coverage.parser.join_regex` is a helper in an internal module. It weighed exactly as much
as `coverage.CoverageData.update`, so a release that tidied its internals looked identical
to one that broke its users.

Symbols are now marked `exported` when the author said so - the name is in a module's
`__all__`, or it sits directly in the package's own namespace, which is the import path
people actually write. For `coverage` 7.5.0 → 7.5.4 that is the difference between "23
symbols affected" and **one**.

### And one exclusion, decided before the numbers were looked at

`attrs`, `fsspec`, `packaging` and `certifi` version by date, not by semver. A project that
never promised semantic versioning cannot be shown to have violated it, so they are held
out of the table above.

It matters: `fsspec` 2024.6.0 → 2024.6.1 alone contributed 88 of the 122 symbols in the
original patch bucket. Keeping it would have made calendar versioning look like a semver
violation.

## Full table

Exported-symbol counts are of the *old* version.

| Package | From | To | Bump | Exported symbols | Exported broken | Internal broken |
|---|---|---|---|---:|---:|---:|
| `beautifulsoup4` | 4.12.2 | 4.12.3 | patch | 88 | 0 | 1 |
| `charset-normalizer` | 3.3.0 | 3.3.2 | patch | 15 | 0 | 0 |
| `click` | 8.1.6 | 8.1.7 | patch | 197 | 0 | 0 |
| `coverage` | 7.5.0 | 7.5.4 | patch | 80 | **1** | 10 |
| `filelock` | 3.15.1 | 3.15.4 | patch | 16 | 0 | 0 |
| `httpx` | 0.27.0 | 0.27.2 | patch | 156 | 0 | 0 |
| `jinja2` | 3.1.2 | 3.1.4 | patch | 96 | 0 | 0 |
| `markupsafe` | 2.1.3 | 2.1.5 | patch | 35 | 0 | 0 |
| `platformdirs` | 4.2.0 | 4.2.2 | patch | 47 | 0 | 0 |
| `pytest` | 8.2.0 | 8.2.2 | patch | 13 | 0 | 0 |
| `requests` | 2.32.2 | 2.32.3 | patch | 63 | 0 | 0 |
| `rich` | 13.7.0 | 13.7.1 | patch | 6 | 0 | 0 |
| `tqdm` | 4.66.1 | 4.66.4 | patch | 91 | 0 | 0 |
| `typer` | 0.12.0 | 0.12.3 | patch | 13 | 0 | 2 |
| `urllib3` | 2.2.1 | 2.2.2 | patch | 92 | **2** | 0 |
| `anyio` | 4.1.0 | 4.4.0 | minor | 155 | **1** | 0 |
| `click` | 8.0.4 | 8.1.7 | minor | 200 | **9** | 1 |
| `filelock` | 3.13.1 | 3.15.4 | minor | 9 | **4** | 0 |
| `httpx` | 0.26.0 | 0.27.0 | minor | 156 | 0 | 0 |
| `pluggy` | 1.4.0 | 1.5.0 | minor | 45 | 0 | 0 |
| `rich` | 13.0.1 | 13.7.1 | minor | 6 | 0 | 18 |
| `starlette` | 0.35.1 | 0.37.2 | minor | 2 | 0 | 0 |
| `typer` | 0.9.0 | 0.12.3 | minor | 13 | 0 | 4 |
| `urllib3` | 2.1.0 | 2.2.2 | minor | 90 | **2** | 0 |
| `click` | 7.1.2 | 8.1.7 | major | 177 | **29** | 14 |
| `rich` | 12.6.0 | 13.7.1 | major | 6 | 0 | 20 |
| `urllib3` | 1.26.18 | 2.2.2 | major | 79 | **18** | 71 |

The minor row is the one worth acting on. `click` 8.0 → 8.1 removed `get_os_args`,
`get_terminal_size` and `Group.resultcallback` and reshaped six more exported symbols, all
under a bump that promises additions only.

## What this measures, and what it does not

**API surface only.** This sweep ran with `--no-behaviour`, so it says nothing about the
category the tool exists for: same name, same signature, different answer. That requires
executing both versions and is much slower. The two published comparisons in
[RESULTS.md](RESULTS.md) do include it.

**`rich` reports 6 exported symbols** because it declares almost nothing at the package
root and uses `__all__` sparsely. The exported count is a measure of what a package
*declares*, not of what people import, and packages differ enormously in how much they
declare. Comparing the counts across rows is not meaningful; comparing a package to itself
across versions is.

**27 pairs is a sample, not a census.** These are popular, well-maintained packages, which
if anything biases toward *better* semver discipline than average.

**Four pairs could not be measured**: `pyyaml` 6.0.1 has no wheel for this interpreter and
builds from source, and three `pydantic` pairs failed the same way. Excluded rather than
guessed at.
