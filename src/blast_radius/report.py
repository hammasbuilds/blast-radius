"""The report, ordered by what can actually reach you.

Sorting is by severity, and severity puts **anything your code references** above anything
it does not, whatever the kind. An upgrade that removes forty functions you never call is a
non-event; the same upgrade changing one you call in a loop is an incident, and a report
that lists them in alphabetical order makes you find that out yourself.

Within that, silent behaviour changes come first. They are the only kind nothing else will
tell you about: an import error announces itself, a signature change is caught by a type
checker, and a new function cannot break anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from blast_radius.types import Kind, Report

HEADLINE = {
    Kind.SILENT: "same name, same signature, different answer",
    Kind.GONE: "no longer exists",
    Kind.RESHAPED: "signature changed",
    Kind.ADDED: "new",
}


def summary(report: Report) -> str:
    counts = report.counts()
    lines = [
        "=" * 78,
        f"BLAST RADIUS - {report.package} {report.old_version} -> {report.new_version}",
        "=" * 78,
        f"  gone      {counts.get('gone', 0):5}   an import error at startup",
        f"  reshaped  {counts.get('reshaped', 0):5}   a type checker would catch these",
        f"  SILENT    {counts.get('silent', 0):5}   nothing catches these",
        f"  added     {counts.get('added', 0):5}",
    ]
    if report.compared or report.unreachable:
        lines.append(
            f"\n  behaviour compared on {report.compared} function(s); "
            f"{report.unreachable} could not be called in either version"
        )

    silent = report.of(Kind.SILENT)
    if silent:
        lines.append(f"\n  {len(silent)} silent change(s) - the ones with no other warning:\n")
        for c in silent[:10]:
            w = c.witness or {}
            lines.append(f"  {c.qualname}")
            lines.append(f"    {c.detail}")
            lines.append(f"    input : {w.get('args', '?')}")
            lines.append(f"    {report.old_version:>7} : {w.get('old', '?')[:90]}")
            lines.append(f"    {report.new_version:>7} : {w.get('new', '?')[:90]}")
            if c.used_at:
                lines.append(f"    YOU CALL IT AT: {', '.join(c.used_at[:3])}")
            lines.append("")
        if len(silent) > 10:
            lines.append(f"  ... and {len(silent) - 10} more in the JSON")

    reaching = report.reaching_you
    if reaching:
        lines.append(f"\n  {len(reaching)} change(s) your code references:")
        for c in sorted(reaching, key=lambda x: -x.severity)[:15]:
            lines.append(
                f"    [{c.kind.value:8}] {c.qualname}  ({len(c.used_at)} site(s), "
                f"e.g. {c.used_at[0]})"
            )
    elif report.changes:
        lines.append(
            "\n  None of these were matched to your code - either it does not use the\n"
            "  changed parts, or no repo was given with --used-by."
        )

    if not report.changes:
        lines.append(
            "\n  Nothing changed in the public surface, and nothing behaved differently\n"
            "  on the inputs tried. That is a statement about this comparison, not a\n"
            "  guarantee: private APIs, and anything not exercised, are not covered."
        )

    lines.append(f"\n  took {report.seconds:.0f}s")
    return "\n".join(lines)


def write_json(report: Report, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "package": report.package,
                "old_version": report.old_version,
                "new_version": report.new_version,
                "counts": report.counts(),
                "compared": report.compared,
                "unreachable": report.unreachable,
                "reaching_you": len(report.reaching_you),
                "seconds": round(report.seconds, 1),
                "changes": [c.as_row() for c in report.sorted()],
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="",
    )


def write_markdown(report: Report, path: Path) -> None:
    counts = report.counts()
    out = [
        f"# `{report.package}` {report.old_version} → {report.new_version}",
        "",
        "| | count | who tells you |",
        "|---|---:|---|",
        f"| gone | {counts.get('gone', 0)} | an ImportError at startup |",
        f"| reshaped | {counts.get('reshaped', 0)} | a type checker |",
        f"| **silent** | **{counts.get('silent', 0)}** | **nothing** |",
        f"| added | {counts.get('added', 0)} | - |",
        "",
    ]
    silent = report.of(Kind.SILENT)
    if silent:
        out += [
            "## Silent behaviour changes",
            "",
            "Same name, same signature, different result. No import fails and no type",
            "checker complains.",
            "",
            f"| function | input | {report.old_version} | {report.new_version} | you call it |",
            "|---|---|---|---|---|",
        ]
        for c in silent:
            w = c.witness or {}
            esc = lambda s: str(s).replace("|", "\\|")[:70]  # noqa: E731
            out.append(
                f"| `{c.qualname}` | `{esc(w.get('args'))}` | `{esc(w.get('old'))}` | "
                f"`{esc(w.get('new'))}` | {len(c.used_at) or ''} |"
            )
    gone = [c for c in report.of(Kind.GONE) if c.used_at]
    if gone:
        out += ["", "## Removed, and referenced by your code", ""]
        out += [f"- `{c.qualname}` — {', '.join(c.used_at[:5])}" for c in gone]
    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="")
