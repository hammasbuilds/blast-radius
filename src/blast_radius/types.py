"""What an upgrade can break, in three increasingly quiet ways.

A dependency bump arrives as a pull request with a version number and a changelog. Neither
is evidence. This tool sorts what actually changed into three kinds, and the ordering is by
how likely each is to reach production unnoticed:

**GONE** - a name that existed and does not. Loud: an ImportError at startup, caught by any
CI run that imports the module.

**RESHAPED** - the name survives and its signature changed. Quieter: caught by a type
checker if you run one against a typed dependency, and by a test only if a test calls it
that way.

**WIDENED** - the signature changed, but only in ways every existing call survives: a new
parameter with a default, a positional-only parameter that can now also be passed by name.
Listed so nothing is hidden, and never counted as a break. The behaviour pass still runs
these, using calls that are valid against both versions.

**SILENT** - same name, same signature, different answer. Nothing catches this. No import
fails, no type checker complains, no changelog entry is required by convention. It is the
category this tool exists for, and the only one that needs execution to find.

A fourth axis cuts across all three: **do you actually use it?** An upgrade that breaks
forty functions you never call is a non-event, and the same upgrade breaking one you call
in a loop is an incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    GONE = "gone"
    RESHAPED = "reshaped"
    WIDENED = "widened"
    SILENT = "silent"
    ADDED = "added"


SEVERITY = {Kind.SILENT: 4, Kind.GONE: 3, Kind.RESHAPED: 2, Kind.WIDENED: 1, Kind.ADDED: 0}

BREAKING = frozenset({Kind.GONE, Kind.RESHAPED, Kind.SILENT})
"""The kinds that can break a caller. WIDENED and ADDED cannot."""


@dataclass(frozen=True)
class Symbol:
    """One public thing in a package, as it appears in one version."""

    qualname: str
    kind: str  # "function" | "class" | "method" | "other"
    signature: str = ""
    doc_first_line: str = ""

    @property
    def module(self) -> str:
        return self.qualname.rpartition(".")[0]


@dataclass
class Change:
    kind: Kind
    qualname: str
    detail: str = ""
    witness: dict | None = None
    """For a SILENT change: the input, and what each version returned.

    Nothing else in this tool needs a witness, because nothing else is in doubt. A name
    that is gone is gone. A behaviour change is a claim, and a claim needs proof.
    """

    used_at: list[str] = field(default_factory=list)
    """Where the target project calls it. Empty means the change cannot reach you."""

    before: str = ""
    """For RESHAPED and WIDENED: the old signature. `detail` joins the two with " -> ",
    which cannot be split back apart once return annotations contain " -> " too."""

    after: str = ""

    maybe_at: list[str] = field(default_factory=list)
    """Where the target project calls a METHOD of this name on an object whose type cannot
    be known without running it (`req.specifier.contains(...)`). Possible, not proven, so
    it ranks below `used_at` and never trips `--fail-on used`."""

    aliases: list[str] = field(default_factory=list)
    """Other import paths for the same object, so a caller writing any of them is matched."""

    @property
    def severity(self) -> int:
        # A change you actually call outranks one you do not, whatever its kind.
        return SEVERITY[self.kind] + (10 if self.used_at else 5 if self.maybe_at else 0)

    def as_row(self) -> dict:
        return {
            "kind": self.kind.value,
            "qualname": self.qualname,
            "detail": self.detail[:300],
            "witness": self.witness,
            "used_at": self.used_at[:10],
            "maybe_at": self.maybe_at[:10],
        }


@dataclass
class Report:
    package: str = ""
    old_version: str = ""
    new_version: str = ""
    changes: list[Change] = field(default_factory=list)
    compared: int = 0
    """Public callables actually exercised. The denominator for any behaviour claim."""

    unreachable: int = 0
    """Callables that could not be exercised in either version - no verdict either way."""

    seconds: float = 0.0

    used_by: str | None = None
    """The path given to --used-by, or None when no project was matched against."""

    scanned_files: int = 0
    """How many Python files under `used_by` were read."""

    behaviour_checked: bool = False
    """False when the behaviour pass did not run (--no-behaviour). A SILENT count of zero
    then means "not looked for", and the report has to say so rather than print a 0."""

    def of(self, kind: Kind) -> list[Change]:
        return [c for c in self.changes if c.kind is kind]

    @property
    def reaching_you(self) -> list[Change]:
        return [c for c in self.changes if c.used_at]

    @property
    def maybe_reaching_you(self) -> list[Change]:
        return [c for c in self.changes if c.maybe_at and not c.used_at]

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.kind in BREAKING]

    def sorted(self) -> list[Change]:
        return sorted(self.changes, key=lambda c: (-c.severity, c.qualname))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.changes:
            out[c.kind.value] = out.get(c.kind.value, 0) + 1
        return out
