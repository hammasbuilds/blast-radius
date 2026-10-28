# blast-radius next to another tool

[griffe](https://mkdocstrings.github.io/griffe/) is the API differ behind mkdocstrings.
`griffe check` reads two trees of Python source and reports what broke between them, and it
is the closest thing to this tool that exists: both answer "what did this upgrade change
about the public API", both run offline, both are free.

They answer it differently. griffe **parses** source and never imports it; blast-radius
**installs and imports** both versions in separate subprocesses, then executes the functions
whose call shape survived. That difference decides everything below, in both directions.

Measured 2026-10-28, one machine, one interpreter, griffe 2.4.1, blast-radius 0.1.0, both
pointed at the same two installed directories per pair. Install time is excluded on both sides: the script
installs each version once, before either tool runs. Every number here is in
[`griffe-comparison.json`](griffe-comparison.json), written by
`scripts/griffe_comparison.py`, which installs both versions and runs both tools;
`tests/test_comparison_numbers.py` checks this page against the artefact.

## `urllib3` 2.2.1 -> 2.2.2, a patch release

| | griffe | blast-radius |
|---|---:|---:|
| findings | **13** | 3 breaking (2 reshaped, 1 silent), 1 added |
| distinct objects they are reported on | **2** | 3 |
| behaviour changes | not checked - nothing is imported | **1** |
| seconds | **under a second** | a few seconds |

Both tools found the same change: 2.2.2 inserts `version_string` into the constructors of
`BaseHTTPResponse` and `HTTPResponse`. griffe reports it as 13 breakages, 12 of which read
like this:

```text
urllib3.response.HTTPResponse.__init__
  Positional parameter was moved
  [positional or keyword] reason: str | None = None
    ->  [positional or keyword] reason: str | None = None
```

Old and new are the same text, because the parameter did not change - only its index did,
once a parameter was inserted ahead of it. One insertion, counted once per parameter it
shifted. blast-radius reports the two constructors.

Neither count is wrong, and the two are not comparable. A rate built on either is a
statement about the unit the tool counts in, which is why this project's census counts
**pairs in which an exported name went or changed shape**, never findings.

### The one thing only one tool can report

blast-radius also reports a behaviour change in this patch release, on a function whose
name and signature are identical in both versions:

```text
urllib3.HTTPResponse.read
  same signature (self, amt: 'int | None' = None, decode_content: 'bool | None' = None,
  cache_content: 'bool' = False) -> 'bytes'; 1 of 12 exercised inputs disagree
  input : (-1, True, True,)
    2.2.1 : raise: RuntimeError: buffer is empty
    2.2.2 : ok: None
```

griffe cannot report this and is not trying to: it never imports the package, so it cannot
call anything. This is the category this tool exists for, and the comparison's honest shape
is not "one tool beats the other" but "one of them has a question the other does not ask".

## `click` 7.1.2 -> 8.1.7, a major upgrade

| | griffe | blast-radius |
|---|---:|---:|
| findings | **104** | 16 gone, 11 reshaped, 9 widened, 4 silent, 51 added |
| distinct objects they are reported on | 32 | - |
| public objects reported removed | **28** | **16** |
| behaviour changes | not checked | **4** |
| seconds | **under a second** | a few seconds |

Again the grain differs: 40 of griffe's 104 are *positional parameter was moved*. Another
**15 are *base class was removed*, and all 15 are the word `object`**: click 7 wrote `class
Context(object):` for Python 2, and click 8 writes `class Context:`. Nothing a caller does
can trip on that, and it is a quarter of the gap between 104 and 16. Checked one by one
against click 7.1.2's source, not inferred - the check is in `tests/test_comparison_numbers.py`.

And again the interesting part is not the totals.

### griffe found four removals this tool could not see, and they were real

| removed from `click.core` | what it is |
|---|---|
| `SUBCOMMAND_METAVAR` | `"COMMAND [ARGS]..."` |
| `SUBCOMMANDS_METAVAR` | `"COMMAND1 [ARGS]... [COMMAND2 [ARGS]...]..."` |
| `DEPRECATED_HELP_NOTICE` | `" (DEPRECATED)"` |
| `DEPRECATED_INVOKE_NOTICE` | `"DeprecationWarning: ..."` |

`from click.core import SUBCOMMAND_METAVAR` works under click 7 and is an `ImportError`
under click 8. blast-radius reported none of them, because its surface walk described a name
only when it was a routine or a class - so the report whose first line reads *an import
error at startup* was blind to four import errors at startup.

Fixed: the surface walk now reads module-level constants as well, under three conditions
(immutable printable value, declared in `__all__` or spelled in capitals, and assigned in
that module's own file - the last is what keeps `from socket import AF_INET` out of the
package's surface). Only the name is compared; a constant whose value changed is a behaviour
change with no witness, and `__version__` changes every release. click's `gone` count moved
12 -> 16 and urllib3 2.2.1 -> 2.2.2 gained one addition, `urllib3.response.HAS_ZSTD`.

**This was the comparison's point.** Nothing in this project's own test suite would have
found it: a tool that reads only callables passes every test about callables.

### This tool found five removals griffe does not list, for a reason

| removed name | griffe's view |
|---|---|
| `click.types.filename_to_ui` | an alias to `click._compat.filename_to_ui`, `is_public = False` |
| `click.types.get_streerror` | same |
| `click.types.get_filesystem_encoding` | same |
| `click.utils.is_bytes` | same |
| `click.core.iteritems` | same |

In click 7.1.2 each is imported into a public module from the private `click._compat`
(`click/types.py:6`, `click/utils.py:9-11`, `click/core.py:10`). None of the five is defined
anywhere in click 8.1.7, so `from click.types import filename_to_ui` raised `ImportError`
the day click 8 shipped. griffe sees an alias whose target lives in a
private module, marks it non-public, and does not report its removal.

That is an editorial judgement, not a miss: the name was never meant to be public API, and
griffe is describing documented API. blast-radius reports what an import statement can
reach, so a re-export is in the surface whatever module it came from. Two different
questions again - and this one is worth knowing when a static differ reports an upgrade as
clean.

## What this does not show

- **Two packages.** click and urllib3, one major upgrade and one patch release. The census
  behind [SEMVER.md](SEMVER.md) walks thousands of pairs with this tool and none with
  griffe, so nothing here generalises to "finds more" in either direction.
- **griffe's 104 and 13 findings were not individually audited.** The 12 shifted-parameter
  rows and the four constants were read line by line; the rest were counted. Some of the
  remainder are very likely real changes blast-radius did not report, the same way the
  constants were.
- **No recall comparison is possible from this.** The tools do not share an operator set, a
  unit of counting, or a definition of public, and neither offers the other's. A fair recall
  number needs a labelled corpus of known breaks, which does not exist for Python packages.
- **griffe is several times faster on these pairs, and that is not a flaw in either tool.**
  Parsing source is cheaper than installing two versions and executing them, and executing
  them is the only way to get the behaviour column. Seconds are quoted loosely on purpose:
  they move run to run, and the exact figures of the run behind this page are in the
  artefact. The ordering is the stable part, and it is the part the guard test checks.
- **One version of each tool**, on Windows, on one Python. griffe's own output on a different
  Python may differ: it resolves annotations, and this tool deliberately ignores them.
