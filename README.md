<h1 align="center">blast-radius (Python · inspect · differential testing · AST)</h1>
<p align="center"><i>What a dependency upgrade actually changes — including what nothing warns you about</i></p>

<p align="center">
  <a href="https://github.com/hammasbuilds/blast-radius#what-it-does">What it does</a> &middot;
  <a href="https://github.com/hammasbuilds/blast-radius#results">Results</a> &middot;
  <a href="https://github.com/hammasbuilds/blast-radius/blob/main/docs/SEMVER.md">The semver sweep</a> &middot;
  <a href="https://github.com/hammasbuilds/blast-radius/blob/main/docs/RESULTS.md">Full results</a> &middot;
  <a href="https://github.com/hammasbuilds/blast-radius#how-it-works">How it works</a> &middot;
  <a href="https://github.com/hammasbuilds/blast-radius#run-it">Run it</a> &middot;
  <a href="https://github.com/hammasbuilds/blast-radius#scope">Scope</a> 
</p>

<p align="center">
  <a href="https://github.com/hammasbuilds/blast-radius/actions/workflows/ci.yml"><img src="https://github.com/hammasbuilds/blast-radius/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/runtime%20deps-0-brightgreen" alt="zero dependencies">
  <img src="https://img.shields.io/badge/model-none%20required-success" alt="no model">
  <img src="https://img.shields.io/badge/tests-274-brightgreen" alt="tests">
  <img src="https://img.shields.io/badge/upgrade%20pairs%20measured-27-blue" alt="pairs">
  <a href="https://github.com/hammasbuilds/blast-radius/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="license"></a>
</p>

---

## What it does

```text
 install BOTH versions ──> read both public surfaces ──┬──> GONE      an ImportError tells you
                                                       ├──> RESHAPED  a type checker tells you
                                                       ├──> WIDENED   nothing to tell: old calls still work
                                                       └──> same shape? ──> EXECUTE both on the same inputs
                                                                                   └──> SILENT  nothing tells you
```

A dependency bump arrives as a version number and a changelog. Neither is evidence. This
sorts what actually changed by **how likely it is to reach production unnoticed**:

| | who tells you |
|---|---|
| a name that vanished (`gone`) | an `ImportError`, at startup, on any CI run |
| a signature that changed incompatibly (`reshaped`) | a type checker, if you run one against a typed dependency |
| a signature that only grew (`widened`) | nobody needs to: every existing call still works |
| **same name, same signature, different answer (`SILENT`)** | **nothing** |

> **A changelog is a claim. This is the diff.**

## Results

### 22 upgrade pairs: how often does a patch release break public API?

**API surface only.** This sweep runs `--no-behaviour`, so it sees names that vanished and
signatures that changed, and **cannot detect a single `SILENT` change** — the category the
opening of this README argues nothing else reports. The evidence for `SILENT` is the two
packages in [`RESULTS.md`](docs/RESULTS.md), not this table.

Semantic versioning says a patch release changes nothing a caller can see, and a minor
release only adds. Measured across widely-pinned packages:

| Bump | Pairs | Broke exported API | Broken symbols | Of exported symbols |
|---|---:|---:|---:|---:|
| **patch** | 13 | **2 of 13 (15%)** | 3 | 3 of 1,136 (0.26%) |
| **minor** | 6 | **2 of 6 (33%)** | 7 | 7 of 505 (1.4%) |
| major | 3 | **2 of 3** | 29 | 29 of 262 (11%) |

Five of the 27 pairs are **0.x** releases, where [SemVer
§4](https://semver.org/#spec-item-4) promises nothing at all — for a 0.x package the second
component *is* the breaking position — so they are excluded, on the same grounds this study
already excludes calendar-versioned packages. All five were clean, so excluding them
**raises** both headlines. **All six breaks come from two packages**: `click` three and
`urllib3` three, with urllib3's `HTTPResponse` reshape falling in both the patch and the
minor row. The major row is 2 of 3 and gets no percentage.

Small enough to name every instance, which is the point — a percentage with no names behind
it is not checkable. Full method, the three rows' dependence, the per-symbol view and four
earlier corrections: [`docs/SEMVER.md`](docs/SEMVER.md).

**`urllib3` 2.2.1 → 2.2.2**, a patch release, added a **required** keyword-only parameter to
`BaseHTTPResponse.__init__`, inserted between `version` and `reason`:

```
2.2.1  (*, headers=None, status, version,                 reason, decode_content, ...)
2.2.2  (*, headers=None, status, version, version_string, reason, decode_content, ...)
```

Every subclass calling `super().__init__(...)` now raises `TypeError`. `HTTPResponse` takes
the same parameter positionally, so positional callers have `reason` land silently in
`version_string`.

The first version of this sweep said **38%** for patch releases. Three bugs in the probe
were inflating it — aliased symbols counted once per importing module, internal module moves
read as removals, and internal churn weighed the same as published API. A fourth took the
minor row from **44% to 22%**: an added *optional* parameter was being counted as a break.
It cannot break a caller, and is now reported as `widened`. Every correction moved a number
down.

&#128202; **[The full sweep, every break named, and the three corrections &rarr;](https://github.com/hammasbuilds/blast-radius/blob/main/docs/SEMVER.md)**

### One upgrade in depth

`packaging` 21.3 → 24.0, with call sites matched against
[`pypa/build`](https://github.com/pypa/build) at commit `8d1dd6b` (line numbers below
are from that commit):

```
git clone https://github.com/pypa/build pypa-build
git -C pypa-build checkout 8d1dd6b
blast-radius check packaging 21.3 24.0 --used-by pypa-build

  gone          2   an import error at startup
  reshaped      0   a type checker would catch these
  SILENT        4   nothing catches these
  widened       3   not breaking: existing calls still work
  added         9
```

The one worth the whole tool:

```
packaging.version.parse
  same signature (version: str) -> Union[ForwardRef('LegacyVersion'), ForwardRef('Version')]; 3 of 4 exercised inputs disagree
  input : ("x",)
     21.3 : ok: <LegacyVersion('x')>
     24.0 : raise: InvalidVersion: Invalid version: 'x'
```

(Output from Python 3.12. Annotations are printed the way your Python prints them, so
3.14 shows that return type as `ForwardRef('LegacyVersion') | ForwardRef('Version')`; the
counts and verdicts do not change.)

`parse("x")` returned a value and now raises. Same name, same signature. No import fails and
no type checker complains — it takes running both versions to find.

And one that pypa/build is exposed to. `SpecifierSet.contains` only *gained* an optional
parameter, so it is `widened`, not breaking — but because old calls still bind, the
behaviour pass ran it, and it changed underneath:

```
packaging.specifiers.SpecifierSet.contains
  old calls still bind (signature widened from (self, item: Union[packaging.version.Version, packaging.version.LegacyVersion, str], prereleases: Optional[bool] = None) -> bool); 5 of 6 exercised inputs disagree
  input : ("x", True,)
     21.3 : ok: True
     24.0 : raise: InvalidVersion: Invalid version: 'x'
  you may call it at: src/build/_util.py:66
```

pypa/build calls it as `req.specifier.contains(dist.version, prereleases=True)`. The
receiver's type only exists at runtime, so that line is reported as a **possible** call
site, ranked below the proven ones and never enough on its own to fail `--fail-on used`.
The proven ones are `packaging.utils.canonicalize_name` (widened, 3 sites, e.g.
`src/build/env.py:38`) and `packaging.metadata.parse_email` (added,
`src/build/__main__.py:485`) — neither of which can break it.

An upgrade removing forty functions nobody calls is a non-event. The same upgrade touching
one you call in a loop is an incident — so the report sorts by that before anything else.

### What the behaviour pass reaches on a real library

`click` 8.1.6 → 8.1.7 (`blast-radius check click 8.1.6 8.1.7`, a few seconds):

| of 234 stable callables | |
|---|---:|
| not run: the name says it acts on the machine (`launch`, `edit`, `prompt`, `getchar`, `confirm`, `pager`, ...) | 21 |
| **exercised in both versions** | **111** |
| ran, and rejected every generated input | 56 |
| needs an instance this tool could not build | 42 |
| never handed an argument of the right type | 4 |

No API change and no silent change on the inputs tried. The 102 not exercised are reported
by reason rather than as one "could not be called" number: a function this tool never
managed to hand a valid argument is a fact about the tool, and one that ran and refused is a
fact about the package.

An earlier run reported one silent change here, `BashComplete.source`: on a machine with no
`bash`, 8.1.6 raises *Couldn't detect Bash version* and 8.1.7 warns and returns the
completion script. That difference is real - call it directly and it shows - but the
behaviour pass now refuses to let a probed function start a process, so both versions fail
at the same `subprocess.run` and it is not reported. That is the price of the guard
described under [Scope](https://github.com/hammasbuilds/blast-radius#scope).

The first runs exercised none of it. Four fixes, none of them about click, made the pass work at all:

| | |
|---|---|
| the payload went on the **command line** | 12,290 characters returned nothing with exit code 0; on Windows the behaviour pass silently did nothing on any package worth checking |
| `SystemExit` was not caught | it inherits `BaseException`, so `except Exception` missed it, and one click command calling `sys.exit()` ended the whole run |
| `_params` mis-parsed a **return annotation** | `(value: int, name: str = "x") -> bool` was read as taking ONE parameter, so every annotated function was called short and raised `TypeError` |
| a class needing a constructor argument was **refused** | now built with a generated argument where the annotation allows a guess |

See [docs/RESULTS.md](https://github.com/hammasbuilds/blast-radius/blob/main/docs/RESULTS.md) for all three runs; the
JSON each number is read from is committed next to it.

## How it works

**Both versions, side by side, in separate processes.** Two versions of one package cannot
coexist in an interpreter — `sys.modules` is keyed by name, so the second import wins and
every later comparison is one version against itself. Each is probed in its own subprocess,
and **the probe proves which file it loaded** before reporting anything.

**Only the package's own names.** `dir(module)` returns everything reachable, third-party
imports included. Symbols are filtered by `__module__`.

**Call shape, not signature text.** Two signatures are "the same" when their parameter
names, kinds and defaults match. Annotations are excluded on purpose: adding a type hint
cannot break a caller.

**Compatible, not merely different.** A changed call shape is `reshaped` only if some
existing call could trip on it: a parameter removed, renamed, reordered, a default taken
away, a new required keyword. A new parameter with a default, or a positional-only one that
may now be passed by name, is `widened` — listed, never counted as breaking.

**Then execute what every old call still reaches.** Functions that kept their name and a
compatible call shape are run, on the same generated inputs built from the *old*
signature, in both versions. Comparing behaviour across an incompatible signature change
would find differences the signature already explained.

**Executing a package means sandboxing it.** The behaviour pass imports two versions of a
third-party package and calls its functions on generated inputs. That is running somebody
else's code on your machine, so the probe installs a sandbox first: writes are confined to
a scratch root, deletes, renames and metadata changes outside it are refused, the network
is refused except loopback, starting a process is refused, `ctypes` is refused, and reading
from a console raises instead of hanging.

It works in two layers, because one is not enough. Patching names in `os`, `builtins` and
`socket` covers what Python code reaches by those names; it cannot see a library doing its
I/O in C. An audit hook sits underneath, and CPython raises those events from inside the C
implementations, so the hook sees a call however it was reached and whatever reference it
was bound to.

The layering is measured, not asserted. **28 escape routes** are run against the sandbox
by `tests/test_sandbox_os_level.py` and `tests/test_sandbox_c_level.py`, and a third test
checks that this number is the number those two assert - it read 28 here while the tests
ran 10, because the full battery lived in a scratch directory and only the routes that had
once escaped were kept. The routes are: `nt`/`posix` (the C module `os` wraps) under every name, references bound before
the sandbox installed, `_socket` under `socket.socket`, `io.FileIO` and `_io` under `io`,
`sqlite3` opening a file in C, and `shutil` operating on paths outside the root. **8 of
those 28 escaped an earlier version of the sandbox** — `nt.unlink`, `nt.rename`, `nt.mkdir`,
`nt.utime`, `nt.truncate`, two pre-bound `os` references, and a raw `_socket` connect that
reached the real internet and failed only on a timeout. All 28 are refused now, and
`tests/test_sandbox_os_level.py` fails if any of them stops being.

None of those are hostile calls. Ordinary library code deletes a stale cache entry, renames
a file into place or touches a timestamp on import. Doing it on the machine of somebody who
only asked for a diff is the thing being prevented. The other half of each test asserts the
probe's own scratch writes, reads, imports and loopback lookups still work — a sandbox that
blocks everything is not safe, it is broken, and the behaviour pass needs all of them on
every run.

**Your code, not your dependencies.** `--used-by` resolves each file's imports, so a
project's own `parse()` is not credited to `packaging`. It skips virtualenvs (any directory
holding `pyvenv.cfg`), `site-packages`, `node_modules`, `.tox`, and `build/` or `dist/`
output that is not itself a package. Files are decoded the way Python decodes them
(PEP 263 coding cookies included), and any file it cannot read or parse is counted and
named in the output rather than skipped in silence.

## Run it

```bash
pip install git+https://github.com/hammasbuilds/blast-radius

blast-radius check packaging 21.3 24.0
blast-radius check packaging 21.3 24.0 --used-by path/to/your/repo
blast-radius check requests 2.28.0 2.31.0 --no-behaviour    # API diff only, fast
blast-radius check packaging 21.3 24.0 --out report/         # also writes REPORT.md + JSON
blast-radius check packaging 21.3 24.0 --json | jq .counts    # the JSON on stdout
```

PyPI release coming: `pip install blast-radius` (or `uv tool install` / `pipx install`)
will work once it is published.

`--json` prints the same object as `blast-radius.json` and nothing else on stdout; the
human report moves to stderr. If the run cannot finish, stdout stays empty and the exit
status is 2.

Needs no model, no API key, no GPU. It installs both versions itself, with `uv` if present
and `pip` otherwise, into throwaway directories it cleans up. The name you install and the
name you import may differ (`beautifulsoup4` is `bs4`); it reads the right one from the
package's metadata, and `--import-name` overrides.

### In CI

```bash
# fail the dependency-bump PR if your code references anything that broke
blast-radius check some-lib 1.2.0 1.3.0 --used-by . --fail-on used
```

`--fail-on` takes `gone`, `reshaped`, `silent`, `any` (all three) or `used` (any of the
three that your `--used-by` code references), comma-separated or repeated. `widened` and
`added` never fail a run: they cannot break a caller.

| exit | meaning |
|---:|---|
| 0 | finished; no `--fail-on` condition met |
| 1 | finished; a `--fail-on` condition was met |
| 2 | could not finish: bad arguments, a missing `--used-by` path, an install or import failure |

### From source

```bash
git clone https://github.com/hammasbuilds/blast-radius
cd blast-radius
uv sync                     # the package plus the dev group (pytest, ruff)
uv run pytest -q
uv run python demo.py
```

## Layout

```
src/blast_radius/
  probe.py    import one version in a subprocess; PROVE which file it loaded
  diff.py     gone / reshaped / added, then execute what survived unchanged
  report.py   sorted by what can reach you, silent changes first
  types.py    the kinds of change, and why they are ordered that way
scripts/
  semver_sweep.py   re-runs the 44-pair measurement behind docs/SEMVER.md
```

284 tests. `pytest --cov=blast_radius` reports **91%** of statements, with one gap worth
naming: the sandbox is a source string executed in the probe's subprocesses, so coverage
cannot see it at all and the 91% says nothing about the part that guards your machine.
That is measured separately, by running 28 escape routes against it.

## Scope

- **It does not read changelogs.** Deliberately. The changelog is the claim being checked.
- **It cannot reach every API.** A class whose constructor takes an argument is built with a
  generated one where the annotation allows a guess, and reported as needing an instance
  where the guess fails. On `click` 8.1.6 → 8.1.7 that is **42 of 234** stable callables.
- **Generated arguments, not real ones.** A pool of literals chosen per parameter from its
  annotation, varied one at a time. A behaviour change that only shows on a complex input
  will be missed. On click, **56 of 234** rejected every generated input and **4** were
  never handed an argument of the right type.
- **Public surface only.** A project reaching into private names is not covered.
- **It runs the package's code, behind a guard.** The behaviour pass calls public functions
  with generated arguments, in a subprocess that sees only the standard library and the
  installed version (not the environment blast-radius runs in), works in a throwaway temp
  dir, and has stdin closed. Functions whose names say they act on the machine are not run
  at all, and in the rest, starting a process, opening a network connection, reading the
  console and writing outside the temp dir raise `PermissionError` - in both versions
  alike, so it compares as a refusal. Deleting, renaming or touching a file outside the
  root is refused too, and so is `ctypes`. Two layers do it: name patches for what Python
  code reaches by name, and an audit hook underneath for what a library does in C. **28
  escape routes are run against it as a test; 8 of them escaped an earlier version.**
  That guards against *ordinary library code* - a stale cache entry deleted on import,
  a file renamed into place - and is not a security boundary against code written to
  break out: run it on packages you would install anyway. The price is that a behaviour
  change which needs a child process or the network is not seen.
- **Method calls on runtime objects are "possible", not proven.** `obj.contains(...)`
  cannot be tied to a class without running the code, so such sites are listed separately
  and do not trip `--fail-on used`.
- **An import path that disappears while the object survives is not reported.** Symbols
  are keyed on the object, so `packaging.specifiers.parse` (21.3 imported `parse` into that
  module; 24.0 does not) is folded into `packaging.version.parse`. Code importing through
  such an incidental path is still matched, but the path's removal is not listed as `gone`.
- **A function that never returns costs one `--timeout`, per version.** The probe abandons
  it after `--timeout` seconds (default 20) and carries on with the rest. The usual cause,
  a function waiting on the console (`click.getchar`, `click.prompt`), no longer gets that
  far: those are skipped by name and `input()` gets end-of-file, which is why the click
  runs take seconds where they used to take seven minutes.
- **Two packages.** `packaging` and `click`. Two upgrades are not a general claim about
  upgrades, and nothing here says how this behaves on a package shaped differently from
  both.

## Also worth reading

| | |
|---|---|
| &#128200; **[The semver sweep](https://github.com/hammasbuilds/blast-radius/blob/main/docs/SEMVER.md)** | 44 pairs: the 22 that made the promise, and 22 more on 0.x, which promises nothing. Every break named, API surface only |
| &#128202; **[Results](https://github.com/hammasbuilds/blast-radius/blob/main/docs/RESULTS.md)** | All three runs in full, with the limits |
| **[suite-auditor](https://github.com/hammasbuilds/suite-auditor)** | The same differential idea, pointed at a test suite |
| **[pr-referee](https://github.com/hammasbuilds/pr-referee)** | And pointed at a diff |
| **[repo-surgeon](https://github.com/hammasbuilds/repo-surgeon)** | And at a migration, refusing what it cannot prove |

## Keywords

dependency upgrade &middot; breaking change &middot; semantic versioning &middot; API diff
&middot; differential testing &middot; regression detection &middot; supply chain &middot;
dependabot &middot; CI &middot; python packaging &middot; inspect &middot; AST &middot;
call site analysis

## License

MIT - see [LICENSE](https://github.com/hammasbuilds/blast-radius/blob/main/LICENSE).
