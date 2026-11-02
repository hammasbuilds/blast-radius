"""Does a breaking change reach a real caller? Measured against real client code.

    python scripts/caller_census.py [--top 2000] [--dependents 5] [--out caller-census.json]

Section 4.4 of the manuscript estimates caller-level impact from the pair-level rate under
**uniform symbol choice**: a caller importing `k` of a module's `n` exported names is
assumed to pick them at random, so the chance it touches one of the `b` broken ones is
`1 - C(n-b, k)/C(n, k)`. The section states, rather than hides, that the assumption
overstates - breakage is concentrated in the unused tail (Gini 0.887), and real callers
import the popular names. What it could not say is by how much.

This measures it the way the literature does. Li et al. (2023) report the share of *client
programs* affected by a breaking change in Go by reading the clients; this does the same
for Python:

  1. A reverse-dependency index is built from the **external** top-downloads list by
     reading each package's PyPI metadata `requires_dist`. Position in that list cannot
     depend on this measurement, and a dependent enters the index by declaring the
     dependency rather than by being chosen here.
  2. For each broken pair in the cohorts whose version number promised compatibility, the
     surface comparison is re-run to recover **which** exported names broke. The published
     ledger keeps only how many; the names are what a caller can be checked against.
  3. Each dependent's newest sdist is downloaded and parsed with `ast` - not imported, and
     not executed. Every `from <module> import x`, every `import <module>` followed by
     `<module>.x`, and every aliased form of both, is collected as a name that dependent
     actually uses from that module.
  4. A (pair, dependent) observation is a **hit** if any name the dependent uses is among
     the names that broke.

Two rates are reported, because name matching has a choice in it and the choice moves the
answer. A broken qualname is dotted (`click.core.Option.foo`) and a dependent's usage is
also dotted but rooted differently (`core.Option`), so:

  * **exact** - the broken qualname ends with the used path, so the client names the
    broken thing itself. Unambiguous exposure, and the lower bound: a hit reached through
    a re-export under a different name is missed.
  * **enclosing** - the used path names an *ancestor* of the broken thing. Real exposure
    for a subclass or a caller of that member, none for a client that only instantiates
    the class, and the import graph cannot tell those apart.
  * **loose** - only the final components agree, so it can be a coincidence of naming.
    The upper bound.

Each tier contains the one above it. Reporting one alone would be picking a convenient end
of the range, and the middle tier is not a rounding detail: folding it into `exact` put the
headline at 76% for `click` on the strength of `Path.coerce_path_result`, an internal hook
that almost none of those clients call.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

TOP = "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages.min.json"
PROMISING = ("patch", "minor", "0.x-patch")
SCRATCH = Path(os.environ.get("BLAST_SCRATCH", ROOT / ".scratch" / "caller-census"))


def _pin_temp_to_scratch_drive() -> None:
    """Keep every temporary file off the system drive, which has filled before."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    tmp = SCRATCH / "tmp"
    cache = SCRATCH / "uv-cache"
    tmp.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(tmp)
    os.environ.setdefault("UV_CACHE_DIR", str(cache))
    tempfile.tempdir = None


def fetch_json(url: str, tries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"index unreachable: {type(last).__name__}: {last}")


def top_packages(count: int) -> list[str]:
    data = fetch_json(TOP)
    rows = data["rows"] if isinstance(data, dict) else data
    return [r["project"] for r in rows[:count]]


def _canonical(name: str) -> str:
    out = []
    for char in name.strip().lower():
        out.append("-" if char in "_.-" else char)
    return "".join(out)


def _requirement_name(spec: str) -> str | None:
    """The distribution name from a `requires_dist` entry, or None if unparseable.

    `requires_dist` entries look like `click>=8.0; extra == "cli"`. Only the leading name
    is wanted, and an entry that does not start with one is skipped rather than guessed at.
    """
    text = spec.strip()
    cut = len(text)
    for sep in (";", "[", "(", "<", ">", "=", "!", "~", " ", ","):
        index = text.find(sep)
        if index != -1:
            cut = min(cut, index)
    name = text[:cut].strip()
    return _canonical(name) if name and name[0].isalnum() else None


def reverse_index(names: list[str], say, workers: int = 8) -> dict[str, list[str]]:
    """package -> the packages in `names` that declare a dependency on it.

    Metadata only: one small JSON per package, no sdists. A package whose metadata cannot
    be read contributes nothing and is counted, because an index built from eight hundred
    of a thousand responses would understate every dependent count below without saying so.

    Concurrent, because sequentially it is two thousand round trips and the first run spent
    a quarter of an hour without finishing the first progress line. The dependency
    *direction* is read from each response independently, so order does not matter; the
    index is assembled under a lock and the dependent lists are sorted at the end, so the
    file does not depend on which response arrived first.
    """
    index: dict[str, set[str]] = {}
    unreadable: list[str] = []
    lock = threading.Lock()
    done = 0

    def one(name: str) -> None:
        nonlocal done
        try:
            data = fetch_json(f"https://pypi.org/pypi/{name}/json")
            specs = data.get("info", {}).get("requires_dist") or []
        except RuntimeError as exc:
            with lock:
                unreadable.append(f"{name}: {exc}")
                done += 1
            return
        found = set()
        for spec in specs:
            dependency = _requirement_name(spec)
            if dependency and dependency != _canonical(name):
                found.add(dependency)
        with lock:
            for dependency in found:
                index.setdefault(dependency, set()).add(name)
            done += 1
            if done % 100 == 0:
                say(f"  index: {done}/{len(names)} packages, {len(index)} with a dependent")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, names))
    say(f"  index: {done}/{len(names)} packages, {len(index)} with a dependent")
    out: dict[str, list[str]] = {key: sorted(value) for key, value in sorted(index.items())}
    out["__unreadable__"] = unreadable
    return out


def newest_archive(package: str) -> tuple[str, str] | None:
    """The newest final release's sdist or wheel url, and its version.

    A wheel is accepted here where the pair census insists on an sdist, because the only
    thing wanted is the client's own Python source and a pure-Python wheel carries it. A
    release publishing neither returns None.
    """
    data = fetch_json(f"https://pypi.org/pypi/{package}/json")
    version = data.get("info", {}).get("version")
    if not version:
        return None
    files = data.get("releases", {}).get(version) or []
    for kind in ("sdist", "bdist_wheel"):
        for entry in files:
            if entry.get("packagetype") == kind and not entry.get("yanked"):
                return entry["url"], version
    return None


def _sources(url: str) -> list[tuple[str, str]]:
    """(path, text) for every Python file in the archive at `url`.

    The archive is read in memory and never extracted to disk, and nothing in it is
    imported or executed - `ast.parse` only. A client package is somebody else's code
    fetched over the network, and running it to find out what it imports would be a far
    larger thing to do than this measurement needs.
    """
    with urllib.request.urlopen(url, timeout=120) as response:
        blob = response.read()
    out = []
    if url.endswith(".whl") or url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                if info.filename.endswith(".py") and info.file_size < 2_000_000:
                    out.append((info.filename, archive.read(info).decode("utf-8", "replace")))
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
            for member in archive.getmembers():
                if member.isfile() and member.name.endswith(".py") and member.size < 2_000_000:
                    handle = archive.extractfile(member)
                    if handle is not None:
                        out.append((member.name, handle.read().decode("utf-8", "replace")))
    return out


def import_roots(module_field: str, package: str) -> list[str]:
    """The top-level import names a client would use, from the ledger's `module` field.

    That field is a *display* string built by `_named`: 15 of the 163 pairs here carry
    something like `attrs,attr` or a list ending in "and 5 more", because a distribution can
    install several top-level packages. Splitting on the comma and dropping the tail gives
    the real roots; passing the whole string to `used_names` would have made its root
    `attrs,attr` and matched nothing, and a pair that matches nothing is indistinguishable
    from a pair no client uses.
    """
    roots = []
    for piece in module_field.replace(" and ", ",").split(","):
        name = piece.strip().split(".")[0]
        if name and name.isidentifier() and "more" not in name:
            roots.append(name)
    return roots or [package.replace("-", "_")]


def used_names(sources: list[tuple[str, str]], module: str) -> set[str]:
    """Dotted paths this client uses from `module`, relative to `module`.

    `from m import x` gives `x`; `from m.sub import x` gives `sub.x`; `import m` followed by
    `m.a.b` gives `a.b`; `import m.sub as s` followed by `s.x` gives `sub.x`. A bare
    `import m` with no attribute access contributes nothing, which is correct - a client
    that imports a module and touches none of its names cannot be broken by a name going
    away, and counting it as a user would be the uniform-choice assumption in another form.
    """
    found: set[str] = set()
    root = module.split(".")[0]
    for _path, text in sources:
        try:
            # Warnings suppressed, not shown. Parsing somebody else's source raises
            # SyntaxWarning for every invalid escape sequence in it, and a run over a few
            # hundred client packages buries its own progress under warnings that are
            # facts about their files rather than about this measurement.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(text)
        except SyntaxError:
            # Python 2 files and templates still ship inside sdists. A file that will not
            # parse is skipped; it is not evidence that the client uses nothing.
            continue
        # alias -> the dotted path inside `module` that the alias stands for.
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == root or alias.name.startswith(root + "."):
                        inside = alias.name[len(root) :].lstrip(".")
                        aliases[alias.asname or alias.name.split(".")[0]] = inside
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module != root and not node.module.startswith(root + "."):
                    continue
                prefix = node.module[len(root) :].lstrip(".")
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    dotted = f"{prefix}.{alias.name}" if prefix else alias.name
                    found.add(dotted)
                    aliases[alias.asname or alias.name] = dotted
        if not aliases:
            continue
        # The inner nodes of one chain are skipped. `click.testing.CliRunner` is three
        # nested Attribute nodes, and recording each of them yielded `testing` as a usage
        # alongside `testing.CliRunner`; a bare module path then matches every broken name
        # beneath it, so a client touching one function read as exposed to the whole
        # submodule. Only the outermost chain is what the source actually names.
        inner: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                inner.add(id(node.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and id(node) not in inner:
                parts = []
                cursor: ast.expr = node
                while isinstance(cursor, ast.Attribute):
                    parts.append(cursor.attr)
                    cursor = cursor.value
                if isinstance(cursor, ast.Name) and cursor.id in aliases:
                    base = aliases[cursor.id]
                    tail = ".".join(reversed(parts))
                    found.add(f"{base}.{tail}" if base else tail)
    return {name for name in found if name}


def _hits(used: set[str], broken: list[str]) -> dict:
    """Which broken names this client is exposed to, in three tiers of strength.

    A broken qualname is rooted at the distribution's module (`click.Path.coerce_path_result`)
    and a client's usage is rooted inside it (`Path`), so matching has a choice in it and
    the choice moves the answer by a lot. Three tiers rather than two, because the middle
    one turned out to carry most of the mass and is the weakest claim:

      * **exact** - the used path names the broken thing itself: the broken qualname ends
        with the used path, component-wise. A client doing `from click import Parameter`
        against `click.Parameter` reshaped. This is the tier that is unambiguously exposure.
      * **enclosing** - the used path names an *ancestor* of the broken thing:
        `click.Path.coerce_path_result` broke and the client uses `click.Path`. Real
        exposure for a subclass or for a caller of that member, and none at all for a
        client that merely instantiates the class and never touches it - which cannot be
        told apart from the import graph. Counted on its own rather than folded into
        `exact`, where it was: `coerce_path_result` is an internal hook of `click.Path`,
        every client using `click.Path(exists=True)` scored as hit, and the headline rate
        came out at 76% on the strength of a method almost none of them call.
      * **loose** - only the final components agree. Can be a coincidence of naming, so it
        is the upper bound.

    Each tier includes the one above it, so `exact <= enclosing <= loose` and the three
    bracket the answer instead of being three guesses.
    """
    exact = set()
    enclosing = set()
    loose = set()
    for qualname in broken:
        parts = qualname.split(".")
        for name in used:
            pieces = name.split(".")
            if len(pieces) <= len(parts) and parts[-len(pieces) :] == pieces:
                exact.add(qualname)
            elif any(parts[i : i + len(pieces)] == pieces for i in range(len(parts) - len(pieces))):
                enclosing.add(qualname)
            elif parts[-1] == pieces[-1]:
                loose.add(qualname)
    enclosing |= exact
    loose |= enclosing
    return {
        "exact": sorted(exact),
        "enclosing": sorted(enclosing),
        "loose": sorted(loose),
    }


DEFAULT_INDEX = ROOT / "docs" / "caller-index.json.gz"


def _read_index(path: Path) -> dict:
    """The reverse index, gzipped or not.

    Committed gzipped - 417 KB of JSON compresses to 71 KB - so the measurement reproduces
    without two thousand metadata requests against a throttled index. The plain form is
    still read, because a run that built its own index writes that.
    """
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def _write_index(index: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(index, indent=1).encode("utf-8")
    if path.suffix == ".gz":
        with gzip.open(path, "wb") as handle:
            handle.write(blob)
    else:
        path.write_bytes(blob)


def _sweep_pairs() -> list[dict]:
    """The sweep rows that already record which names broke.

    `semver_sweep.measure` returns the broken names as lists and the sweep file keeps them;
    the 1,000-package census collapses them to counts to keep its ledger small. So these
    rows carry, for free, exactly what the census rows would have to be re-measured to
    recover. Every cohort is kept rather than only the promising ones - a major bump is
    allowed to break, and what a caller actually experiences across one is worth a row of
    its own beside the ones that promised not to.
    """
    path = ROOT / "docs" / "semver-sweep.json"
    if not path.exists():
        raise SystemExit(f"no sweep file at {path}")
    out = []
    for row in json.loads(path.read_text(encoding="utf-8")):
        broken = list(row.get("exported_gone") or []) + list(row.get("exported_reshaped") or [])
        if not broken or not row.get("module"):
            continue
        out.append(
            {
                "package": row["package"],
                "old": row["old"],
                "new": row["new"],
                "cohort": row.get("bump", "?"),
                "module": row["module"],
                "exported_symbols": row.get("exported_symbols"),
                "exported_gone": row.get("exported_gone") or [],
                "exported_reshaped": row.get("exported_reshaped") or [],
            }
        )
    return out


def summarise(rows: list[dict]) -> dict:
    """The measured caller rate, bracketed, against the model's estimate for the same pairs.

    An observation is one (pair, dependent) reading. The rate is over observations rather
    than over pairs, because a pair with four hundred dependents and a pair with one are
    not equally informative about what a caller experiences, and averaging per-pair rates
    would weight them the same. Both are reported, because the per-pair mean is the one
    comparable to section 4.4's estimate, which is also a per-pair average.
    """
    usable = [r for r in rows if not r.get("error") and r.get("observations")]
    observations = [
        (row, seen)
        for row in usable
        for seen in row["observations"]
        if "skipped" not in seen and seen.get("names_used") is not None
    ]
    users = [(row, seen) for row, seen in observations if seen["names_used"]]
    out: dict = {
        "pairs_with_a_surface": len(usable),
        "observations": len(observations),
        "observations_where_the_client_uses_any_name": len(users),
        "observations_where_the_client_imports_but_uses_nothing": len(observations) - len(users),
    }
    if not observations:
        return out
    for which in ("exact", "enclosing", "loose"):
        hit = sum(1 for _row, seen in observations if seen.get(f"{which}_hits"))
        out[f"{which}_rate_over_observations"] = round(hit / len(observations), 4)
        out[f"{which}_hits"] = hit
        if users:
            among = sum(1 for _row, seen in users if seen.get(f"{which}_hits"))
            out[f"{which}_rate_among_clients_that_use_a_name"] = round(among / len(users), 4)
        per_pair = []
        for row in usable:
            seen_rows = [s for s in row["observations"] if "skipped" not in s]
            if seen_rows:
                per_pair.append(
                    sum(1 for s in seen_rows if s.get(f"{which}_hits")) / len(seen_rows)
                )
        out[f"{which}_mean_of_per_pair_rates"] = (
            round(sum(per_pair) / len(per_pair), 4) if per_pair else None
        )
    # Section 4.4's own model, evaluated on these clients rather than on an assumed
    # caller. Its formula is 1 - C(n-b, k)/C(n, k): the chance a caller importing k of n
    # exported names touches one of the b that broke, under uniform symbol choice. Every
    # input is taken from the observation - n and b from the pair, and **k from the number
    # of names this client was actually seen to use** - so the only thing that differs
    # between the two columns is *which* names a client picks. That is exactly the
    # assumption section 4.4 states it is making, isolated.
    # Imported, never reimplemented. The point of this script is that the model and the
    # measurement disagree, and a second copy of the model here would make that a
    # disagreement between two pieces of my own arithmetic.
    from semver_compliance import _at_least_one  # noqa: PLC0415

    predicted = []
    for row, seen in users:
        exported = row.get("exported_symbols") or 0
        broken = len(row.get("broken_names") or [])
        chance = _at_least_one(exported, broken, seen["names_used"])
        if chance is not None:
            predicted.append(chance)
    if predicted:
        measured = sum(1 for _row, seen in users if seen.get("exact_hits")) / len(users)
        out["model_under_uniform_choice"] = {
            "what": (
                "section 4.4's estimate, with n and b from the pair and k from the names "
                "this client actually uses. Only the choice of names differs."
            ),
            "observations_the_model_can_be_asked_about": len(predicted),
            "observations_too_small_for_the_model": len(users) - len(predicted),
            "mean_predicted_rate": round(sum(predicted) / len(predicted), 4),
            "measured_exact_rate": round(measured, 4),
            "ratio_measured_over_predicted": (
                round(measured / (sum(predicted) / len(predicted)), 2) if sum(predicted) else None
            ),
            "direction": (
                "understates" if measured > sum(predicted) / len(predicted) else "overstates"
            ),
        }
    out["note"] = (
        "A client that imports the module and touches none of its names cannot be broken "
        "by a name going away. Those observations are in the denominator, counted "
        "separately, and reported both ways: excluding them would measure the wrong "
        "population and including them silently would let a module's popularity stand in "
        "for its callers' exposure."
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=2000, help="packages in the reverse index")
    parser.add_argument(
        "--workers", type=int, default=8, help="concurrent metadata requests for the index"
    )
    parser.add_argument(
        "--dependents", type=int, default=5, help="dependents read per broken pair (default 5)"
    )
    parser.add_argument("--pairs", type=int, default=0, help="limit the pairs measured (0 = all)")
    parser.add_argument(
        "--from-sweep",
        action="store_true",
        help="take the broken names from docs/semver-sweep.json instead of re-running the "
        "surface comparison. Seven of the hand-picked sweep pairs already record the "
        "names, so this costs no installs at all - and three of them are click, "
        "coverage and urllib3, which have far more real dependents than the median "
        "census package. The sample is the sweep's, which was chosen by hand, so it "
        "answers 'how wrong is the uniform-choice model on well-used packages' and not "
        "'what is the rate across the ecosystem'. The census run answers the second",
    )
    parser.add_argument(
        "--resummarise",
        type=pathlib.Path,
        default=None,
        help="recompute the summary of an existing output file and rewrite it, without "
        "fetching anything. The per-observation rows are the data; the summary is "
        "arithmetic over them, so a change to the arithmetic should not cost another "
        "pass over the network - and the artefact stays something this script produced "
        "rather than something edited by hand",
    )
    parser.add_argument(
        "--install-timeout",
        type=float,
        default=600.0,
        help="seconds allowed for one version's install (default 600). A pair that "
        "exceeds it is recorded as an error rather than dropped",
    )
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "docs" / "caller-census.json")
    parser.add_argument(
        "--index",
        type=pathlib.Path,
        default=DEFAULT_INDEX if DEFAULT_INDEX.exists() else None,
        help="reverse index to reuse instead of rebuilding it, gzipped or plain. "
        f"Defaults to {DEFAULT_INDEX.name} when that exists",
    )
    args = parser.parse_args()
    _pin_temp_to_scratch_drive()

    def say(message: str) -> None:
        print(message, flush=True)

    if args.resummarise:
        payload = json.loads(args.resummarise.read_text(encoding="utf-8"))
        payload["summary"] = summarise(payload["rows"])
        args.resummarise.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        for key, value in payload["summary"].items():
            if key not in ("note", "model_under_uniform_choice"):
                say(f"  {key:<52}{value}")
        model = payload["summary"].get("model_under_uniform_choice")
        if model:
            say("")
            for key, value in model.items():
                if key != "what":
                    say(f"  model: {key:<45}{value}")
        say("")
        say(f"rewrote {args.resummarise}")
        return 0

    import metrics
    from semver_sweep import measure

    if args.from_sweep:
        promising = _sweep_pairs()
        say(
            f"{len(promising)} sweep pair(s) record the broken names, so no install is "
            "needed; cohorts: " + ", ".join(sorted({r["cohort"] for r in promising}))
        )
    else:
        rows = [r for r in metrics.measured(metrics.load()) if metrics.broke(r)]
        promising = [r for r in rows if r.get("cohort") in PROMISING]
        say(f"{len(promising)} broken pairs in the cohorts that promised compatibility")

    if args.index and args.index.exists():
        index = _read_index(args.index)
        say(f"reverse index reused from {args.index.name}: {len(index) - 1} packages")
    else:
        names = top_packages(args.top)
        say(f"building a reverse index over the top {len(names)} packages")
        index = reverse_index(names, say, args.workers)
        target = args.index or DEFAULT_INDEX
        _write_index(index, target)
        say(f"  written to {target.name}")
    unreadable = index.pop("__unreadable__", [])

    eligible = [r for r in promising if index.get(_canonical(r["package"]))]
    say(
        f"{len(eligible)} of them have at least one dependent in the index; "
        f"{len(promising) - len(eligible)} have none"
    )
    plan = eligible
    if args.pairs:
        # Counted apart from the eligibility filter. Printing one line after truncating
        # read as "3 have a dependent, 160 have none", which is a claim about the
        # ecosystem made by a --pairs flag.
        plan = eligible[: args.pairs]
        say(f"  --pairs {args.pairs}: measuring {len(plan)} of the {len(eligible)} eligible")

    results = []
    started = time.time()
    for position, row in enumerate(plan, 1):
        package = row["package"]
        record = {
            "package": package,
            "old": row["old"],
            "new": row["new"],
            "cohort": row["cohort"],
            "exported_symbols": row.get("exported_symbols"),
            "broken_count_in_the_ledger": metrics.symbols(row),
            "dependents_in_the_index": len(index[_canonical(package)]),
        }
        if args.from_sweep:
            got = {
                "module": row["module"],
                "exported_gone": row.get("exported_gone") or [],
                "exported_reshaped": row.get("exported_reshaped") or [],
            }
        else:
            work = Path(tempfile.mkdtemp(prefix="caller-", dir=SCRATCH / "tmp"))
            try:
                got = measure(
                    package, row["old"], row["new"], work, install_timeout=args.install_timeout
                )
            except Exception as exc:  # noqa: BLE001 - a crash is this pair's result
                got = {"error": f"{type(exc).__name__}: {exc}"}
        if got.get("error"):
            record["error"] = got["error"]
        else:
            broken = list(got.get("exported_gone") or []) + list(got.get("exported_reshaped") or [])
            record["broken_names"] = broken
            record["module"] = got.get("module")
            # The ledger's count and this one come from two runs of the same function over
            # whatever PyPI serves today. They can differ, and a difference is a fact about
            # the pair rather than an error, so both are kept.
            record["broken_count_now"] = len(broken)
            roots = import_roots(got.get("module") or "", package)
            record["import_roots"] = roots
            observations = []
            for dependent in index[_canonical(package)][: args.dependents]:
                seen: dict = {"dependent": dependent}
                try:
                    archive = newest_archive(dependent)
                    if archive is None:
                        seen["skipped"] = "publishes no sdist or wheel"
                    else:
                        url, version = archive
                        seen["version"] = version
                        sources = _sources(url)
                        used = set()
                        for root in roots:
                            used |= used_names(sources, root)
                        seen["names_used"] = len(used)
                        hit = _hits(used, broken)
                        seen["exact_hits"] = hit["exact"]
                        seen["enclosing_hits"] = hit["enclosing"]
                        seen["loose_hits"] = hit["loose"]
                except Exception as exc:  # noqa: BLE001
                    seen["skipped"] = f"{type(exc).__name__}: {exc}"[:160]
                observations.append(seen)
            record["observations"] = observations
        results.append(record)
        rate = (time.time() - started) / position
        say(
            f"  [{position}/{len(plan)}] {package} {row['old']}->{row['new']} "
            f"{'error' if 'error' in record else str(record.get('broken_count_now')) + ' broken'}"
            f"  {rate:.0f}s/pair ~{rate * (len(plan) - position) / 60:.0f}min left"
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "what": (
                        "Caller-level impact measured against real client code, against "
                        "section 4.4's estimate under uniform symbol choice."
                    ),
                    "top_packages_indexed": args.top,
                    "dependents_per_pair": args.dependents,
                    "pairs_in_the_promising_cohorts": len(promising),
                    "pairs_with_a_dependent_in_the_index": len(eligible),
                    "pairs_measured": len(plan),
                    "pairs_limit": args.pairs or None,
                    "metadata_unreadable": unreadable,
                    "source": "semver-sweep" if args.from_sweep else "pair-census",
                    "summary": summarise(results),
                    "rows": results,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    final = summarise(results)
    say("")
    for key, value in final.items():
        if key != "note":
            say(f"  {key:<52}{value}")
    say(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
