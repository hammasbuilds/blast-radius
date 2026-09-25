"""Which of your files actually use the changed symbol.

This is the part of the report that decides what gets read first: a change the
project references is sorted above one it does not. So a wrong answer here is not
a cosmetic problem - it promotes the wrong change to the top of the page.

Matching used to be on the trailing attribute name, which the README described as
deliberate over-reporting: "a project with its own `parse` is credited with using
`packaging.version.parse`. That is the right direction to err." It is the right
direction only while there is no better option, and there is one - a symbol has to
be imported before it can be called, and the imports are right there in the file.
"""

from __future__ import annotations

from blast_radius.diff import find_call_sites
from blast_radius.types import Change, Kind


def test_a_project_with_its_own_parse_is_not_credited_with_using_the_package(tmp_path):
    """The over-reporting case, now excluded.

    `mine.py` imports packaging and also defines its own `parse`. Matching on the
    trailing name credits the call to `parse('1.0')` as a use of
    `packaging.version.parse`, which it plainly is not.
    """
    (tmp_path / "mine.py").write_text(
        "import packaging\n"
        "\n"
        "\n"
        "def parse(text):\n"
        "    return text\n"
        "\n"
        "\n"
        "def go():\n"
        "    return parse('1.0')\n",
        encoding="utf-8",
    )
    changes = [Change(Kind.GONE, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)

    assert changes[0].used_at == [], "a project's own parse() is not packaging's"


def test_every_way_the_symbol_can_arrive_is_resolved(tmp_path):
    """Resolution has to be complete, or precision is bought with under-reporting -
    and a missed call site is a break that reaches production, which is the whole
    reason the old behaviour erred the other way."""
    forms = {
        "direct.py": "from packaging.version import parse\n\n\ndef go():\n    return parse('1')\n",
        "aliased.py": "from packaging.version import parse as p\n\n\ndef go():\n    return p('1')\n",
        "module.py": "from packaging import version\n\n\ndef go():\n    return version.parse('1')\n",
        "full.py": "import packaging.version\n\n\ndef go():\n    return packaging.version.parse('1')\n",
        "modalias.py": "import packaging.version as v\n\n\ndef go():\n    return v.parse('1')\n",
    }
    for name, body in forms.items():
        (tmp_path / name).write_text(body, encoding="utf-8")

    changes = [Change(Kind.GONE, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)

    found = {site.split(":")[0] for site in changes[0].used_at}
    assert found == set(forms), f"missed {sorted(set(forms) - found)}"


def test_a_file_that_only_names_the_package_in_a_string_is_not_a_call_site(tmp_path):
    """The old pass required only that the package name appear anywhere in the file,
    so a help string mentioning it was enough to start matching attribute names."""
    (tmp_path / "doc.py").write_text(
        'HELP = "run pip install packaging first"\n\n\ndef parse(x):\n    return x\n',
        encoding="utf-8",
    )
    changes = [Change(Kind.GONE, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)

    assert changes[0].used_at == []


def test_a_relative_import_cannot_reach_a_third_party_package(tmp_path):
    """`from .version import parse` is the project's own version module, whatever it
    is called. Treating the module name as absolute would credit every project with
    a `version.py` of its own."""
    pkg = tmp_path / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "version.py").write_text("def parse(x):\n    return x\n", encoding="utf-8")
    (pkg / "app.py").write_text(
        "import packaging\n"
        "\n"
        "from .version import parse\n"
        "\n"
        "\n"
        "def go():\n"
        "    return parse('1')\n",
        encoding="utf-8",
    )
    changes = [Change(Kind.GONE, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)

    assert changes[0].used_at == []


def test_a_star_import_still_finds_the_module(tmp_path):
    """`from packaging.version import *` binds names this pass cannot enumerate. The
    module itself is recorded so the file is not dropped entirely, which would be
    the under-reporting failure."""
    (tmp_path / "star.py").write_text(
        "from packaging.version import *\n\n\ndef go():\n    return parse('1')\n",
        encoding="utf-8",
    )
    changes = [Change(Kind.GONE, "packaging.version")]
    find_call_sites(tmp_path, "packaging", changes)

    assert changes[0].used_at, "the module binding should survive a star import"
