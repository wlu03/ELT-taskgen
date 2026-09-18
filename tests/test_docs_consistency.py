"""Documentation and packaging consistency — the drift class, pinned.

WHY THIS EXISTS.  Prose drifts and code does not, so every number a document
asserts is a claim that rots silently.  This repository has already paid for
that twice: `pyproject.toml` pointed `readme` at a file that had been deleted
(setuptools warned and shipped an empty long description), and `make demo` plus
two document sections went on describing a `demo` subcommand for days after the
CLI stopped having one.  Nothing here executes the pipeline, opens a workspace,
touches `runs/`, or spends a cent — every assertion is a string in a file
compared against the single source of truth in the code:

  * `engine.STAGE_ORDER`          the ladder and its length
  * `gates.GATE_NAMES` /
    `gates.VARIANT_GATE_NAMES`    the gate rosters and their lengths
  * `cli.build_parser()`          the subcommand vocabulary
  * `workspace.DEFAULT_WORKSPACE` the scratch workspace `make clean` may delete
  * `engine._SCHEMA`              the ledger DDL INTERFACES.md transcribes

The rule this file enforces is not "the docs must be right about everything" —
it is "a document may not assert a number, a command name, or a file path that
the code disagrees with, and may not offer a command that cannot run".  Prose
that says "the code is the authority" is fine; prose that quotes a stale count,
cites a module that no longer exists, or introduces a block as working "without
`uv`" and then runs `uv` in it, is a test failure.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from elt_taskgen import cli
from elt_taskgen.engine import STAGE_ORDER
from elt_taskgen.models import TaskVariant
from elt_taskgen.verification import gates
from elt_taskgen.workspace import DEFAULT_WORKSPACE

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The documents that describe the system AS IT IS.  Historical snapshots
#: (docs/README_full_20260814.md, docs/runs/*.md) are deliberately excluded:
#: they carry a HISTORICAL banner and are kept verbatim as a record, so holding
#: them to today's vocabulary would force us to rewrite evidence.
CURRENT_DOCS: tuple[str, ...] = (
    "README.md",
    "Makefile",
    "docs/INTERFACES.md",
    "docs/SOURCES.md",
    "docs/TWO_STAGE_RLVR_CONTRACT.md",
    "docs/plans/endtoend_runbook.md",
    "docs/plans/tinker_elt_rl_environment.md",
    # The Phase 0 change log of the bounded-agents roadmap: it names
    # subcommands, modules and tests as they are, so it is held to today's
    # vocabulary like every other current document.
    "docs/plans/bounded_agents_phase0.md",
    # The Phase 1 change log (the runner, the author session, the bounded
    # proposer, the review remediation): held to today's vocabulary the same way.
    "docs/plans/bounded_agents_phase1.md",
    # The Phase 2 change log (the OQ-23 option C decision, the training
    # signal and the declarative environment, the witness session blocks):
    # held to today's vocabulary the same way.
    "docs/plans/bounded_agents_phase2.md",
    # The Phase 3 change log (the POPULATION route and the `attack` member of
    # `certify`, the POP/SHC critic validators, the correction channel):
    # held to today's vocabulary the same way.
    "docs/plans/bounded_agents_phase3.md",
    # The Phase 4 change log (the harness-6 metrology protocol, the fixture
    # families, the trial seam, the manifest relaxation): it names subcommands,
    # config keys and the execution model, so it is held to today's vocabulary
    # like every other current document.
    "docs/plans/bounded_agents_phase4.md",
    # The Phase 5 change log (the sandbox attestation and its fail-closed
    # preflight, the release attestation gate, the parity battery, the
    # model-facing/replay copy split) and, above all, its hand-off list: it
    # names fenced files, functions and pins the owner still has to land, so it
    # is held to today's vocabulary like every other current document.
    "docs/plans/bounded_agents_phase5.md",
    "docs/difficulty/README.md",
    # The bounded-agent pilot pre-registration files (roadmap 0.F): the
    # directory README and every PILOT-<name>.md are held to today's
    # vocabulary, so a pilot cannot promise a subcommand or an emulator.
    *sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "docs" / "experiments").glob("*.md")
    ),
)

#: Documents that MUST carry a historical banner, because they describe a world
#: with a `demo` subcommand in it, or a pool whose recorded verdict ("rejected")
#: no longer holds.  They are evidence and are kept verbatim; the banner is what
#: stops a reader mistaking evidence for a current description.
HISTORICAL_DOCS: tuple[str, ...] = (
    "docs/README_full_20260814.md",
    *sorted(str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "docs" / "runs").glob("*.md")),
)

#: Docs checked only for execution-status vocabulary may name future commands;
#: implementation reviews and historical evidence remain exempt.
VOCABULARY_DOCS: tuple[str, ...] = CURRENT_DOCS + (
    "docs/EXECUTION_MODEL.md",
    "docs/WAREHOUSE_CONNECTORS.md",
    "docs/plans/duckdb_rlvr_cloud_runtime_migration.md",
    # The cloud-free agent RLVR plan (roadmap Phase 2 Docs row): it names the
    # future `train` verb and the deferred 2.c work, so it is held to the
    # execution-status vocabulary only, not to the subcommand roster.
    "docs/plans/cloud_free_elt_agent_rlvr.md",
)

#: The published documents: every document the checks above read or the README
#: links, and the documents and scripts those cite. `docs/` is otherwise
#: private, and `.gitignore` publishes exactly this list. The glob-built sets
#: above only check the files that are present, so a checkout missing one of
#: these would check fewer documents and still pass without the presence test.
REQUIRED_DOCS: tuple[str, ...] = (
    "docs/CONFIGURABLE_PIPELINE.md",
    "docs/EXECUTION_MODEL.md",
    "docs/INTERFACES.md",
    "docs/README_full_20260814.md",
    "docs/SOURCES.md",
    "docs/TWO_STAGE_RLVR_CONTRACT.md",
    "docs/WAREHOUSE_CONNECTORS.md",
    "docs/difficulty/README.md",
    "docs/difficulty/scripts/measure_all.py",
    "docs/difficulty/scripts/measure_discriminating_power.py",
    "docs/experiments/PILOT-P2.md",
    "docs/experiments/PILOT-P3.md",
    "docs/experiments/PILOT-P4.md",
    "docs/experiments/PILOT-P5.md",
    "docs/experiments/README.md",
    "docs/plans/bounded_agents_phase0.md",
    "docs/plans/bounded_agents_phase1.md",
    "docs/plans/bounded_agents_phase2.md",
    "docs/plans/bounded_agents_phase3.md",
    "docs/plans/bounded_agents_phase4.md",
    "docs/plans/bounded_agents_phase5.md",
    "docs/plans/cloud_free_elt_agent_rlvr.md",
    "docs/plans/duckdb_rlvr_cloud_runtime_migration.md",
    "docs/plans/endtoend_runbook.md",
    "docs/plans/tinker_elt_rl_environment.md",
    "docs/runs/demo.md",
)

#: Ban emulator, replacement, and vendor-runtime equivalence claims. Ordinary
#: CLI and upstream compatibility wording remains allowed.
_EMULATOR_CLAIM = re.compile(
    r"emulat(?:e[sd]?|ing|ion|ors?)"
    r"|drop-?in\s+replacement"
    r"|duckdb[- ]compatible"
    r"|local\s+(?:snowflake|databricks|redshift)\b",
    re.IGNORECASE,
)

#: A 3-line window around a match must contain one of these for the phrase to
#: count as denied rather than claimed.  A tripwire, not a parser — which is
#: why the docs keep each denial and its claim word on one physical line.
_CLAIM_DENIAL = re.compile(r"\b(?:no|not|never|nor|cannot|without)\b", re.IGNORECASE)

_CANONICAL_LABEL = "semantic proxy (DuckDB) plus real-runtime adapters"

#: `elt-taskgen <token>` / `$ET <token>` / `python -m elt_taskgen.cli <token>`.
#: The trailing lookahead keeps shell placeholders and brace expansions out:
#: `ingest-<pool>` and `ingest-{dbt,dlt}` are patterns, not subcommand names.
_INVOCATION = re.compile(
    r"(?:elt-taskgen|elt_taskgen\.cli|\$ET)\s+([a-z][a-z-]*)(?![-{<a-z])"
)

_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", re.DOTALL
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text()


def _subcommands() -> frozenset[str]:
    parser = cli.build_parser()
    for action in parser._subparsers._group_actions:  # noqa: SLF001 - argparse has no public API
        if getattr(action, "choices", None):
            return frozenset(action.choices)
    raise AssertionError("cli.build_parser() exposes no subparsers")


#: `find . -name __pycache__ -type d -exec rm -rf {} +` deletes whatever the find
#: expression matched, not a path the recipe names, so `{}` is exempt from the
#: `clean` allowlist below.
_FIND_PLACEHOLDER = "{}"


def _rm_arguments(recipe: str) -> set[str]:
    """Every path an `rm` in this make recipe would delete.

    Deliberately literal: it walks each `rm` word in the recipe and takes the
    non-flag tokens that follow it, stopping at a shell separator so a second
    command on the same line is scanned as its own `rm` rather than being read
    as more arguments to the first.  That is what makes `rm -rf runs/default;
    rm -rf runs` visible as two deletions instead of one.
    """
    out: set[str] = set()
    for raw_line in recipe.splitlines():
        line = raw_line.split("#", 1)[0]
        for match in re.finditer(r"\brm\b", line):
            for word in line[match.end():].split():
                token, separated = word, False
                for sep in (";", "&&", "||", "|", "&"):
                    if sep in token:
                        token, separated = token.split(sep, 1)[0], True
                        break
                if token in ("+", "\\", ""):
                    break
                if not token.startswith("-"):
                    out.add(token)
                if separated:
                    break
    return out


#: Tools a document may claim the reader does not have.  A claim of absence is
#: a promise that the commands offered alongside it run without that tool.
_TOOLS = ("uv", "pip", "docker", "make", "brew")

_TOOL_ABSENCE = re.compile(
    r"without\s+`?(" + "|".join(_TOOLS) + r")`?\b"
    r"|`?(" + "|".join(_TOOLS) + r")`?\s+is\s+(?:unavailable|not\s+(?:available|installed))"
    r"|(?:do|does)\s+not\s+have\s+`?(" + "|".join(_TOOLS) + r")`?\b",
    re.IGNORECASE,
)


def _absent_tools(prose: str) -> set[str]:
    return {
        group.lower()
        for match in _TOOL_ABSENCE.finditer(prose)
        for group in match.groups()
        if group
    }


def _invoked_commands(block: str) -> set[str]:
    """Every program a shell block runs, by name.

    Command position plus two spellings that hide one: an absolute or venv path
    (`.venv/bin/pip` is still pip) and `python -m <module>`.
    """
    out: set[str] = set()
    for raw_line in block.splitlines():
        line = raw_line.strip().lstrip("$").strip()
        if not line or line.startswith("#"):
            continue
        for segment in re.split(r"&&|\|\||[|;]", line):
            words = segment.split()
            while words and "=" in words[0] and not words[0].startswith("-"):
                words = words[1:]  # VAR=value prefixes
            if not words:
                continue
            out.add(words[0])
            out.add(words[0].rsplit("/", 1)[-1])
            if "-m" in words[1:]:
                index = words.index("-m", 1)
                if index + 1 < len(words):
                    out.add(words[index + 1])
    return out


def _prose_and_block_pairs(text: str) -> list[tuple[str, str]]:
    """(the prose just above a fenced shell block, the block's commands).

    Enough context to judge a claim ("without `uv` …") against the commands it
    introduces, without pretending to parse Markdown.
    """
    pairs: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].startswith("```"):
            start = index + 1
            end = start
            while end < len(lines) and not lines[end].startswith("```"):
                end += 1
            prose = "\n".join(lines[max(0, index - 8):index])
            pairs.append((prose, "\n".join(lines[start:end])))
            index = end + 1
            continue
        index += 1
    return pairs


def _makefile_comment_and_recipe_pairs(text: str) -> list[tuple[str, str]]:
    """(the comment block above a target, that target's recipe lines)."""
    pairs: list[tuple[str, str]] = []
    comment: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#"):
            comment.append(line.lstrip("# "))
            continue
        if re.match(r"^[A-Za-z0-9_.-]+:", line) and not line.startswith(".PHONY"):
            recipe = []
            cursor = index + 1
            while cursor < len(lines) and lines[cursor].startswith("\t"):
                recipe.append(lines[cursor].lstrip("\t"))
                cursor += 1
            pairs.append((" ".join(comment), "\n".join(recipe)))
        comment = []
    return pairs


def _sql_columns(ddl: str) -> dict[str, list[str]]:
    """table -> column names, from a CREATE TABLE block, comments stripped."""
    out: dict[str, list[str]] = {}
    for table, body in _CREATE_TABLE.findall(ddl):
        no_comments = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
        columns = []
        for part in no_comments.split(","):
            tokens = part.split()
            if tokens:
                columns.append(tokens[0])
        out[table] = columns
    return out


class TestPackaging(unittest.TestCase):
    def test_pyproject_readme_exists(self) -> None:
        """`readme` must name a file that is actually in the tree.

        The dangling `readme = "FIX_PIPELINE.md"` did not break the wheel — it
        just made the published long description empty, which is exactly the
        kind of failure nobody notices without a test.
        """
        data = tomllib.loads(_read("pyproject.toml"))
        readme = data["project"]["readme"]
        self.assertIsInstance(readme, str, "table-form readme not expected here")
        self.assertTrue(
            (REPO_ROOT / readme).is_file(),
            f"pyproject readme={readme!r} does not exist",
        )

    def test_runtime_dependencies_are_bounded(self) -> None:
        """Unbounded deps make the determinism claims unfalsifiable.

        Every reproducibility claim in this repo is a SAME-ENVIRONMENT claim, so
        the environment has to be nameable.  `uv.lock` is the pin; the bounds
        here are the ranges the pipeline was empirically validated across.
        """
        data = tomllib.loads(_read("pyproject.toml"))
        deps = {
            re.split(r"[<>=!~ ]", spec, maxsplit=1)[0]: spec
            for spec in data["project"]["dependencies"]
        }
        self.assertEqual(
            set(deps),
            {"pydantic", "duckdb", "pyyaml", "sqlglot", "python-hcl2", "lark"},
            deps,
        )
        exact = {
            "python-hcl2": "python-hcl2==7.3.1",
            "lark": "lark==1.3.1",
        }
        for name, spec in deps.items():
            if name in exact:
                self.assertEqual(spec, exact[name])
                continue
            self.assertIn("<", spec, f"{name} has no upper bound: {spec!r}")
            self.assertIn(">=", spec, f"{name} has no lower bound: {spec!r}")

    def test_build_backend_and_dev_tooling_share_the_exact_setuptools_pin(
        self,
    ) -> None:
        """Frozen development syncs must be able to import package build hooks."""
        data = tomllib.loads(_read("pyproject.toml"))
        pin = "setuptools==84.0.0"
        self.assertEqual(data["build-system"]["requires"], [pin])
        self.assertEqual(data["dependency-groups"]["dev"], [pin])

    def test_project_version_matches_import_and_lock(self) -> None:
        """One release version must describe source imports and the frozen lock."""
        import elt_taskgen

        project = tomllib.loads(_read("pyproject.toml"))["project"]
        self.assertEqual(project["version"], elt_taskgen.__version__)

        lock = tomllib.loads(_read("uv.lock"))
        locked_project = next(
            package
            for package in lock["package"]
            if package.get("name") == project["name"]
            and package.get("source") == {"editable": "."}
        )
        self.assertEqual(locked_project["version"], project["version"])

    def test_no_document_links_to_a_missing_fix_pipeline(self) -> None:
        """FIX_PIPELINE.md is gone; nothing current may cite it as existing."""
        self.assertFalse(
            (REPO_ROOT / "FIX_PIPELINE.md").exists(),
            "FIX_PIPELINE.md is back — either delete this test or the citations",
        )
        for rel in CURRENT_DOCS:
            self.assertNotIn(
                "FIX_PIPELINE",
                _read(rel),
                f"{rel} cites FIX_PIPELINE.md, which does not exist",
            )

    def test_an_install_path_never_needs_the_tool_it_says_you_lack(self) -> None:
        """"Without `uv`, run `uv export …`" is not an install path.

        This is the packaging half of the drift class: a command block whose
        introduction promises it works without a tool, and whose very first
        line invokes that tool.  A reader who genuinely lacks it is stuck, and
        nothing computable disagrees — so compute it: read the prose above each
        shell block (and above each make recipe), and if it claims a tool is
        absent, that tool may not appear at a command position below.
        """
        blocks: list[tuple[str, str, str]] = [
            ("README.md", prose, block)
            for prose, block in _prose_and_block_pairs(_read("README.md"))
        ]
        blocks += [
            ("Makefile", comment, recipe)
            for comment, recipe in _makefile_comment_and_recipe_pairs(_read("Makefile"))
        ]
        for rel, prose, block in blocks:
            absent = _absent_tools(prose)
            if not absent:
                continue
            invoked = _invoked_commands(block)
            for tool in sorted(absent & invoked):
                self.fail(
                    f"{rel}: a block introduced as working without {tool!r} runs "
                    f"{tool!r}:\n{prose.strip()[-200:]}\n---\n{block.strip()}"
                )

    def test_package_docstring_names_a_real_document(self) -> None:
        import elt_taskgen

        doc = elt_taskgen.__doc__ or ""
        self.assertNotIn("FIX_PIPELINE", doc)
        self.assertIn("docs/INTERFACES.md", doc)


class TestSubcommandVocabulary(unittest.TestCase):
    def test_demo_subcommand_is_gone(self) -> None:
        self.assertNotIn("demo", _subcommands())

    def test_docs_only_name_real_subcommands(self) -> None:
        """Every `elt-taskgen <x>` in a current document must be runnable.

        Lines that explicitly demonstrate a REFUSAL (`invalid choice`) are
        skipped: the runbook's "what you might type" table names `author` on
        purpose, to say it does not exist.

        The set of known names is exactly `cli.build_parser()`'s — there is no
        allowance for subcommands a document promises before the parser has
        them, because that allowance is the hole this test exists to close.
        """
        known = _subcommands()
        for rel in CURRENT_DOCS:
            for lineno, line in enumerate(_read(rel).splitlines(), 1):
                if "invalid choice" in line:
                    continue
                for token in _INVOCATION.findall(line):
                    self.assertIn(
                        token,
                        known,
                        f"{rel}:{lineno} invokes unknown subcommand {token!r}",
                    )

    def test_current_docs_never_advertise_demo(self) -> None:
        for rel in CURRENT_DOCS:
            for lineno, line in enumerate(_read(rel).splitlines(), 1):
                for token in _INVOCATION.findall(line):
                    self.assertNotEqual(
                        token,
                        "demo",
                        f"{rel}:{lineno} still advertises the removed `demo` subcommand",
                    )

    def test_historical_docs_are_labelled_historical(self) -> None:
        """They may keep saying `demo` — but they must say they are old."""
        for rel in HISTORICAL_DOCS:
            head = "\n".join(_read(rel).splitlines()[:12])
            self.assertIn("HISTORICAL", head, f"{rel} lacks its historical banner")


class TestLegacyReleaseDocumentation(unittest.TestCase):
    """The five schema-2 drives are evidence, not current release claims."""

    def test_readme_scopes_the_five_legacy_drives(self) -> None:
        text = " ".join(_read("README.md").split())
        for required in (
            "they are not the current corpus",
            "schema 2.0",
            "`duckdb-census/1`",
            "Current code computes census version 2",
            "fails closed on their warehouse census records",
            "do not establish fresh upstream-source provenance",
            "Airbyte/warehouse/dbt runtime certification",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_source_run_snapshots_are_never_labelled_current(self) -> None:
        text = _read("docs/SOURCES.md")
        self.assertNotIn("Status today (measured", text)
        self.assertNotIn("all five real source pools now reach", text)
        for report in ("fivetran", "synsql", "schemapile", "wikidbs"):
            with self.subTest(report=report):
                self.assertRegex(
                    text,
                    rf"Historical run snapshot \(\[`docs/runs/{report}\.md`\]"
                    rf"\(runs/{report}\.md\);(?: not\n> |\n> not )current\)",
                )

    def test_release_attestation_is_not_documented_as_runtime_certification(
        self,
    ) -> None:
        text = " ".join(_read("docs/SOURCES.md").split())
        for required in (
            "five historical releases",
            "`schema_version 2.0`",
            "`duckdb-census/1`",
            "carry no pinned live-runtime record",
            "neither current bundle verification",
            "That label covers release attestation, not destination execution",
            "a separate pinned live certification record",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)


class TestMakefile(unittest.TestCase):
    def test_no_demo_target(self) -> None:
        text = _read("Makefile")
        self.assertNotRegex(text, r"(?m)^demo:")
        phony = re.search(r"(?m)^\.PHONY:(.*)$", text)
        assert phony is not None
        self.assertNotIn("demo", phony.group(1).split())

    def test_every_cli_target_is_live(self) -> None:
        known = _subcommands()
        for token in re.findall(r"elt_taskgen\.cli\s+([a-z][a-z-]*)", _read("Makefile")):
            self.assertIn(token, known, f"Makefile runs unknown subcommand {token!r}")

    def test_clean_removes_only_default_workspace_and_generated_debris(self) -> None:
        """`make clean` must target owned scratch/cache/build outputs only.

        It used to remove `taskgen-workspace`, a name the CLI stopped using; a
        clean that deletes nothing is worse than no clean, and a clean that
        deletes a kept drive is unrecoverable.  Blacklisting the five drive
        names is too weak to say that: `rm -rf runs/default runs/*` and
        `rm -rf runs/default; rm -rf runs` both name no drive and both destroy
        every drive.  So this reads the arguments of every `rm` in the recipe
        and holds them to an exact allowlist.
        """
        clean = re.search(r"(?ms)^clean:\n(.*?)(?:\n\S|\Z)", _read("Makefile"))
        assert clean is not None, "no clean target"
        body = clean.group(1)
        removed = _rm_arguments(body)
        self.assertEqual(
            removed - {_FIND_PLACEHOLDER},
            {
                str(DEFAULT_WORKSPACE),
                ".pytest_cache",
                ".ruff_cache",
                "build",
                "dist",
                "src/elt_taskgen.egg-info",
                "constraints.txt",
            },
            "`make clean` removes something other than the scratch workspace, "
            "generated caches/build metadata and the constraints file",
        )
        self.assertIn(str(DEFAULT_WORKSPACE), removed)
        self.assertNotIn("find .", body)
        self.assertNotIn("taskgen-workspace", body)
        for drive in ("dbt_elt", "dlt_elt", "schemapile_elt", "synsql_elt", "wikidbs_elt"):
            self.assertNotIn(drive, body)


class TestInstallCommandsCanActuallyRun(unittest.TestCase):
    """A documented install command that aborts is the D1 defect class.

    `uv export --frozen --format requirements-txt` emits `-e .` as its first
    requirement plus per-package `--hash=` lines, and `pip install -e . -c` then
    aborts twice over: "Editable requirements are not allowed as constraints",
    and then "cannot be installed when requiring hashes".  Both `--no-emit-
    project` and `--no-hashes` are therefore load-bearing wherever we tell a
    reader to build a constraints file.  Verified end to end in a clean venv
    (pip 26.1.1): with both flags the install succeeds and pins duckdb 1.5.5 /
    sqlglot 30.16.0 from uv.lock.
    """

    def _export_lines(self, text: str) -> list[str]:
        return [
            line.strip()
            for line in text.splitlines()
            if "uv export" in line and "requirements-txt" in line
        ]

    def test_every_documented_uv_export_is_pip_installable(self):
        for name in ("README.md", "Makefile"):
            lines = self._export_lines(_read(name))
            self.assertTrue(lines, f"{name} documents no uv export command")
            for line in lines:
                with self.subTest(file=name, line=line):
                    self.assertIn("--no-emit-project", line)
                    self.assertIn("--no-hashes", line)


class TestDerivedCounts(unittest.TestCase):
    """Counts asserted in prose must equal the ones computed from the code."""

    def _one(self, text: str, pattern: str, label: str) -> int:
        found = re.findall(pattern, text, re.DOTALL)
        self.assertTrue(found, f"{label}: pattern {pattern!r} matched nothing")
        values = {int(v) for v in found}
        self.assertEqual(len(values), 1, f"{label}: inconsistent counts {values}")
        return values.pop()

    def test_readme_gate_counts(self) -> None:
        text = _read("README.md")
        self.assertEqual(
            self._one(text, r"#\s*(\d+)\s+shared-integrity gates", "README shared"),
            len(gates.GATE_NAMES),
        )
        self.assertEqual(
            self._one(text, r"#\s*(\d+)\s+EL gates", "README EL"),
            len(gates.VARIANT_GATE_NAMES[TaskVariant.EXTRACT_LOAD]),
        )
        self.assertEqual(
            self._one(text, r"#\s*(\d+)\s+T gates", "README T"),
            len(gates.VARIANT_GATE_NAMES[TaskVariant.TRANSFORM]),
        )

    def test_readme_stage_count(self) -> None:
        self.assertEqual(
            self._one(_read("README.md"), r"stages \(\*\*(\d+)\*\*\)", "README stages"),
            len(STAGE_ORDER),
        )

    def test_interfaces_counts(self) -> None:
        text = _read("docs/INTERFACES.md")
        self.assertEqual(
            self._one(text, r"\*\*(\d+)\*\*\s+ledger stages", "INTERFACES stages"),
            len(STAGE_ORDER),
        )
        self.assertEqual(
            self._one(
                text, r"\*\*(\d+)\*\*\s+shared-integrity\s+gates", "INTERFACES shared"
            ),
            len(gates.GATE_NAMES),
        )
        self.assertEqual(
            self._one(text, r"\*\*(\d+)\*\* for `extract_load`", "INTERFACES EL"),
            len(gates.VARIANT_GATE_NAMES[TaskVariant.EXTRACT_LOAD]),
        )
        self.assertEqual(
            self._one(text, r"\*\*(\d+)\*\* for\s+`transform`", "INTERFACES T"),
            len(gates.VARIANT_GATE_NAMES[TaskVariant.TRANSFORM]),
        )

    def test_sources_counts(self) -> None:
        text = _read("docs/SOURCES.md")
        self.assertEqual(
            self._one(text, r"\*\*(\d+)\*\* ledger stages", "SOURCES stages"),
            len(STAGE_ORDER),
        )
        self.assertEqual(
            self._one(text, r"battery has \*\*(\d+)\*\* gates", "SOURCES gates"),
            len(gates.GATE_NAMES),
        )

    def test_interfaces_gate_roster_is_complete(self) -> None:
        """Every gate name the roster holds must appear in INTERFACES.md."""
        text = _read("docs/INTERFACES.md")
        for name in gates.GATE_NAMES:
            self.assertIn(f"'{name}'", text, f"gate {name!r} undocumented")
        for variant in (TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM):
            for name in gates.VARIANT_GATE_NAMES[variant]:
                self.assertIn(f"'{name}'", text, f"gate {name!r} undocumented")

    def test_interfaces_prose_fidelity_thresholds(self) -> None:
        from elt_taskgen.review import prose_fidelity

        text = _read("docs/INTERFACES.md")
        self.assertIn(f"RULE_TERM_COVERAGE: float             # {prose_fidelity.RULE_TERM_COVERAGE}", text)
        self.assertIn(
            f"COLUMN_DESC_COVERAGE: float           # {prose_fidelity.COLUMN_DESC_COVERAGE}",
            text,
        )

    def test_interfaces_scorer_version(self) -> None:
        self.assertIn(
            f'SCORER_VERSION: str = "{gates.SCORER_VERSION}"', _read("docs/INTERFACES.md")
        )


class TestExperimentsAndBudgetDocs(unittest.TestCase):
    """Roadmap 0.F: `docs/experiments/` is a current document, and the
    metrology budget guidance is ONE number stated in every place it is
    stated."""

    def test_experiments_readme_is_a_current_doc(self) -> None:
        self.assertIn("docs/experiments/README.md", CURRENT_DOCS)
        text = _read("docs/experiments/README.md")
        self.assertIn("PILOT-<name>.md", text)
        for required in ("hypothesis", "cohort digest", "routing fingerprint", "budget cap"):
            self.assertIn(required, text.lower(), required)
        self.assertIn("runs/", text)  # pilot workspace roots are never under it

    def test_budget_guidance_is_synchronised(self) -> None:
        """`metrology._COST_NOTE` names the flag value and the list-rate cost;
        README, INTERFACES, the runbook and the CLI help must quote the same
        ones, and the documented default is `DEFAULT_BUDGET_PER_TASK_USD`."""
        from elt_taskgen.review import metrology as metrology_mod
        from elt_taskgen.review import providers as providers_mod

        note = metrology_mod._COST_NOTE  # noqa: SLF001 - it IS the operator-facing string
        flag = re.search(r"--budget-per-task \d+", note)
        usd = re.search(r"\$\d+(?:\.\d+)?", note)
        assert flag is not None and usd is not None, note
        for rel in ("README.md", "docs/INTERFACES.md", "docs/plans/endtoend_runbook.md"):
            with self.subTest(file=rel):
                self.assertIn(flag.group(0), _read(rel))
        for rel in ("README.md", "docs/plans/endtoend_runbook.md"):
            with self.subTest(file=rel, figure=usd.group(0)):
                self.assertIn(usd.group(0), _read(rel))
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._subparsers._group_actions  # noqa: SLF001
            if getattr(action, "choices", None)
        )
        metrology_help = next(
            choice.help for choice in subparsers._choices_actions if choice.dest == "metrology"  # noqa: SLF001
        )
        self.assertIn(flag.group(0), metrology_help)
        self.assertIn(usd.group(0), metrology_help)
        budget = next(
            action
            for action in subparsers.choices["metrology"]._actions  # noqa: SLF001
            if "--budget-per-task" in action.option_strings
        )
        self.assertIn(flag.group(0), budget.help)
        self.assertEqual(budget.default, providers_mod.DEFAULT_BUDGET_PER_TASK_USD)
        default = f"{providers_mod.DEFAULT_BUDGET_PER_TASK_USD:.2f}"
        self.assertIn(
            f"Most commands default to {default}; pipeline defaults to 7.00",
            budget.help,
        )
        self.assertIn(f"default ${default}", _read("README.md"))
        self.assertIn(f"default {default}", _read("docs/INTERFACES.md"))


class TestBoundedAgentsPhase3Records(unittest.TestCase):
    """Roadmap Phase 3 records and the current bounded-role defaults."""

    def _agents_doc(self) -> dict:
        import yaml

        return yaml.safe_load(_read("config/agents.yaml"))

    def test_phase4_log_distinguishes_its_snapshot_from_current_defaults(self) -> None:
        text = _read("docs/plans/bounded_agents_phase4.md")
        self.assertIn("historical phase log", text)
        self.assertIn("population and shortcut sessions", text)
        self.assertIn("ambiguity and feasibility remain", text)
        self.assertIn("`config/agents.yaml` is the current authority", text)

    def test_phase3_flags_ship_current_profile_and_are_documented(self) -> None:
        from elt_taskgen.review import repair_proposer as rp

        doc = self._agents_doc()
        pop = doc["roles"]["population_adversary"]["session"]
        shc = doc["roles"]["shortcut_attacker"]["session"]
        self.assertTrue(pop["enabled"])
        self.assertTrue(shc["enabled"])
        self.assertEqual(pop["mode"], "harness_validated")
        self.assertEqual(pop["harness_validators"], ["compile_proposal"])
        self.assertEqual(shc["harness_validators"], ["compile_probe"])
        self.assertFalse(pop["measured_match_bit"])
        self.assertFalse(doc["repair"]["certify"]["attack_enabled"])
        self.assertEqual(
            doc["repair"]["routes_bounded"], ["specification", "reference", "population"]
        )
        self.assertEqual(rp.DEFAULT_ROUTES_BOUNDED, ("specification", "reference", "population"))
        self.assertFalse(rp.DEFAULT_CERTIFY_ATTACK_ENABLED)
        for rel in ("README.md", "docs/INTERFACES.md", "docs/plans/bounded_agents_phase3.md"):
            text = _read(rel)
            for key in (
                "repair.certify.attack_enabled",
                "measured_match_bit",
                "routes_bounded",
                "roles.population_adversary.session",
            ):
                with self.subTest(file=rel, key=key):
                    self.assertIn(key, text)
        # The rollback of each flag is a config key, documented as such.
        log = _read("docs/plans/bounded_agents_phase3.md")
        self.assertIn("`enabled: false`", log)
        self.assertIn("without `population`", log)

    def test_pilot_p4_uses_canonical_names_and_states_the_vetoes(self) -> None:
        from elt_taskgen.review import providers as providers_mod
        from elt_taskgen.review.tools import critic_validators as cv

        text = _read("docs/experiments/PILOT-P4.md")
        for heading in (
            "## 1. Hypothesis",
            "## 2. Arms",
            "## 3. Cohort digest",
            "## 4. Replication",
            "## 5. Primary metric and test",
            "## 6. Adopt thresholds, hard vetoes and fallback",
            "## 7. Exclusion rules",
            "## 8. Budget cap",
            "## 9. Routing fingerprint per arm",
        ):
            self.assertIn(heading, text, heading)
        # Canonical names as implemented: the validators, the forced tool, the
        # flag, the limits, and the post-session projection named as NOT a tool.
        for name in (
            f"`{cv.COMPILE_PROPOSAL_TOOL}`",
            f"`{cv.MEASURED_MATCH_BIT_TOOL}`",
            f"`{providers_mod.FINDINGS_TOOL_NAME}`",
            "`max_compile_corrections",
            "`max_oracle_bits",
            "`SCHEMA_RETRIES`",
            "harness_validated",
            "`project_proposal_matrix`",
            "`promote_proposed_cases`",
            "`materialize_mutation`",
        ):
            with self.subTest(name=name):
                self.assertIn(name, text)
        for grammar in cv.GRAMMAR_CODES:
            self.assertIn(f"`{grammar}`", text, grammar)
        self.assertIn(f"`{cv.SAME_MUTANT_FLAG}`", text)
        # The arms, the echo-rate veto and the adopt thresholds of Output 11.
        for arm in ("| A ", "| B0 ", "| B1 ", "| C "):
            self.assertIn(arm, text, arm)
        self.assertIn("0.05", text)
        self.assertIn("echo rate", text.lower())
        self.assertIn("\\ge 0.15", text)
        self.assertIn("\\ge 0.10", text)
        self.assertIn("never a tool", text)
        self.assertIn("runs/", text)
        # The F2 decision is stated (three compiled submissions, an override).
        self.assertIn("F2", text)
        self.assertIn("`max_compile_corrections: 2`", text)
        # This is a dated pre-registration record, not a mirror of the current
        # routing document. Protocol/default changes must stale its digests,
        # never rewrite the values that defined its historical arms.
        self.assertIn("pre-registration", text.lower())
        for historical_digest in (
            "9f2d177ecb462f07",  # system prompt
            "bb49097556cf9839",  # policy
            "25d6642e96088612",  # behaviour
        ):
            self.assertIn(historical_digest, text)
        providers_mod.clear_behavior_caches()
        self.assertNotIn(
            providers_mod.role_behavior_sha256("population_adversary")[:16], text
        )


class TestLedgerSchemaDocumentation(unittest.TestCase):
    def test_interfaces_ddl_matches_engine_schema(self) -> None:
        """INTERFACES.md transcribes engine._SCHEMA; the transcription must agree.

        The repairs table gained a `fingerprint` column (the state a repair round
        started from — the evidence that makes an inert repair provable) and the
        document did not follow, so a reader writing SQL against it wrote SQL
        against a table that no longer exists.
        """
        from elt_taskgen import engine as engine_mod

        documented = _sql_columns(_read("docs/INTERFACES.md"))
        actual = _sql_columns(engine_mod._SCHEMA)  # noqa: SLF001 - it IS the contract
        self.assertEqual(set(documented), set(actual))
        for table, columns in actual.items():
            self.assertEqual(
                documented[table],
                columns,
                f"docs/INTERFACES.md `{table}` columns drifted from engine._SCHEMA",
            )

    def test_interfaces_stage_comment_lists_every_stage(self) -> None:
        block = re.search(
            r"CREATE TABLE IF NOT EXISTS reports\s*\((.*?)\n\);",
            _read("docs/INTERFACES.md"),
            re.DOTALL,
        )
        assert block is not None
        text = block.group(1)
        for stage in STAGE_ORDER:
            self.assertIn(f"'{stage.value}'", text, f"stage {stage.value!r} undocumented")

    def test_interfaces_documents_every_ledger_verdict(self) -> None:
        from elt_taskgen import engine as engine_mod

        block = re.search(
            r"CREATE TABLE IF NOT EXISTS reports\s*\((.*?)\n\);",
            _read("docs/INTERFACES.md"),
            re.DOTALL,
        )
        assert block is not None
        verdicts = getattr(engine_mod, "_VERDICTS", ())
        for verdict in verdicts:
            self.assertIn(f"'{verdict}'", block.group(1), f"verdict {verdict!r} undocumented")


class TestStageNumbering(unittest.TestCase):
    """Stage numbers in prose are the drift class this pins by construction."""

    def _ladder_position(self, value: str) -> int:
        for index, stage in enumerate(STAGE_ORDER, 1):
            if stage.value == value:
                return index
        raise AssertionError(f"{value!r} is not a ledger stage")

    def test_cli_never_numbers_a_stage_without_naming_it(self) -> None:
        """The `Stage 9:` docstring form is banned outright in cli.py.

        That form is what drifted: the runner docstrings said 9..13 while the
        ladder said 11..15, and nothing could tell, because a bare number names
        nothing that the code can be compared against.  Six of those docstrings
        were removed rather than renumbered, so the anti-recurrence pin has to
        be the absence of the form — otherwise the next person writes `Stage
        11:` and it rots the same way the day a stage is inserted.
        """
        source = (REPO_ROOT / "src" / "elt_taskgen" / "cli.py").read_text()
        numbered = re.findall(r'(?m)^\s*(?:"""|#\s*)?Stage\s+\d+\b.*$', source)
        self.assertEqual(
            numbered,
            [],
            "cli.py numbers a stage without naming it; write "
            "`Ledger stage <name> (<n>)` instead, which this file verifies",
        )

    def test_cli_stage_docstrings_match_the_ladder(self) -> None:
        """`Ledger stage <name> (<n>)` must name a real stage at its real index.

        Written to validate whatever is present rather than to demand a
        particular phrasing: cli.py carries no stage numbering at all today
        (the numeric form was deleted, see the test above), and this is what
        keeps the self-describing form honest if it is adopted later.
        """
        source = (REPO_ROOT / "src" / "elt_taskgen" / "cli.py").read_text()
        for name, number in re.findall(r"Ledger stage (\w+) \((\d+)\)", source):
            self.assertEqual(
                self._ladder_position(name),
                int(number),
                f"cli.py says {name} is stage {number}",
            )

    def test_runbook_ladder_numbers(self) -> None:
        text = _read("docs/plans/endtoend_runbook.md")
        for name, stage in (("AUTHOR", "author"), ("AUDIT", "audit")):
            found = re.findall(rf"{name} is ladder stage (\d+)", text)
            self.assertTrue(found, f"runbook no longer states {name}'s ladder position")
            for number in found:
                self.assertEqual(int(number), self._ladder_position(stage))


class TestRequiredDocumentsArePublished(unittest.TestCase):
    """R03: the required documents were ignored by git, so a clean checkout
    lacked them while every local run still passed."""

    def test_every_required_document_is_present_and_non_empty(self) -> None:
        for rel in REQUIRED_DOCS:
            with self.subTest(rel=rel):
                path = REPO_ROOT / rel
                self.assertTrue(path.is_file(), f"{rel} is missing")
                self.assertTrue(path.read_text(encoding="utf-8").strip(), f"{rel} is empty")

    def test_the_gitignore_allowlist_publishes_exactly_the_required_documents(self) -> None:
        lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/docs/*", lines)
        self.assertNotIn("/docs/", lines)
        published = {
            line[2:] for line in lines
            if line.startswith("!/docs/") and not line.endswith("/")
        }
        self.assertEqual(published, set(REQUIRED_DOCS))


class TestCitedFilesExist(unittest.TestCase):
    def test_difficulty_readme_cites_only_existing_tools(self) -> None:
        """A runbook step pointing at a deleted script is a dead reproduction path."""
        text = _read("docs/difficulty/README.md")
        for rel in sorted(set(re.findall(r"tools/[A-Za-z0-9_]+\.py", text))):
            self.assertTrue(
                (REPO_ROOT / rel).is_file(),
                f"docs/difficulty/README.md cites {rel}, which does not exist",
            )

    def test_docs_cite_only_modules_that_exist(self) -> None:
        """Every in-repo `.py` path a current document names must resolve.

        The general form of the `FIX_PIPELINE.md` failure: prose points at a
        file, the file moves or is deleted, and the citation goes on reading
        like a fact.  Paths are resolved against the repo root, the package
        root (`src/elt_taskgen/`, which is how the docs cite modules —
        `export/serve.py`) and the citing document's own directory.  Bare
        basenames and citations into the sibling benchmark checkouts
        (`ELT-Bench/...`) are prose about other repositories and are out of
        scope; only a path whose first segment is a directory of THIS repo is
        held to exist.
        """
        package_root = REPO_ROOT / "src" / "elt_taskgen"
        owned = {p.name for p in package_root.iterdir() if p.is_dir()} | {
            "src",
            "tools",
            "tests",
            "config",
            "docs",
            "scripts",
        }
        cited = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_]+\.py")
        for rel in CURRENT_DOCS:
            document = REPO_ROOT / rel
            for path in sorted(set(cited.findall(_read(rel)))):
                if path.split("/")[0] not in owned:
                    continue
                roots = (REPO_ROOT, package_root, document.parent)
                self.assertTrue(
                    any((root / path).is_file() for root in roots),
                    f"{rel} cites {path}, which exists nowhere in this repo",
                )

    def test_readme_documentation_table_links_resolve(self) -> None:
        text = _read("README.md")
        for target in re.findall(r"\]\((docs/[^)#]+)\)", text):
            self.assertTrue(
                (REPO_ROOT / target).exists(),
                f"README links to {target}, which does not exist",
            )


class TestExecutionStatusVocabulary(unittest.TestCase):
    """The status label is fixed vocabulary, not a vibe.

    The chosen resolution of IR-001 (IMPLEMENTATION_REVIEW_2026-08-31.md) is to
    call the design what it is — a semantic proxy (DuckDB) plus real-runtime
    adapters — everywhere, and to never let a current document or a package
    docstring claim a vendor emulator that does not exist.  These tests hold
    both directions: the banned phrases stay out (or appear only as denials),
    and the canonical label stays in.
    """

    def _emulator_claims(self, text: str) -> list[tuple[int, str]]:
        """(lineno, line) for every emulator-class claim not being denied.

        `DuckDB-compatible` is a violation in ANY context; every other phrase
        is legal only while being denied — a denial word within one line of the
        match (e.g. "does not emulate ...", "it is not a drop-in replacement"
        in docs/difficulty/README.md).
        """
        lines = text.splitlines()
        out: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            # Every match on the line is examined: a denied emulator phrase
            # earlier in the line must not shadow a banned adjective after it.
            for match in _EMULATOR_CLAIM.finditer(line):
                phrase = re.sub(r"[\s-]+", "-", match.group().lower())
                window = "\n".join(lines[max(0, index - 1):index + 2])
                if phrase == "duckdb-compatible" or not _CLAIM_DENIAL.search(
                    window
                ):
                    out.append((index + 1, line.strip()))
                    break
        return out

    def test_current_docs_never_claim_a_vendor_emulator(self) -> None:
        for rel in VOCABULARY_DOCS:
            for lineno, line in self._emulator_claims(_read(rel)):
                self.fail(
                    f"{rel}:{lineno} makes an emulator-class claim without "
                    f"denying it (or uses the banned 'DuckDB-compatible' "
                    f"label): {line!r}"
                )

    def test_source_never_claims_a_vendor_emulator(self) -> None:
        """Docstrings in destinations.py, runtime/, semantic/ are honest today;
        this keeps them that way."""
        package_root = REPO_ROOT / "src" / "elt_taskgen"
        for path in sorted(package_root.rglob("*.py")):
            rel = str(path.relative_to(REPO_ROOT))
            for lineno, line in self._emulator_claims(path.read_text()):
                self.fail(
                    f"{rel}:{lineno} makes an emulator-class claim without "
                    f"denying it (or uses the banned 'DuckDB-compatible' "
                    f"label): {line!r}"
                )

    def test_canonical_status_label_is_present(self) -> None:
        """The three status-bearing documents each carry the exact label.

        Whitespace-normalized so markdown line wrapping cannot break the pin.
        """
        for rel in ("README.md", "docs/EXECUTION_MODEL.md", "docs/WAREHOUSE_CONNECTORS.md"):
            with self.subTest(file=rel):
                self.assertIn(_CANONICAL_LABEL, " ".join(_read(rel).split()))

    def test_execution_model_status_paragraph_is_complete(self) -> None:
        """The canonical paragraph keeps its three load-bearing statements.

        The third is a time-bound factual pin: when the first real cloud
        certification attestation lands, the paragraph and this test must
        change together — that is the forcing function, not an accident.
        """
        normalized = " ".join(_read("docs/EXECUTION_MODEL.md").split())
        for sentence in (
            "it does not emulate Snowflake, Databricks, or Amazon Redshift",
            "no vendor emulator endpoint exists in this repository",
            "No cloud certification attestation is currently recorded in this checkout",
        ):
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, normalized)

    def test_warehouse_runbook_heading_is_scoped(self) -> None:
        """The runbook heading stays 'Cross-warehouse projection contract'.

        The old 'compatibility contract' heading asserted a three-vendor claim
        the checkout's own evidence table ("none recorded") does not back.
        """
        self.assertNotIn(
            "Cross-warehouse compatibility contract",
            _read("docs/WAREHOUSE_CONNECTORS.md"),
        )


class TestWarehouseConnectorPinDocs(unittest.TestCase):
    """Connector versions and the sync-mode scope quoted in the runbook must
    equal the single source of truth in `elt_taskgen.destinations`.

    This exact drift class occurred: the doc said Snowflake `4.1.2` while the
    code briefly pinned `4.0.49`.
    """

    def test_every_destination_version_in_the_runbook_matches_the_contract(
        self,
    ) -> None:
        from elt_taskgen.destinations import DESTINATION_CONTRACTS, Destination

        labels = {
            "Snowflake": Destination.SNOWFLAKE,
            "Databricks": Destination.DATABRICKS,
            "Redshift": Destination.REDSHIFT,
        }
        text = _read("docs/WAREHOUSE_CONNECTORS.md")
        normalized = " ".join(text.split())
        for label, destination in labels.items():
            contract = DESTINATION_CONTRACTS[destination]
            with self.subTest(destination=destination.value):
                # The destination table row binds both the definition id and
                # the pinned image version.
                row = next(
                    (
                        line
                        for line in text.splitlines()
                        if line.startswith(f"| {label} ")
                    ),
                    None,
                )
                self.assertIsNotNone(row, f"no {label} row in the pin table")
                self.assertIn(f"`{contract.definition_id}`", row)
                self.assertIn(f"`{contract.connector_version}`", row)
                # Every prose mention of a version next to the vendor name
                # (e.g. "Snowflake `4.1.2`") must be the pinned one.
                for quoted in re.findall(
                    rf"{label} `(\d+\.\d+\.\d+)`", normalized
                ):
                    self.assertEqual(
                        quoted,
                        contract.connector_version,
                        f"{label} version drifted in prose",
                    )

    def test_source_pin_sentence_matches_the_contracts(self) -> None:
        from elt_taskgen.destinations import SOURCE_CONNECTOR_CONTRACTS

        labels = {
            "flat_files": "Files",
            "mongodb": "MongoDB v2",
            "postgres": "Postgres",
            "aws_s3": "S3",
        }
        normalized = " ".join(_read("docs/WAREHOUSE_CONNECTORS.md").split())
        for section, label in labels.items():
            contract = SOURCE_CONNECTOR_CONTRACTS[section]
            with self.subTest(section=section):
                self.assertIn(
                    f"{label} `{contract.connector_version}`",
                    normalized,
                    f"{label} source pin drifted from destinations.py",
                )

    def test_sync_mode_scope_names_exactly_the_supported_modes(self) -> None:
        from elt_taskgen.destinations import SUPPORTED_SYNC_MODES

        text = _read("docs/WAREHOUSE_CONNECTORS.md")
        self.assertIn("## Sync-mode scope", text)
        section = text.split("## Sync-mode scope", 1)[1].split("\n## ", 1)[0]
        for mode in SUPPORTED_SYNC_MODES:
            self.assertIn(f"`{mode}`", section)
        self.assertIn("elt_taskgen.destinations.SUPPORTED_SYNC_MODES", section)
        # No other sync mode may be presented as certified: the only
        # backtick-quoted *_append/_dedupe/overwrite tokens are the declared
        # ones.
        quoted_modes = {
            token
            for token in re.findall(r"`([a-z_]+)`", section)
            if token.endswith(("_append", "_dedupe", "_overwrite"))
            or token == "overwrite"
        }
        self.assertEqual(quoted_modes, set(SUPPORTED_SYNC_MODES))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
