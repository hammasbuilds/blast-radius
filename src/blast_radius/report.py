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

from blast_radius.types import Change, Kind, Report

HEADLINE = {
    Kind.SILENT: "same name, same signature, different answer",
    Kind.GONE: "no longer exists",
    Kind.RESHAPED: "signature changed",
    Kind.WIDENED: "signature grew, existing calls still work",
    Kind.ADDED: "new",
}


def around_difference(a: str, b: str, width: int = 90) -> tuple[str, str]:
    """Both strings cut to `width`, around the first place they differ.

    Cutting both at the same fixed width shows two identical prefixes whenever the
    difference is further in - `sys_tags` returns hundreds of tags, and the first
    ninety characters of both lists were the same, so the report printed two
    identical lines under a heading claiming they differ.
    """
    if len(a) <= width and len(b) <= width:
        return a, b
    i = next((k for k, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), None)
    if i is None:
        i = min(len(a), len(b))
    start = max(0, i - width // 3)
    # Snap back to a separator so the window starts on a whole item where possible.
    snap = max(a.rfind(", ", 0, start), b.rfind(", ", 0, start))
    if start and snap != -1 and start - snap < width // 3:
        start = snap + 2

    def cut(s: str) -> str:
        piece = s[start : start + width]
        return ("..." if start else "") + piece + ("..." if start + width < len(s) else "")

    return cut(a), cut(b)


def _count_line(label: str, n: int | str, note: str) -> str:
    return f"  {label:<9} {n:>5}   {note}".rstrip()


def _by_severity(changes: list[Change]) -> list[Change]:
    return sorted(changes, key=lambda c: (-c.severity, c.qualname))


def summary(report: Report) -> str:
    counts = report.counts()
    silent_n: int | str = counts.get("silent", 0) if report.behaviour_checked else "-"
    silent_note = (
        "nothing catches these"
        if report.behaviour_checked
        else "not checked: the behaviour pass was skipped (--no-behaviour)"
    )
    lines = [
        "=" * 78,
        f"BLAST RADIUS - {report.package} {report.old_version} -> {report.new_version}",
        "=" * 78,
        _count_line("gone", counts.get("gone", 0), "an import error at startup"),
        _count_line("reshaped", counts.get("reshaped", 0), "a type checker would catch these"),
        _count_line("SILENT", silent_n, silent_note),
        _count_line("widened", counts.get("widened", 0), "not breaking: existing calls still work"),
        _count_line("added", counts.get("added", 0), ""),
    ]
    if report.behaviour_checked:
        lines.append(
            f"\n  behaviour compared on {report.compared} function(s); "
            f"{report.unreachable} could not be called in either version"
        )

    silent = _by_severity(report.of(Kind.SILENT))
    if silent:
        lines.append(f"\n  {len(silent)} silent change(s) - the ones with no other warning:\n")
        for c in silent[:10]:
            w = c.witness or {}
            old, new = around_difference(str(w.get("old", "?")), str(w.get("new", "?")))
            lines.append(f"  {c.qualname}")
            lines.append(f"    {c.detail}")
            lines.append(f"    input : {w.get('args', '?')}")
            lines.append(f"    {report.old_version:>7} : {old}")
            lines.append(f"    {report.new_version:>7} : {new}")
            if c.used_at:
                lines.append(f"    YOU CALL IT AT: {', '.join(c.used_at[:3])}")
            elif c.maybe_at:
                lines.append(f"    you may call it at: {', '.join(c.maybe_at[:3])}")
            lines.append("")
        if len(silent) > 10:
            lines.append(f"  ... and {len(silent) - 10} more (--out writes them all)")

    if report.weak:
        names = ", ".join(c.qualname for c in report.weak[:6])
        more = " ..." if len(report.weak) > 6 else ""
        lines.append(
            f"\n  Not counted: {len(report.weak)} function(s) differ only where one version"
            " rejects a generated\n  argument as the wrong type (TypeError/AttributeError)"
            f" - no real caller passes those.\n    {names}{more}"
        )

    for kind in (Kind.GONE, Kind.RESHAPED):
        items = _by_severity(report.of(kind))
        if not items:
            continue
        lines.append(f"\n  {kind.value}:")
        for c in items[:15]:
            where = f"   <- your code: {c.used_at[0]}" if c.used_at else ""
            lines.append(f"    {c.qualname}{where}")
            if kind is Kind.RESHAPED and c.before:
                # Windowed around the difference: urllib3's new `version_string`
                # sits 170 characters into a long signature, and a plain cut at
                # 150 showed two identical prefixes and not the change itself.
                was, now = around_difference(c.before, c.after, width=100)
                lines.append(f"      was: {was}")
                lines.append(f"      now: {now}")
            elif kind is Kind.RESHAPED:
                lines.append(f"      {c.detail[:150]}")
        if len(items) > 15:
            lines.append(f"    ... and {len(items) - 15} more (--out writes them all)")

    if report.used_by is not None:
        reaching = _by_severity(report.reaching_you)
        if reaching:
            lines.append(f"\n  {len(reaching)} change(s) your code references:")
            for c in reaching[:15]:
                lines.append(
                    f"    [{c.kind.value:8}] {c.qualname}  ({len(c.used_at)} site(s), "
                    f"e.g. {c.used_at[0]})"
                )
        maybe = _by_severity(report.maybe_reaching_you)
        if maybe:
            lines.append(
                f"\n  {len(maybe)} change(s) your code MAY reference - a method of that name is"
                " called\n  on an object whose type cannot be known without running it:"
            )
            for c in maybe[:10]:
                lines.append(
                    f"    [{c.kind.value:8}] {c.qualname}  ({len(c.maybe_at)} site(s), "
                    f"e.g. {c.maybe_at[0]})"
                )
        if not reaching and not maybe and report.changes:
            lines.append(
                f"\n  None of these changes is referenced by the {report.scanned_files}"
                f" Python file(s) read under {report.used_by}"
            )
    elif report.changes:
        lines.append("\n  Pass --used-by <your repo> to see which of these your code references.")

    if not report.changes:
        first = (
            "\n  Nothing changed in the public surface, and nothing behaved differently\n"
            "  on the inputs tried."
            if report.behaviour_checked and report.compared
            else "\n  Nothing changed in the public surface. No function could be exercised,\n"
            "  so behaviour is unknown."
            if report.behaviour_checked
            else "\n  Nothing changed in the public surface. Behaviour was not compared\n"
            "  (--no-behaviour)."
        )
        lines.append(
            first + " That is a statement about this comparison, not a guarantee:\n"
            "  private APIs, and anything not exercised, are not covered."
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
                "behaviour_checked": report.behaviour_checked,
                "compared": report.compared,
                "unreachable": report.unreachable,
                "reaching_you": len(report.reaching_you),
                "seconds": round(report.seconds, 1),
                "changes": [c.as_row() for c in report.sorted()],
                "weak_differences": [c.as_row() for c in report.weak],
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="",
    )


def _esc(s: object, limit: int = 70) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")[:limit]


def _used(c: Change) -> str:
    sites = [f"`{u}`" for u in c.used_at[:3]]
    if not sites and c.maybe_at:
        sites = [f"maybe `{u}`" for u in c.maybe_at[:3]]
    more = len(c.used_at or c.maybe_at) > 3
    return ", ".join(sites) + (" ..." if more else "")


def write_markdown(report: Report, path: Path) -> None:
    counts = report.counts()
    silent_n = f"**{counts.get('silent', 0)}**" if report.behaviour_checked else "not checked"
    out = [
        f"# `{report.package}` {report.old_version} → {report.new_version}",
        "",
        "| | count | who tells you |",
        "|---|---:|---|",
        f"| gone | {counts.get('gone', 0)} | an ImportError at startup |",
        f"| reshaped | {counts.get('reshaped', 0)} | a type checker |",
        f"| **silent** | {silent_n} | **nothing** |",
        f"| widened | {counts.get('widened', 0)} | not breaking - existing calls still work |",
        f"| added | {counts.get('added', 0)} | - |",
        "",
    ]
    if report.behaviour_checked:
        out += [
            f"Behaviour compared on {report.compared} function(s); {report.unreachable} "
            "could not be called in either version.",
            "",
        ]
    else:
        out += ["Behaviour was not compared (`--no-behaviour`).", ""]

    silent = _by_severity(report.of(Kind.SILENT))
    if silent:
        out += [
            "## Silent behaviour changes",
            "",
            "Same name, same signature, different result. No import fails and no type",
            "checker complains.",
            "",
            f"| function | input | {report.old_version} | {report.new_version} | you call it at |",
            "|---|---|---|---|---|",
        ]
        for c in silent:
            w = c.witness or {}
            old, new = around_difference(str(w.get("old")), str(w.get("new")), width=70)
            out.append(
                f"| `{c.qualname}` | `{_esc(w.get('args'))}` | `{_esc(old, 80)}` | "
                f"`{_esc(new, 80)}` | {_used(c)} |"
            )
        out.append("")

    if report.weak:
        out += [
            f"## Not counted: differences only on wrong-typed arguments ({len(report.weak)})",
            "",
            "One version raised TypeError or AttributeError on a generated argument no real",
            "caller would pass. Listed for completeness; not a silent change.",
            "",
            f"| function | input | {report.old_version} | {report.new_version} |",
            "|---|---|---|---|",
        ]
        for c in report.weak:
            w = c.witness or {}
            out.append(
                f"| `{c.qualname}` | `{_esc(w.get('args'))}` | `{_esc(w.get('old'), 80)}` | "
                f"`{_esc(w.get('new'), 80)}` |"
            )
        out.append("")

    sections = (
        (Kind.GONE, "Gone", "No longer exists. Importing or calling it fails."),
        (Kind.RESHAPED, "Reshaped", "The call shape changed in a way existing calls can trip on."),
        (Kind.WIDENED, "Widened", "The signature grew; every existing call still binds."),
        (Kind.ADDED, "Added", "New in the new version."),
    )
    for kind, title, blurb in sections:
        items = _by_severity(report.of(kind))
        if not items:
            continue
        out += [f"## {title} ({len(items)})", "", blurb, ""]
        if kind in (Kind.RESHAPED, Kind.WIDENED):
            out += ["| symbol | before -> after | you use it at |", "|---|---|---|"]
            out += [f"| `{c.qualname}` | `{_esc(c.detail, 300)}` | {_used(c)} |" for c in items]
        else:
            out += ["| symbol | you use it at |", "|---|---|"]
            out += [f"| `{c.qualname}` | {_used(c)} |" for c in items]
        out.append("")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8", newline="")
