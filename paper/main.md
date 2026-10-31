---
title: "Silent Behavioural Change in Python Dependency Upgrades: A 2,997-Pair Census"
author: "Muhammad Hammas, Independent Researcher"
date: 2026-10-31
---

# Abstract

Semantic versioning asks a maintainer to promise that a patch release changes nothing a
caller can observe. Existing studies test that promise by **diffing APIs** — comparing the
names and signatures of two releases — and report violation rates for Maven, npm and
multi-ecosystem corpora. A static diff cannot see a function that keeps its name, keeps its
signature, and returns something different.

We install both versions of a package, import each in an isolated subprocess, and execute
every function whose call shape survived the upgrade on identical generated inputs. Over
**2,997 measured release pairs from 1,204 distinct PyPI packages**, 15.6% broke an exported
name. Patch releases broke one in forty (2.5%, 95% CI [1.6%, 3.8%]); major releases broke
two in five (41.5% [37.4%, 45.7%]).

Three results qualify those rates rather than decorate them. **Breakage rises monotonically
with the size of the exported surface**, from 4.1% [3.0%, 5.6%] for packages exporting ten
names or fewer to 40.5% [32.0%, 49.6%] for packages exporting over a thousand — so a
cohort rate is partly a measurement of API size. **Four of fifteen pairwise cohort
comparisons do not survive multiplicity correction**, so six published percentages are not
six distinguishable populations. And **broken symbols are extremely concentrated** (Gini
0.887; the top 10% of breaking pairs own 84.7% of all broken symbols), so symbol-level
rates are properties of a few packages.

Against `griffe`, a static API differ, the executing approach found four removed public
constants `griffe` reported and our own tool did not — a defect the comparison exposed and
we fixed — while `griffe` cannot express the behavioural category at all.

# 1. Introduction

A dependency bump arrives as a version number and a changelog. Neither is evidence.

The literature on whether version numbers predict breakage is substantial and almost
entirely static. Replication work on Maven Central finds breaking changes widespread
without regard for semantic versioning, while also finding compliance improving over
time [@maven-replication]. A study of 384 Maven artifacts reports 67% introducing at least
one semantic-versioning violation. A multi-ecosystem study spanning Maven, PyPI and
RubyGems detects breaking changes by diffing release code and reports API-breaking changes
in 26% of provider packages. On npm, clients recover from breaking changes in about half
of cases, usually by changing the pinned version rather than their code [@npm-breaking]. A
2026 systematic review treats persistent semantic-versioning violation as an open
problem [@slr].

Every one of those detects breakage by comparing declarations. Our contribution is the
category that method cannot reach, measured at scale on the ecosystem least covered by it.

**Contributions.**

1. A behavioural-difference method for library upgrades, with a sandbox, a determinism
   check, and an explicit refusal to report a difference it cannot reproduce (§2).
2. A census of 2,997 release pairs across 1,204 PyPI packages, with per-cohort rates,
   Wilson intervals and pairwise significance under Holm correction (§3).
3. Three qualifications of those rates — a monotonic surface-size confound, four
   non-separated cohort pairs, and extreme concentration of symbol-level damage (§4).
4. A comparison against `griffe` that located a real defect in our own tool (§5).

# 2. Method

**Both versions, side by side.** Two versions of one package cannot coexist in an
interpreter: `sys.modules` is keyed by name, so the second import wins and every later
comparison is one version against itself. Each version is installed into its own directory
and probed in its own subprocess, and the probe proves which file it loaded before
reporting anything — a `sys.path` entry that does not exist is silently ignored by Python,
which previously made a known-breaking upgrade report "nothing changed".

**Four kinds of change,** ordered by how quietly each reaches production: `gone` (an
`ImportError` at startup), `reshaped` (a type checker catches it), `widened` (no existing
call can trip on it; never counted as breaking), and `SILENT` — same name, same call shape,
different answer.

**Call shape, not signature text.** Two signatures are the same when their parameter names,
kinds and defaults match. Annotations are excluded deliberately: adding a type hint cannot
break a caller. Comparing signature *text* instead reported 795 breaking changes for one
upgrade that added type hints, and starved the behavioural pass of candidates.

**What the behavioural pass refuses to claim.** A difference is reported only if it
reproduces when re-run; a function that disagrees with itself is recorded as
non-deterministic, not silent. Differences that occur only where one version rejects a
generated argument as the wrong type are reported separately and never counted. Both
reasons are published per finding.

**Constants.** A removed module-level constant is an `ImportError` at startup exactly as a
removed function is. We read a name as a constant when its value is immutable and
printable, it is in `__all__` or spelled in capitals, and it is assigned in that module's
own file — the last condition keeps re-exported third-party names out of a package's
surface. Only the name is compared; a changed value is a behavioural change with no
witness.

# 3. The census

Pairs are the most recent consecutive release pair of each bump class per package,
published since 2021-01-01, from 1,389 package names on PyPI's top-downloads list. **3,777
pairs were walked.** 20 are dropped because PyPI's upload order placed the older version
second; 52 because a sweep resumed over several days measured a second pair for a package
already represented in its cohort, leaving 3,705 rows. Of those, 768 could not be
measured — no importable module, a failed install, or a surface walk that did not
finish. **2,937 pairs were measured.**

| Bump | Breaking / measured | Rate | Wilson 95% |
|---|---:|---:|---|
| patch | 20 / 808 | 0.025 | [0.016, 0.038] |
| minor | 114 / 830 | 0.137 | [0.116, 0.163] |
| major | 221 / 532 | 0.415 | [0.374, 0.457] |
| 0.x-patch | 29 / 372 | 0.078 | [0.055, 0.110] |
| 0.x-breaking | 65 / 346 | 0.188 | [0.150, 0.233] |
| 0.0.z | 8 / 49 | 0.163 | [0.085, 0.292] |

The ordinal claim holds strongly: patch vs minor $p = 2.1\times10^{-17}$, minor vs major
$p = 6.3\times10^{-31}$ (Fisher exact, Holm-corrected over fifteen comparisons).

# 4. Three qualifications

**4.1 Surface size is a confound, and it is monotonic.**

| Exported names | Breaking / measured | Rate | Wilson 95% |
|---|---:|---:|---|
| 0–10 | 36 / 880 | 0.041 | [0.030, 0.056] |
| 10–30 | 85 / 615 | 0.138 | [0.113, 0.168] |
| 30–100 | 115 / 613 | 0.188 | [0.159, 0.220] |
| 100–300 | 92 / 414 | 0.222 | [0.185, 0.265] |
| 300–1000 | 82 / 299 | 0.274 | [0.227, 0.327] |
| 1000+ | 47 / 116 | 0.405 | [0.320, 0.496] |

A tenfold spread with non-overlapping intervals end to end. A package exporting a thousand
names has more opportunities to lose one, so any cohort comparison is partly a comparison
of API sizes. Median surface is 28 names; p95 is 849; the largest is 22,713.

**4.2 Four of fifteen cohort pairs are not separated.** After Holm correction, `minor vs
0.x-breaking`, `0.x-patch vs 0.0.z`, `minor vs 0.0.z` and `0.x-breaking vs 0.0.z` are not
distinguishable at $\alpha = 0.05$. Every one of them involves a cohort with few measured
pairs. Printing six percentages side by side implies six populations; there are not six.

**4.3 Symbol-level damage is owned by a handful of pairs.** Gini 0.887 over 457 breaking
pairs; the top 10% account for 84.7% of all broken symbols. Per cohort the largest single
pair owns 63.8% (`0.x-breaking`), 51.5% (`0.x-patch`) and 49.2% (`patch`) of that cohort's
symbols. The distribution of damage is heavy-tailed: the median breaking pair loses a
handful of names and the largest loses thousands, so a mean describes no pair in it. **We
therefore report pair-level rates as the headline and never a symbol-level rate without its
largest contributor named.**

# 5. Comparison with a static differ

`griffe` and this tool were run over the same two installed directories for `click`
7.1.2→8.1.7 and `urllib3` 2.2.1→2.2.2. `griffe` reported 104 and 13 findings; this tool
reported 16 gone / 11 reshaped / 9 widened / 4 silent and 2 reshaped / 1 silent / 1 added.
The counts are not comparable: 40 of `griffe`'s click findings are positional parameters
whose index moved, and 15 are the word `object` dropped from a Python-2-era base list — all
15 verified against click 7.1.2's source.

Two findings matter.

**`griffe` found four removed public constants that this tool could not see.**
`click.core.SUBCOMMAND_METAVAR` and three others are strings; our surface walk described a
name only when it was a routine or a class. `from click.core import SUBCOMMAND_METAVAR` is
an `ImportError` under click 8, and it appeared in no report this tool had ever produced.
Nothing in our own test suite could have found it: a tool that reads only callables passes
every test about callables. Fixed; `gone` moved 12→16.

**The behavioural category is outside what a static differ can express.** In the patch
release `urllib3` 2.2.1→2.2.2, `HTTPResponse.read` keeps its name and signature and returns
`None` where it previously raised `RuntimeError` on one of twelve exercised inputs.
`griffe` never imports the package and so cannot call anything.

# 6. Threats to validity

**Surface size confounds every cohort rate** (§4.1), and we do not adjust for it: we report
it. An adjusted rate would need a model of how surface size and release discipline relate,
which this design cannot identify.

**The denominator is chosen by what could be measured.** 768 of 3,705 rows produced no
comparison, and the reasons are not independent of the packages — a stub-only distribution
differs from one whose surface walk exceeded its budget. Rates are over measured pairs and
the exclusions are published with their reasons.

**The census spans revisions of this tool.** It was resumed over several days and 3,698 of
3,705 rows do not record which revision measured them. One row reports
`BedrockAgentEvent.decoded_body` removed, and `decoded_body` is a `@property` — which the
current walk cannot report at all, demonstrating that part of the file was produced by code
no longer in the tree. To bound this, 60 pairs were re-measured at a single revision (10
per cohort, fixed seed): **0 verdict changes, 0 exported-symbol changes, 2 rows where only
internal symbols moved.** The re-check's sensitivity was established by planting false
removals in sampled rows — six of six caught.

**Generated inputs are not real call sites.** The behavioural pass builds arguments from
the old signature; a function whose difference only appears on inputs the generator never
produces is missed. This bounds recall, not precision: every reported difference carries a
witness.

**Single machine, single interpreter, Windows.** Three of the eight flakiness-adjacent
arms available elsewhere do not run here, and one package (ansible, 6,846 modules) could
not be walked within any budget tried.

> **On the numbers in §4.** An earlier draft quoted these from a research module that
> loaded the per-pair ledger without the two filters the published summary applies —
> dropping pairs whose `new` version predates its `old`, and keeping one pair per package
> per cohort. It described 2,997 pairs where the rest of the paper describes 2,937, with
> the patch cohort at 23/830 rather than 20/808. The module now imports both filters from
> the summariser rather than reproducing them, and every figure in §4 was regenerated.

# 7. Reproducibility

`docs/census-rows.json.gz` (115 KB) holds one row per pair with counts and reasons and no
symbol names. `python scripts/census_summary.py --census docs/census-rows.json.gz`
reproduces the published summary **byte-identically**, which is checked by a test, so every
figure and rate in this paper rebuilds from the repository without the 4.3 MB raw artefact.
The comparison against `griffe` rebuilds via `scripts/griffe_comparison.py`, which installs
both versions and both tools.

# References

- [@maven-replication] *Breaking changes and semantic versioning in Maven Central: a replication study.* Journal of Systems and Software, 2023.
- [@npm-breaking] *I Depended on You and You Broke Me: An Empirical Study of Manifesting Breaking Changes in Client Packages.* arXiv:2301.04563.
- [@slr] *Breaking Changes in Software Ecosystems: A Systematic Literature Review.* arXiv:2605.24397.
- *griffe* — API differ, mkdocstrings. <https://mkdocstrings.github.io/griffe/>

> **Citation status.** The three references above were located by a literature search and
> their claims are quoted from that search's summaries, not from the full texts. Before
> submission each must be read and verified, and the search found **no PyPI-specific
> semantic-versioning compliance study** — the novelty claim in §1 rests on that absence
> and needs one further confirmatory pass.
