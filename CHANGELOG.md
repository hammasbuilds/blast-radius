# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-09-24

First release.

### Added

- `--json`: the blast-radius.json object on stdout, the human report on stderr; stdout
  stays empty when the run cannot finish (exit 2).
- A one-line reason before the installer's text when an install fails: a version that
  does not exist, a package name that is not on the index, or a timeout.
- `blast-radius check <package> <old> <new>` — installs both versions into separate
  directories, compares their public API, then **executes** every function that kept
  both name and call shape on identical inputs. Reports three kinds, ordered by how
  likely each is to reach production unnoticed: `gone` (an ImportError tells you),
  `reshaped` (a type checker might), and `SILENT` — same name, same signature,
  different answer, which nothing tells you.
- `--used-by <repo>` — matches every change against your own source, with file and line,
  so an upgrade removing forty functions nobody calls is not ranked beside one that
  touches a function you call in a loop.
- `docs/SEMVER.md` — 27 real upgrade pairs measured: **13% of patch releases and 22% of
  minor releases removed or reshaped exported API**. Every instance named.
- `demo.py` — the `urllib3 2.2.1 -> 2.2.2` case in one command.
- `widened` — a signature change every existing call survives (a new parameter with a
  default, a positional-only parameter that may now be passed by name). Listed, never
  counted as breaking, and still executed by the behaviour pass.
- `--fail-on gone|reshaped|silent|any|used` for CI (comma-separated or repeated);
  `--fail-on-silent` kept. Exit codes: 0 pass, 1 gate tripped, 2 could not finish.
- `--import-name`, and the import name is read from the installed dist-info, so
  `beautifulsoup4` probes `bs4`.
- `--version`, `--install-timeout`, help text with units for every option.
- `--used-by` accepts a single `.py` file, and reports method calls on objects of unknown
  type as *possible* call sites.
- `REPORT.md` lists every gone, reshaped, widened and added symbol by name.

### Fixed

- `--used-by` with a path that does not exist exited 0 with "none of these were matched
  to your code". It is now an error (exit 2), checked before anything is installed.
- Call-site matching counted `venv/`, `env/`, `site-packages`, `.tox` and build output as
  your code, and silently skipped files that were not UTF-8. Virtualenvs are detected by
  `pyvenv.cfg`, files are decoded per PEP 263, and unreadable files are counted and named.
- `--no-behaviour` printed "SILENT 0, nothing catches these" and "nothing behaved
  differently" although nothing had run.
- A failed install showed a truncated uv error followed by an unrelated pip one. The full
  uv error is shown and pip is only used when uv is absent; uv is pinned to the running
  interpreter with `--python`.
- Long witnesses and signatures were cut at a fixed width, often showing two identical
  prefixes; they are now cut around the first difference.

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
