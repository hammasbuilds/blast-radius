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

**Behaviour compared on 0 functions; 400 could not be called
in either version.** That is the honest result and it is a limitation, not a clean bill of
health.

`packaging` is largely pure functions over strings, so they can be called with generated
values. `click` is classes and decorators that need a constructed `Context` before anything
does useful work, and a tool that calls functions with literals cannot reach them. The
report says "could not be called" rather than "no changes found", because those are
different claims.

## What this cost to learn

Four separate sources of confident wrong answers, each fixed and each pinned by a test:

| the bug | what it reported |
|---|---|
| `sys.path` silently ignores an entry that does not exist | both probes loaded the *installed* copy, so a known-breaking upgrade "changed nothing" |
| `packaging.tags.Tag.__repr__` embeds `id(self)` in **decimal** | four identical tag lists read as four silent behaviour changes |
| `dir()` returns names a module merely imported | `packaging` 21.3 does `from pyparsing import ...`, so dropping pyparsing read as **455** removed symbols |
| comparing signature **text** rather than call shape | click 8 adding type hints read as **795** reshaped, leaving **0** functions for the behaviour pass |

The last is the subtlest. Adding a type annotation cannot break a caller, but it changes
`str(inspect.signature(f))` - so the comparison reported 795 breaking changes *and* starved
the only stage that finds what nothing else warns about. Comparing parameter names, kinds
and defaults instead took it to 68 and unblocked the pass.

## Limits

- **Two upgrades.** One found a silent change, one could not be executed at all. Neither is
  a general claim about upgrades.
- **Generated arguments, not real ones.** A function is called with literals from a small
  pool. Anything needing a constructed object is unreachable, and reported as such.
- **Public surface only.** Private names are skipped; a project reaching into them is not
  covered.
- **Call-site matching is by trailing name**, so it over-reports: a project with its own
  `parse` is credited with using `packaging.version.parse`. Over-reporting is the right
  direction - a missed call site is a break that reaches production, a spurious one costs
  ten seconds.
