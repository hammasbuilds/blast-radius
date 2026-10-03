# Results

Three upgrades of two packages, run with:

```bash
git clone https://github.com/pypa/build pypa-build && git -C pypa-build checkout 8d1dd6b
blast-radius check packaging 21.3 24.0 --used-by pypa-build --out packaging-out
blast-radius check click 7.1.2 8.1.7 --out click7-out
blast-radius check click 8.1.6 8.1.7 --out click8-out
```

No language model anywhere. Every number below is read out of the `blast-radius.json` each
run wrote, committed here as `docs/*.json`. Regenerated on 2026-10-03 (Python 3.12,
Windows) after the probe stopped seeing the environment blast-radius runs in and the
behaviour pass gained its guard (see the click sections). Annotation text in the output
follows your Python: 3.14 prints `Optional[str]` as `str | None`.

## `packaging` 21.3 -> 24.0

| | count | who tells you |
|---|---:|---|
| gone | 2 | an `ImportError` at startup |
| reshaped | 0 | a type checker, if you run one |
| **silent** | **4** | **nothing** |
| widened | 3 | nobody needs to - existing calls still work |
| added | 9 | - |

Behaviour was compared on 14 functions; 8 could not be called in either version (5 need an
instance this tool could not build, 3 rejected every generated input).

### Why this page used to say gone 12, reshaped 5, silent 2

Those numbers were from the first version of the probe and were wrong, each in a way that
inflated them:

- **gone 12 -> 2.** What is actually gone is `LegacySpecifier` and `LegacyVersion`. Of the
  other ten, six were inherited methods counted once per class and path:
  `LegacySpecifier.contains` *is* `_IndividualSpecifier.contains`, which `Specifier` still
  has, so its removal is the removal of `LegacySpecifier`, already counted. The other four
  were **incidental import paths** - `packaging.requirements.Specifier`,
  `packaging.requirements.LegacySpecifier`, `packaging.specifiers.parse`,
  `packaging.specifiers.LegacyVersion` - names 21.3 happened to import into a second module.
  Those paths really do stop working in 24.0 (`from packaging.requirements import
  Specifier` raises), and the tool no longer lists that as `gone`: symbols are keyed on the
  object, not on every module that imported it. Code that imports through such a path is
  still matched to the object's change. This is a deliberate trade, listed under Limits.
- **reshaped 5 -> 0.** Two were aliases again. The other three - `canonicalize_name`,
  `canonicalize_version`, `SpecifierSet.contains` - only *gained* parameters with defaults,
  which no existing call can trip on. They are now `widened`.
- **silent 2 -> 4.** Because `widened` functions are still executed (on calls built from the
  old signature, which bind identically in both), `SpecifierSet.contains` got run - and it
  changed. `generic_tags` is the other addition; the first run exercised 7 functions, this
  one 14.

### The one that matters

```
packaging.version.parse
  same signature (version: str) -> Union[ForwardRef('LegacyVersion'), ForwardRef('Version')]; 3 of 4 exercised inputs disagree
  input : ("x",)
     21.3 : ok: <LegacyVersion('x')>
     24.0 : raise: InvalidVersion: Invalid version: 'x'
```

`parse("x")` returned a `LegacyVersion` and now raises. Same name, same signature. No import
fails and no type checker complains - this is the category the tool exists for, and it
takes execution to find.

### What reaches a real consumer

Pointed at [`pypa/build`](https://github.com/pypa/build) at commit `8d1dd6b` with
`--used-by` (36 Python files read):

| kind | symbol | where | |
|---|---|---|---|
| `silent` | `packaging.specifiers.SpecifierSet.contains` | `src/build/_util.py:66` | **possible** |
| `widened` | `packaging.utils.canonicalize_name` | `src/build/env.py:38`, `:211`, `:241` | proven |
| `added` | `packaging.metadata.parse_email` | `src/build/__main__.py:485` | proven |

The proven references cannot break pypa/build: one function gained an optional keyword,
the other is new. The one that can is the *possible* one. pypa/build calls
`req.specifier.contains(dist.version, prereleases=True)`, and in 24.0
`SpecifierSet.contains("x", prereleases=True)` raises `InvalidVersion` where 21.3 returned
`True` - so an installed distribution with a non-PEP 440 version now raises there. The line
is only "possible" because `req.specifier` is an object whose type exists at runtime; a
static pass cannot prove it is a `SpecifierSet`, so it is listed separately and does not
trip `--fail-on used`.

The previous version of this page listed eight references, including
`[gone] LegacySpecifier.contains at src/build/_util.py:66`. That was the old matcher crediting
any `.contains` to every class with a `contains` method - three "gone" and two "reshaped"
entries for one line of code, none of them the class actually being called.

## `click` 7.1.2 -> 8.1.7

| | count |
|---|---:|
| gone | 12 |
| reshaped | 11 |
| **silent** | **4** |
| widened | 9 |
| added | 51 |

184 functions kept a call shape every old call still binds to. 21 were not run because
their names say they act on the machine (`launch`, `edit`, `prompt`, `getchar`, ...); of the
163 run in both versions, behaviour was compared on 87, and 76 could not be called in either
(41 rejected every generated input, 31 need an instance this tool could not build, 3 were
never handed an argument of the right type, 1 needed different constructor arguments in the
two versions). About six seconds. Seven signatures were readable in only one version and
are neither compared nor counted as reshaped.

The four silent changes, each a real difference in what click 8 returns:

| function | input | 7.1.2 | 8.1.7 |
|---|---|---|---|
| `click.Choice.get_missing_message` | `("",)` | `'Choose from:\n\tx.'` | `'Choose from:\n\tx'` |
| `click.FileError.format_message` | `()` | `'Could not open file x: unknown error'` | `"Could not open file 'x': unknown error"` |
| `click.NoSuchOption.format_message` | `()` | `'no such option: x'` | `'No such option: x'` |
| `click.types.StringParamType.convert` | `(0, "", "")` | `0` | `'0'` |

Not counted, and listed in the report as such:

- five functions differ only where one version raised `TypeError` or `AttributeError` on a
  generated argument (`Argument.get_default("")` passes a string where a `Context` belongs,
  and 7.1.2 happened to ignore it). No real caller passes those.
- `click.Path.convert` disagreed with *itself* when re-run, so its difference is dropped as
  non-deterministic rather than reported.
- decorators such as `help_option` and `version_option` return a closure that click 8 builds
  through `option()`, so it reprs as `option.<locals>.decorator`. Closure reprs are
  normalised before comparing.

The first runs of this comparison reported gone 50, reshaped 68, added 292 (before aliases
were de-duplicated and additive signature changes became `widened`). The previous version
of this page (2026-09-27, Python 3.14) said gone 15, reshaped 13, widened 14, added 51.
Since then functions are skipped by name, a signature readable in only one version is no
longer counted as reshaped, and the probe no longer imports from the environment
blast-radius runs in. Only the last was measured on its own: it moved gone from 13 to 12
and added from 50 to 51, because click 7.1.2 defines `termui.get_winterm_size` only when
`colorama` imports, colorama is not a click 7 dependency, and whether it imported depended
on what was installed next to the tool.

## `click` 8.1.6 -> 8.1.7

No API change. Of 234 stable callables, 21 are not run by name; of the 213 run in both
versions, **111 were exercised** and **no behaviour changed** on the inputs tried. The 102
not exercised: 56 ran and rejected every generated input, 42 need an instance this tool
could not build, 4 were never handed an argument of the right type. About three seconds.

An earlier run of this page reported 124 exercised and one silent change,
`BashComplete.source`: on a machine with no `bash`, 8.1.6 raises *Couldn't detect Bash
version* and 8.1.7 warns and returns the completion script. Called directly, outside the
probe, that still reproduces. The behaviour pass now refuses to let a probed function start
a process, so both versions fail at the same `subprocess.run` and the difference is not
reported - the cost of the guard. That run also took 410 seconds, nearly all of it
`click.getchar`, `click.prompt` and `click.termui.hidden_prompt_func` waiting on a console
once per version; those are now skipped by name.

That earlier run was itself a correction. This page once reported **0 exercised, 400
unreachable**, and explained it: click is classes and decorators needing a constructed
`Context`, so a tool calling functions with literals cannot reach them. That explanation was
wrong. Four defects were producing the zero, and none of them was about click:

| the defect | what it did |
|---|---|
| the payload travelled on the command line | 12,290 characters returned nothing with exit code 0; 400 functions hit `WinError 206`. The default limit is 400, so on Windows the behaviour pass silently did nothing on any package worth checking |
| `SystemExit` was not caught | it inherits `BaseException`, so `except Exception` missed it; one click command calling `sys.exit()` ended the whole run |
| `_params` mis-parsed a return annotation | `(value: int, name: str = "x") -> bool` read as ONE parameter, so every annotated function was called short and raised `TypeError`. click annotated its entire API in v8 |
| a class needing a constructor argument was refused outright | 95 of 234 - a larger bucket than the 54 the tool could then exercise |

## What this cost to learn

**Sixteen separate sources of confident wrong answers**, each fixed and each pinned by a
test. Four were found while building the tool; the other twelve came out of one session of
asking why click reported nothing, and every one of them was silent - not a single crash
among them until the last two.

| the bug | what it reported |
|---|---|
| `sys.path` silently ignores an entry that does not exist | both probes loaded the *installed* copy, so a known-breaking upgrade "changed nothing" |
| `packaging.tags.Tag.__repr__` embeds `id(self)` in **decimal** | four identical tag lists read as four silent behaviour changes |
| `dir()` returns names a module merely imported | `packaging` 21.3 does `from pyparsing import ...`, so dropping pyparsing read as **455** removed symbols |
| comparing signature **text** rather than call shape | click 8 adding type hints read as **795** reshaped, leaving **0** functions for the behaviour pass |

The last of those four is the subtlest. Adding a type annotation cannot break a caller, but it
changes `str(inspect.signature(f))` - so the comparison reported 795 breaking changes *and*
starved the only stage that finds what nothing else warns about. Comparing parameter names,
kinds and defaults instead took it to 68 and unblocked the pass.

### The twelve found by asking why click reported nothing

Each of these on its own was enough to produce a zero, and the first explanation on the page
for that zero blamed click's design instead.

| the bug | what it did |
|---|---|
| the payload travelled on the command line | 12,290 characters returned nothing with exit code 0; 400 functions hit `WinError 206` |
| `SystemExit` escaped `except Exception` | it inherits `BaseException`. One click command calling `sys.exit()` ended the run mid-batch |
| a called function printing to stdout | results cross the process boundary as one `__BR_JSON__` line, and click's entry points print usage text into it |
| a generated literal was bound to `self` | `Argument.add_to_parser("a", "b")` with the string `"a"` as the Argument. A method not touching `self` runs happily and compares two calls no user could make |
| results were reported only at the very end | anything stopping the interpreter discarded the 141 already computed and never attempted the 93 after |
| the child inherited stdin | `click.confirm` waits for a keypress that never comes, and took the batch with it |
| `_recover` could not name the culprit when the FIRST function hung | the one case with no completed results to carry the news |
| `str(exc)` raised inside `except` | `ClickException.__str__` returns `self.message` unchanged, so `File.fail(1)` yields an exception whose `str()` raises `TypeError`. `click.File.fail` was blamed for it |
| `_params` mis-parsed a return annotation | `(value: int, name: str = "x") -> bool` read as ONE parameter, so every annotated function was called short |
| `argument_sets` ignored annotations | a parameter annotated `int` was offered `""` first |
| a class needing a constructor argument was refused | 95 of 234 on click, larger than the 54 then exercised |
| a temp directory that would not delete | `PermissionError [WinError 32]` from the cleanup path, after the answer was in hand |

And one that was not silent, only invisible: the child's stdout was a **pipe**, so a
grandchild spawned by a probed function inherited it and killing the child at the deadline
could not close it. `subprocess.run(timeout=60)` then waited in `communicate()` for an EOF
that never came. A comparison bounded to twelve minutes sat for fifty-six with `Notepad.exe`
alive in the process tree - `click.edit` had opened it. A `--timeout` that can be ignored is
worse than none, because the run looks like it is still working.

## Limits

- **Two packages.** `packaging` and `click`. Neither is a general claim about upgrades.
- **The guard hides what needs a process or the network.** A probed function may not start
  a process, connect anywhere, read the console or write outside its temp directory; a
  behaviour change that depends on one of those (`BashComplete.source` above) is not seen.
- **Generated arguments, not real ones.** A function is called with literals from a small
  pool, chosen per parameter from its annotation. A class whose constructor takes an argument
  is built with a generated one where the annotation allows a guess, and reported as needing
  an instance where the guess fails.
- **Public surface only.** Private names are skipped; a project reaching into them is not
  covered.
- **A vanished import path is not `gone` if the object survives.** See the packaging
  section: `packaging.requirements.Specifier` stopped resolving in 24.0 and is not listed.
- **Call-site matching resolves imports.** A name is credited only where the file imports it,
  so a project with its own `parse` is no longer counted as using `packaging.version.parse`.
  What this still cannot see is dynamic access - `getattr` on a module, `importlib` by string.
