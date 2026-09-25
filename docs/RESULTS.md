# Results

Two upgrades, run with:

```bash
blast-radius check packaging 21.3 24.0 --used-by /path/to/pypa-build
blast-radius check click 7.1.2 8.1.7
```

No language model anywhere. Every number below is read out of `docs/*.json`.

## `packaging` 21.3 -> 24.0

| | count | who tells you |
|---|---:|---|
| gone | 12 | an `ImportError` at startup |
| reshaped | 5 | a type checker, if you run one |
| **silent** | **2** | **nothing** |
| added | 10 | - |

Behaviour was compared on 7 function(s); 15 could not be
called in either version.

### The one that matters

```
packaging.version.parse
  same signature (version: str)
  input : ("",)
   21.3 : ok: <LegacyVersion('')>
   24.0 : raise: InvalidVersion: Invalid version: ''
```

`parse("")` returned a `LegacyVersion` and now raises. Same name, same signature. No import
fails and no type checker complains - this is the category the tool exists for, and it takes
execution to find.

### What reaches a real consumer

Pointed at [`pypa/build`](https://github.com/pypa/build) with `--used-by`,
**8 of the changes are referenced by its source**, with file and line:

| kind | symbol | where |
|---|---|---|
| `gone` | `packaging.requirements.LegacySpecifier.contains` | `src/build/_util.py:66` |
| `gone` | `packaging.requirements.Specifier.contains` | `src/build/_util.py:66` |
| `gone` | `packaging.specifiers.LegacySpecifier.contains` | `src/build/_util.py:66` |
| `reshaped` | `packaging.requirements.SpecifierSet.contains` | `src/build/_util.py:66` |
| `reshaped` | `packaging.specifiers.SpecifierSet.contains` | `src/build/_util.py:66` |
| `reshaped` | `packaging.utils.canonicalize_name` | `src/build/env.py:241` |
| `added` | `packaging.metadata.parse_email` | `src/build/__main__.py:485` |
| `added` | `packaging.requirements.canonicalize_name` | `src/build/env.py:241` |

An upgrade removing forty functions nobody calls is a non-event. The same upgrade touching
one you call in a loop is an incident, and that is why the report sorts by this before
anything else.

## `click` 7.1.2 -> 8.1.7

| | count |
|---|---:|
| gone | 50 |
| reshaped | 68 |
| silent | 0 |
| added | 292 |

**Behaviour compared on 116 of 234 stable callables** (measured on 8.1.6 -> 8.1.7, where the
stable set is largest).

This page used to report **0 exercised, 400 unreachable**, and explained it: click is classes
and decorators needing a constructed `Context`, so a tool calling functions with literals
cannot reach them. That explanation was wrong, and comfortable enough to survive a while.
Four defects were producing the zero, and none of them was about click:

| the defect | what it did |
|---|---|
| the payload travelled on the command line | 12,290 characters returned nothing with exit code 0; 400 functions hit `WinError 206`. The default limit is 400, so on Windows the behaviour pass silently did nothing on any package worth checking |
| `SystemExit` was not caught | it inherits `BaseException`, so `except Exception` missed it; one click command calling `sys.exit()` ended the whole run |
| `_params` mis-parsed a return annotation | `(value: int, name: str = "x") -> bool` read as ONE parameter, so every annotated function was called short and raised `TypeError`. click annotated its entire API in v8 |
| a class needing a constructor argument was refused outright | 95 of 234 - a larger bucket than the 54 the tool could then exercise |

| | before | after |
|---|---:|---:|
| exercised | 54 | **116** |
| needs an instance | 95 | **43** |
| no generated argument reached it | 81 | 71 |

The remaining 71 is the honest limit, and it is now reported as its own line rather than
folded into "could not be called": a function this tool never handed a valid argument to is a
fact about the tool, and one that ran and refused is a fact about the package.

Three functions still cannot be reached at all - `click.getchar`, `click.prompt` and
`click.termui.hidden_prompt_func` read the console directly, so closing stdin does not stop
them. The probe restarts past each and keeps everything already finished; the cost is one
full `--timeout` per function.

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

- **Two upgrades.** One found a silent change, one could not be executed at all. Neither is
  a general claim about upgrades.
- **Generated arguments, not real ones.** A function is called with literals from a small
  pool, chosen per parameter from its annotation. A class whose constructor takes an argument
  is built with a generated one where the annotation allows a guess, and reported as needing
  an instance where the guess fails.
- **Public surface only.** Private names are skipped; a project reaching into them is not
  covered.
- **Call-site matching resolves imports.** A name is credited only where the file imports it,
  so a project with its own `parse` is no longer counted as using `packaging.version.parse`.
  What this still cannot see is dynamic access - `getattr` on a module, `importlib` by string.
