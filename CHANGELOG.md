# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-09-24

First release.

### Added

- `blast-radius check <package> <old> <new>` — installs both versions into separate
  directories, compares their public API, then **executes** every function that kept
  both name and call shape on identical inputs. Reports three kinds, ordered by how
  likely each is to reach production unnoticed: `gone` (an ImportError tells you),
  `reshaped` (a type checker might), and `SILENT` — same name, same signature,
  different answer, which nothing tells you.
- `--used-by <repo>` — matches every change against your own source, with file and line,
  so an upgrade removing forty functions nobody calls is not ranked beside one that
  touches a function you call in a loop.
- `docs/SEMVER.md` — 27 real upgrade pairs measured: **13% of patch releases and 44% of
  minor releases removed or reshaped exported API**. Every instance named.
- `demo.py` — the `urllib3 2.2.1 -> 2.2.2` case in one command.

### Fixed

- Aliased symbols were counted once per importing module, so one signature change to
  `coverage.CoverageData.update` was reported as six. Surfaces are now keyed on the
  shortest public path; `coverage` 7.5.0 went from 1,409 symbols to 547.
- A class moved between internal modules read as a removal plus an addition, although
  the public import path still resolved and no caller could tell.
- Internal churn weighed the same as published API. Symbols now carry an `exported`
  flag, set from `__all__` or the package root namespace.
- A failed install crashed the run on Windows. `subprocess.run(text=True)` decodes with
  the locale codec, and `uv` draws its errors with box characters cp1252 cannot
  represent; the resulting `UnicodeDecodeError` is not an `OSError` and was not caught.

[0.1.0]: https://github.com/hammasbuilds/blast-radius/releases/tag/v0.1.0
