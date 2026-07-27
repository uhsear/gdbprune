#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gdbprune - delete stale leaf versions from a traditionally-versioned
Enterprise geodatabase, printing a plan first.

WHO THIS IS FOR
    A paid ArcGIS Enterprise install, a TRADITIONALLY-versioned geodatabase,
    and an admin .sde connection file produced by Esri's own tooling.
    Branch-versioned geodatabases get nothing from this tool: branch versions
    live in a different system table and are removed through a different API.

WHY LEAF-FIRST
    The relational database refuses to drop a version that is still the parent
    of another version. A single "delete everything old that matches" pass
    therefore selects parents whose children are still alive and fails on them.
    gdbprune runs iterative passes: each pass deletes only versions that are
    currently leaves, then re-reads the version table and repeats. A parent
    becomes eligible only once its children are actually gone. The loop stops
    when a pass deletes nothing, or - on a pathologically deep tree - at
    MAX_PASSES, which is reported as INCOMPLETE and exits 1 rather than
    passing off a half-finished run as success.

CONFIG PRECEDENCE (same order is stated in the README)
    command-line flag  >  environment variable  >  built-in default
    Only --workspace has an environment variable: SDE_MAINTENANCE_WORKSPACE.

SAFETY
    Without --apply the tool prints the plan and exits 0, deleting nothing.

    python gdbprune.py --self-test     # no arcpy, no network, no credentials
"""

from __future__ import print_function

import argparse
import os
import re
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# CONFIGURATION - tunables that are deliberately not command-line flags.
# Flag > environment variable > the values below.
# --------------------------------------------------------------------------

# Version names that are never candidates, no matter what the flags say.
# Compared case-insensitively against the unqualified version name.
RESERVED_VERSIONS = ("DEFAULT",)

# Environment variable consulted when --workspace is absent.
ENV_WORKSPACE = "SDE_MAINTENANCE_WORKSPACE"

# Defaults for the two tunable filters.
DEFAULT_PRUNE_PATTERN = "%SYNC%"
DEFAULT_PRUNE_DAYS = 7

# Esri's system table holding traditional versions.
VERSION_TABLE = "sde.SDE_versions"

# Hard stop on the iterative loop. The loop already terminates on its own
# (a pass that deletes nothing ends it, and a cycle can never produce a leaf),
# so this only bounds pathological version trees. A run cut off here sets
# PruneResult.converged False, prints INCOMPLETE, and exits 1 - a version
# chain deeper than this needs a second run, not a silent truncation.
MAX_PASSES = 100

# Interpreter that ships arcpy. Named in the ModuleNotFoundError message.
PRO_PYTHON = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"


# --------------------------------------------------------------------------
# Pure core - no arcpy, no database, no clock of its own.
# A "version" is a dict: {"name", "parent", "owner", "created"}
#   name    unqualified version name            e.g. "SYNC_20260714_A"
#   parent  unqualified name of its parent      e.g. "DEFAULT" or None
#   owner   database or domain user that owns it
#   created datetime the version was created (None = unknown, never pruned)
# --------------------------------------------------------------------------

PruneResult = namedtuple("PruneResult", "passes failures versions_seen converged")


class PatternError(ValueError):
    """Raised for a --prune-pattern that cannot be turned into safe SQL."""


def compile_like_pattern(pattern):
    """Turn a SQL LIKE pattern into a case-insensitive match function.

    A raw single quote would terminate the string literal in the generated
    SQL predicate, so it is refused outright rather than escaped: escaping
    rules differ per backend and a silently mangled pattern would quietly
    widen or narrow a destructive selection.
    """
    if pattern is None:
        raise PatternError("--prune-pattern must not be empty")
    if "'" in pattern:
        raise PatternError(
            "--prune-pattern must not contain a single quote: %r" % (pattern,)
        )
    if not pattern:
        raise PatternError("--prune-pattern must not be empty")

    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    rx = re.compile("^" + "".join(out) + "$", re.IGNORECASE)
    return lambda name: bool(rx.match(name or ""))


def build_candidate_where(pattern, prune_days):
    """Return the SQL predicate that defines 'stale and matching'.

    This string is validated and printed in the plan so an auditor can read
    exactly what "candidate" means. It is deliberately NOT pushed down into
    the fetch query: leafness has to be computed over EVERY row of the
    version table, because a child that does not match the pattern still
    pins its parent. Filtering the fetch by the pattern would hide such a
    child and make a pinned parent look like a leaf.
    """
    compile_like_pattern(pattern)  # validates; raises PatternError
    if int(prune_days) < 0:
        raise ValueError("--prune-days must not be negative")
    return "name LIKE '%s' AND creation_time < DATEADD(day, -%d, GETDATE())" % (
        pattern,
        int(prune_days),
    )


def qualified_name(version):
    """owner.name, with the owner double-quoted only when it needs to be.

    A domain-qualified owner (DOMAIN\\user) contains a backslash, which is
    not a legal bare identifier character, so it must be double-quoted.
    A plain database user must be left bare - quoting it can flip the
    identifier to case-sensitive on some backends and break the delete.
    """
    owner = (version.get("owner") or "").strip().strip('"')
    name = version["name"]
    if not owner:
        return name
    if "\\" in owner:
        return '"%s".%s' % (owner, name)
    return "%s.%s" % (owner, name)


def is_reserved(version):
    """True for a version that is never a candidate.

    The owner qualification is stripped first: some backends hand back
    `sde.DEFAULT` rather than `DEFAULT` in SDE_versions.name, and a reserved
    name that slipped through under its qualified form would become an
    ordinary candidate. Stripping can only widen the never-delete set, so
    the failure direction is "refused a legitimate delete", not "deleted
    DEFAULT".
    """
    name = (version.get("name") or "").strip().strip('"')
    name = name.rsplit(".", 1)[-1].strip().strip('"')
    return name.upper() in {r.upper() for r in RESERVED_VERSIONS}


def _in_scope(version, only_upper):
    name = (version.get("name") or "").strip().upper()
    return name in only_upper or qualified_name(version).strip().upper() in only_upper


def select_candidates(versions, matcher, cutoff, only_versions=None):
    """The one function that decides what may be deleted right now.

    A version is a candidate when ALL of these hold:
      * it is not a reserved version (sde.DEFAULT)
      * it is inside --only-versions, when that scoping was supplied
        (an EMPTY only_versions list scopes to nothing, not to everything)
      * its creation date is strictly older than `cutoff`
      * its name matches the LIKE pattern
      * NOTHING in `versions` names it as a parent, i.e. it is a leaf

    That last clause is the whole point. Dropping it yields the naive
    one-pass selector, which happily selects a parent with a live child and
    fails against the database.
    """
    parents = set()
    for v in versions:
        p = v.get("parent")
        if p:
            parents.add(p.strip().upper())

    # `is not None`, not truthiness: an empty list means the caller asked to
    # scope the run and named nothing, which must select nothing. Treating it
    # as "no scoping" would silently widen a destructive run to every version.
    only_upper = None
    if only_versions is not None:
        only_upper = {s.strip().upper() for s in only_versions if s and s.strip()}

    out = []
    for v in versions:
        if is_reserved(v):
            continue
        if only_upper is not None and not _in_scope(v, only_upper):
            continue
        created = v.get("created")
        if created is None:
            continue
        # `cutoff` is naive; a backend column such as SQL Server's
        # datetimeoffset can hand back an aware datetime, and comparing the
        # two raises TypeError. Convert to local wall clock and drop the
        # offset so the comparison is always naive-vs-naive.
        if getattr(created, "tzinfo", None) is not None:
            created = created.astimezone().replace(tzinfo=None)
        if created >= cutoff:
            continue
        if not matcher(v.get("name") or ""):
            continue
        if (v.get("name") or "").strip().upper() in parents:
            continue  # still a parent: not a leaf, not eligible this pass
        out.append(v)
    out.sort(key=lambda v: (qualified_name(v).upper()))
    return out


def prune(
    fetch,
    delete=None,
    pattern=DEFAULT_PRUNE_PATTERN,
    prune_days=DEFAULT_PRUNE_DAYS,
    only_versions=None,
    now=None,
    max_passes=MAX_PASSES,
):
    """Leaf-first iterative prune.

    fetch()   -> the current version list. Re-invoked after every applied
                 pass, so the next pass sees the real post-delete tree.
    delete(v) -> performs the deletion. None means dry run: nothing is
                 called, and the tree is walked down in memory instead.

    Returns PruneResult(passes, failures, versions_seen, converged) where
    `passes` is a list of lists of version dicts, one entry per pass that
    removed anything, and `converged` is False when the run stopped because
    it hit `max_passes` rather than because it ran out of work.
    """
    # Validated here, not only in the CLI: a negative prune_days pushes the
    # cutoff into the FUTURE and makes versions created yesterday eligible.
    # This function is the destructive-selection entry point, so the guard
    # belongs here where every caller routes through it.
    if int(prune_days) < 0:
        raise ValueError("prune_days must not be negative")
    matcher = compile_like_pattern(pattern)
    cutoff = (now or datetime.now()) - timedelta(days=int(prune_days))

    survivors = list(fetch())
    versions_seen = len(survivors)
    passes = []
    failures = []
    # A version that refused to delete once will refuse again on every later
    # pass. Remember it so the run neither retries it nor logs it twice.
    refused = set()
    # False until a pass proves there is nothing left to do. Staying False
    # means the loop was cut off by max_passes with work still outstanding -
    # main() turns that into a non-zero exit so an incomplete --apply run is
    # never reported as success.
    converged = False

    for _ in range(int(max_passes)):
        candidates = [
            v
            for v in select_candidates(survivors, matcher, cutoff, only_versions)
            if (v.get("name") or "").strip().upper() not in refused
        ]
        if not candidates:
            converged = True
            break

        removed = []
        for v in candidates:
            if delete is None:
                removed.append(v)
                continue
            try:
                delete(v)
                removed.append(v)
            except Exception as exc:  # a locked version must not stop the run
                refused.add((v.get("name") or "").strip().upper())
                failures.append((v, exc))

        if not removed:
            converged = True
            break  # every candidate failed; another pass would repeat it
        passes.append(removed)

        if delete is None:
            gone = {(v.get("name") or "").strip().upper() for v in removed}
            survivors = [
                v for v in survivors if (v.get("name") or "").strip().upper() not in gone
            ]
        else:
            survivors = list(fetch())

    return PruneResult(passes, failures, versions_seen, converged)


# --------------------------------------------------------------------------
# Execution layer - the only place arcpy is touched.
# --------------------------------------------------------------------------


def _import_arcpy():
    """Import arcpy on demand so --self-test runs on plain CPython."""
    try:
        import arcpy  # noqa: F401  (imported for its side effect + return)
    except ModuleNotFoundError:
        raise SystemExit(
            "arcpy is not available in this interpreter (%s).\n"
            "gdbprune needs the ArcGIS Pro conda environment:\n"
            "    %s gdbprune.py --workspace <admin.sde>\n"
            "Only --self-test runs without arcpy." % (sys.executable, PRO_PYTHON)
        )
    return arcpy


def _coerce_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt)
        except ValueError:
            continue
    return None


def fetch_versions(workspace):
    """Read the entire traditional version table as version dicts.

    The whole table is read, unfiltered, on purpose: leafness depends on
    every row. See build_candidate_where() for why the pattern is not
    pushed into this query.
    """
    arcpy = _import_arcpy()
    if not arcpy.Exists(workspace):
        raise SystemExit("workspace does not exist or is not readable: %s" % workspace)

    sql = arcpy.ArcSDESQLExecute(workspace)
    rows = sql.execute(
        "SELECT name, parent_name, owner, creation_time FROM %s" % VERSION_TABLE
    )
    if not rows or rows is True:
        return []
    if rows and not isinstance(rows[0], (list, tuple)):
        rows = [rows]

    versions = []
    for row in rows:
        name, parent, owner, created = (list(row) + [None, None, None, None])[:4]
        if not name:
            continue
        versions.append(
            {
                "name": str(name).strip(),
                "parent": str(parent).strip() if parent else None,
                "owner": str(owner).strip().strip('"') if owner else "",
                "created": _coerce_datetime(created),
            }
        )
    return versions


def make_deleter(workspace):
    """Return a delete callback bound to a real geodatabase."""
    arcpy = _import_arcpy()

    def _delete(version):
        arcpy.management.DeleteVersion(workspace, qualified_name(version))

    return _delete


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_plan(result, args, workspace, cutoff, applied):
    header = "APPLY - versions are being deleted" if applied else "DRY RUN - nothing is deleted"
    print("")
    print("gdbprune %s  [%s]" % (__version__, header))
    print("  workspace      : %s" % workspace)
    print("  prune pattern  : %s" % args.prune_pattern)
    print(
        "  older than     : %d day(s)  (created before %s)"
        % (args.prune_days, cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    )
    if args.only_versions is None:
        scope = "all versions"
    else:
        scope = ", ".join(args.only_versions) or "(empty - nothing is eligible)"
    print("  scope          : %s" % scope)
    print("  candidate rule : %s" % build_candidate_where(args.prune_pattern, args.prune_days))
    print("  versions read  : %d" % result.versions_seen)
    print("")

    total = 0
    for i, batch in enumerate(result.passes, 1):
        print("  pass %d  -  %d leaf version(s)" % (i, len(batch)))
        for v in batch:
            print("      %s" % qualified_name(v))
        total += len(batch)

    if not result.passes:
        print("  nothing to prune.")
    print("")

    if result.failures:
        print("  %d failure(s):" % len(result.failures))
        for v, exc in result.failures:
            print("      %s  ->  %s" % (qualified_name(v), exc))
        print("")

    if not result.converged:
        print(
            "  INCOMPLETE: stopped at the %d-pass limit with work still\n"
            "  outstanding. Re-run to continue, or raise MAX_PASSES." % MAX_PASSES
        )
        print("")

    verb = "deleted" if applied else "would be deleted"
    print(
        "  %d version(s) %s across %d pass(es)."
        % (total, verb, len(result.passes))
    )
    if not applied:
        print("  Re-run with --apply to perform the deletions.")
    print("")


# --------------------------------------------------------------------------
# Self-test - pure Python, no arcpy, no network, no credentials.
# --------------------------------------------------------------------------

_PASSED = [0]
_FAILED = []


def check(condition, label):
    if condition:
        _PASSED[0] += 1
        print("PASS  %s" % label)
    else:
        _FAILED.append(label)
        print("FAIL  %s" % label)


def _v(name, parent=None, owner="gisowner", age_days=30):
    return {
        "name": name,
        "parent": parent,
        "owner": owner,
        "created": datetime(2026, 1, 1, 12, 0, 0) - timedelta(days=age_days),
    }


def _world(versions):
    """A mutable fake geodatabase: fetch() re-reads it, delete() mutates it."""
    live = list(versions)
    calls = []

    def fetch():
        return list(live)

    def delete(v):
        calls.append(qualified_name(v))
        live[:] = [x for x in live if x["name"] != v["name"]]

    return fetch, delete, calls, live


def _naive_select(versions, matcher, cutoff):
    """The one-pass rewrite this tool exists to prevent: no leaf clause."""
    return [
        v
        for v in versions
        if not is_reserved(v)
        and v.get("created") is not None
        and v["created"] < cutoff
        and matcher(v["name"])
    ]


def self_test():
    NOW = datetime(2026, 1, 1, 12, 0, 0)
    CUTOFF = NOW - timedelta(days=7)
    M = compile_like_pattern("%SYNC%")

    print("gdbprune %s self-test (no arcpy, no network, no credentials)" % __version__)
    print("-" * 66)

    # ---- leaf-first correctness on a chain A -> B -> C -------------------
    # A is the parent of B, B is the parent of C. Only C is a leaf.
    chain = [
        _v("SYNC_A", parent="DEFAULT"),
        _v("SYNC_B", parent="SYNC_A"),
        _v("SYNC_C", parent="SYNC_B"),
        _v("DEFAULT", parent=None, owner="sde", age_days=900),
    ]

    first = select_candidates(chain, M, CUTOFF)
    first_names = [v["name"] for v in first]
    check(first_names == ["SYNC_C"], "chain pass 1 selects exactly the leaf SYNC_C")
    check("SYNC_B" not in first_names, "chain pass 1 does NOT select SYNC_B (has live child)")
    check("SYNC_A" not in first_names, "chain pass 1 does NOT select SYNC_A (has live child)")

    fetch, delete, calls, live = _world(chain)
    res = prune(fetch, delete, "%SYNC%", 7, now=NOW)
    check(len(res.passes) == 3, "chain converges in exactly chain-depth (3) passes")
    check([v["name"] for v in res.passes[0]] == ["SYNC_C"], "chain pass 1 deleted SYNC_C")
    check([v["name"] for v in res.passes[1]] == ["SYNC_B"], "chain pass 2 deleted SYNC_B after re-query")
    check([v["name"] for v in res.passes[2]] == ["SYNC_A"], "chain pass 3 deleted SYNC_A after re-query")
    check(calls == ["gisowner.SYNC_C", "gisowner.SYNC_B", "gisowner.SYNC_A"],
          "chain deletes strictly leaf-to-root")
    check(res.failures == [], "chain run reports no failures")
    check([v["name"] for v in live] == ["DEFAULT"], "chain leaves only DEFAULT alive")
    check(res.versions_seen == 4, "versions_seen reports the initial table size")
    check(res.converged is True, "a run that finishes its work reports converged")

    # ---- THE PINNED DEFECT ----------------------------------------------
    # SYNC_B matches the pattern and is old enough, but has a live child.
    # The naive one-pass selector takes it. This selector must not.
    naive = [v["name"] for v in _naive_select(chain, M, CUTOFF)]
    ours = [v["name"] for v in select_candidates(chain, M, CUTOFF)]
    check("SYNC_B" in naive, "naive one-pass selector DOES select the pinned parent SYNC_B")
    check("SYNC_B" not in ours, "PINNED DEFECT: old+matching version with a live child is never selected")
    check("SYNC_A" in naive, "naive one-pass selector also selects the root SYNC_A")
    check("SYNC_A" not in ours, "PINNED DEFECT: root with a live child is never selected")
    check(len(ours) < len(naive), "leaf clause strictly narrows the naive selection")

    # ---- age threshold ---------------------------------------------------
    aged = [
        _v("SYNC_OLD", parent="DEFAULT", age_days=30),
        _v("SYNC_NEW", parent="DEFAULT", age_days=2),
        {"name": "SYNC_UNKNOWN", "parent": "DEFAULT", "owner": "gisowner", "created": None},
    ]
    picked = [v["name"] for v in select_candidates(aged, M, CUTOFF)]
    check(picked == ["SYNC_OLD"], "only the leaf older than --prune-days is selected")
    check("SYNC_NEW" not in picked, "matching leaf younger than --prune-days is preserved")
    check("SYNC_UNKNOWN" not in picked, "version with an unknown creation date is preserved")

    f2, d2, c2, live2 = _world(aged)
    prune(f2, d2, "%SYNC%", 7, now=NOW)
    check(sorted(v["name"] for v in live2) == ["SYNC_NEW", "SYNC_UNKNOWN"],
          "young and undated versions survive the whole run")

    # A backend column such as SQL Server's datetimeoffset hands back an
    # AWARE datetime; cutoff is naive, and comparing the two raises TypeError.
    # Both fixtures sit days clear of the cutoff, so no local timezone can
    # move them across it.
    EST = timezone(timedelta(hours=-5))
    tz_old = [{"name": "SYNC_TZ_OLD", "parent": "DEFAULT", "owner": "gisowner",
               "created": datetime(2025, 12, 2, 12, 0, 0, tzinfo=EST)}]
    tz_new = [{"name": "SYNC_TZ_NEW", "parent": "DEFAULT", "owner": "gisowner",
               "created": datetime(2025, 12, 30, 12, 0, 0, tzinfo=EST)}]
    check([v["name"] for v in select_candidates(tz_old, M, CUTOFF)] == ["SYNC_TZ_OLD"],
          "a timezone-aware creation_time is compared without raising TypeError")
    check(select_candidates(tz_new, M, CUTOFF) == [],
          "a timezone-aware version inside the window is still preserved")

    # The negative-prune_days guard belongs to the core, not just the CLI:
    # a negative value pushes the cutoff into the future and makes versions
    # created yesterday eligible.
    fresh = [_v("SYNC_YESTERDAY", parent="DEFAULT", age_days=1)]
    try:
        prune(lambda: list(fresh), None, "%SYNC%", -30, now=NOW)
        check(False, "prune() itself refuses a negative --prune-days")
    except ValueError:
        check(True, "prune() itself refuses a negative --prune-days, not just the CLI")

    # ---- pattern ---------------------------------------------------------
    mixed = [
        _v("SYNC_LEAF", parent="DEFAULT"),
        _v("NIGHTLY_LEAF", parent="DEFAULT"),
    ]
    picked = [v["name"] for v in select_candidates(mixed, M, CUTOFF)]
    check(picked == ["SYNC_LEAF"], "old leaf that does not match the pattern is preserved")

    check(compile_like_pattern("SYNC%")("SYNC_1") is True, "trailing %% wildcard matches")
    check(compile_like_pattern("SYNC%")("PRE_SYNC") is False, "anchored pattern rejects a prefix mismatch")
    check(compile_like_pattern("SYNC_")("SYNCA") is True, "underscore wildcard matches one character")
    check(compile_like_pattern("SYNC_")("SYNCAB") is False, "underscore wildcard matches exactly one character")
    check(compile_like_pattern("%sync%")("BUILD_SYNC_9") is True, "pattern matching is case-insensitive")
    check(compile_like_pattern("%A.B%")("XA.BY") is True, "literal dot in a pattern is escaped, not a wildcard")
    check(compile_like_pattern("%A.B%")("XAZBY") is False, "literal dot does not act as a regex wildcard")

    # A non-matching CHILD still pins a matching parent.
    pinned_by_stranger = [
        _v("SYNC_PARENT", parent="DEFAULT"),
        _v("MANUAL_EDIT", parent="SYNC_PARENT"),
    ]
    picked = [v["name"] for v in select_candidates(pinned_by_stranger, M, CUTOFF)]
    check(picked == [], "a child that does not match the pattern still pins its parent")

    # ---- owner quoting ---------------------------------------------------
    domain = _v("SYNC_D", parent="DEFAULT", owner="DOMAIN\\jsmith")
    plain = _v("SYNC_P", parent="DEFAULT", owner="gisowner")
    check(qualified_name(domain) == '"DOMAIN\\jsmith".SYNC_D',
          "owner containing a backslash is double-quoted")
    check(qualified_name(plain) == "gisowner.SYNC_P",
          "plain database owner is NOT quoted")
    check('"' not in qualified_name(plain), "no stray quotes around a plain owner")
    check(qualified_name({"name": "SYNC_Q", "owner": '"DOMAIN\\jsmith"'}) == '"DOMAIN\\jsmith".SYNC_Q',
          "an owner already carrying quotes is normalised, not double-quoted twice")
    check(qualified_name({"name": "SYNC_R", "owner": ""}) == "SYNC_R",
          "an empty owner yields the bare version name")

    f3, d3, c3, _live3 = _world([domain, plain])
    prune(f3, d3, "%SYNC%", 7, now=NOW)
    check(sorted(c3) == ['"DOMAIN\\jsmith".SYNC_D', "gisowner.SYNC_P"],
          "the delete callback receives correctly-quoted qualified names")

    # ---- dry run vs apply ------------------------------------------------
    f4, _d4, _c4, _live4 = _world(chain)
    dry = prune(f4, None, "%SYNC%", 7, now=NOW)
    # Asserted against `dry`, not against _world's delete callback: that
    # callback is never handed to prune(), so any check on it passes
    # unconditionally. Under a prune() that ignores the dry-run gate,
    # None(v) raises TypeError straight into failures - which is what this
    # assertion catches.
    check(dry.failures == [], "without --apply prune() never attempts a delete call")
    check(len(dry.passes) == 3, "the dry run still walks the full 3-pass plan in memory")
    check([v["name"] for p in dry.passes for v in p] == ["SYNC_C", "SYNC_B", "SYNC_A"],
          "the dry-run plan matches the order --apply would use")

    f5, d5, c5, _l5 = _world(chain)
    applied = prune(f5, d5, "%SYNC%", 7, now=NOW)
    check(len(c5) == 3, "with --apply the callback is invoked once per real candidate")
    check(len(c5) == sum(len(p) for p in applied.passes),
          "callback invocations equal the number of planned deletions")

    # ---- pattern rejects a raw single quote ------------------------------
    try:
        compile_like_pattern("%SY'NC%")
        check(False, "pattern containing a single quote raises")
    except PatternError:
        check(True, "--prune-pattern containing a single quote raises PatternError")

    try:
        build_candidate_where("%'; DROP--", 7)
        check(False, "where-clause builder refuses a quoted pattern")
    except PatternError:
        check(True, "where-clause builder raises rather than emitting broken SQL")

    check("SYNC" in build_candidate_where("%SYNC%", 7), "a clean pattern builds a where clause")
    check("-7" in build_candidate_where("%SYNC%", 7), "the where clause carries the day threshold")

    try:
        compile_like_pattern("")
        check(False, "empty pattern raises")
    except PatternError:
        check(True, "an empty --prune-pattern is refused")

    # ---- --only-versions -------------------------------------------------
    scoped = [
        _v("SYNC_ONE", parent="DEFAULT"),
        _v("SYNC_TWO", parent="DEFAULT"),
        _v("SYNC_THREE", parent="DEFAULT", owner="DOMAIN\\jsmith"),
    ]
    picked = [v["name"] for v in select_candidates(scoped, M, CUTOFF, ["SYNC_ONE"])]
    check(picked == ["SYNC_ONE"], "--only-versions restricts the candidate set")
    check("SYNC_TWO" not in picked, "an old matching leaf outside --only-versions is preserved")

    picked = [v["name"] for v in select_candidates(scoped, M, CUTOFF, ["gisowner.SYNC_TWO"])]
    check(picked == ["SYNC_TWO"], "--only-versions accepts the owner-qualified form")

    picked = [v["name"] for v in select_candidates(scoped, M, CUTOFF, ['"DOMAIN\\jsmith".SYNC_THREE'])]
    check(picked == ["SYNC_THREE"], "--only-versions accepts a quoted domain-qualified name")

    picked = [v["name"] for v in select_candidates(scoped, M, CUTOFF, ["sync_one"])]
    check(picked == ["SYNC_ONE"], "--only-versions matching is case-insensitive")

    picked = [v["name"] for v in select_candidates(scoped, M, CUTOFF, ["NO_SUCH_VERSION"])]
    check(picked == [], "--only-versions naming nothing real selects nothing")

    picked = [v["name"] for v in select_candidates(scoped, M, CUTOFF, None)]
    check(len(picked) == 3, "no --only-versions means no scoping")

    # --only-versions "" in a wrapper script (an unset variable) must scope
    # to nothing. Falling back to "no scoping" would silently widen the run
    # to every matching version.
    check(select_candidates(scoped, M, CUTOFF, []) == [],
          "an EMPTY --only-versions scopes to nothing, it does not disable scoping")
    f_empty, d_empty, c_empty, live_empty = _world(scoped)
    prune(f_empty, d_empty, "%SYNC%", 7, only_versions=[], now=NOW)
    check(c_empty == [], "an empty --only-versions asks the database to delete nothing")
    check(len(live_empty) == 3, "an empty --only-versions leaves every version alive")

    # ---- sde.DEFAULT ------------------------------------------------------
    default_only = [{"name": "DEFAULT", "parent": None, "owner": "sde",
                     "created": datetime(2000, 1, 1)}]
    check(select_candidates(default_only, compile_like_pattern("%"), CUTOFF) == [],
          "sde.DEFAULT is never a candidate even as an ancient matching leaf")
    check(select_candidates(default_only, compile_like_pattern("%"), CUTOFF, ["DEFAULT"]) == [],
          "sde.DEFAULT is not a candidate even when named in --only-versions")
    check(select_candidates(default_only, compile_like_pattern("%"), CUTOFF, ["sde.DEFAULT"]) == [],
          "sde.DEFAULT is not a candidate under its qualified name either")
    check(is_reserved({"name": "default"}) is True, "reserved check is case-insensitive")
    check(is_reserved({"name": "sde.DEFAULT"}) is True,
          "an owner-qualified DEFAULT is still recognised as reserved")
    qualified_default = [{"name": "sde.DEFAULT", "parent": None, "owner": "",
                          "created": datetime(2000, 1, 1)}]
    check(select_candidates(qualified_default, compile_like_pattern("%"), CUTOFF) == [],
          "a DEFAULT returned owner-qualified by the driver is never a candidate")

    f6, d6, c6, live6 = _world(default_only)
    prune(f6, d6, "%", 7, now=NOW)
    check(c6 == [], "a full run never asks the database to delete DEFAULT")
    check(len(live6) == 1, "DEFAULT survives a wide-open prune")

    # ---- cycles / self-parents -------------------------------------------
    self_parent = [_v("SYNC_LOOP", parent="SYNC_LOOP")]
    f7, d7, c7, _l7 = _world(self_parent)
    res7 = prune(f7, d7, "%SYNC%", 7, now=NOW, max_passes=25)
    check(res7.passes == [], "a self-parenting version is never a leaf and is never deleted")
    check(c7 == [], "a self-parenting version does not trigger a delete")

    cycle = [_v("SYNC_X", parent="SYNC_Y"), _v("SYNC_Y", parent="SYNC_X")]
    f8, d8, c8, live8 = _world(cycle)
    res8 = prune(f8, d8, "%SYNC%", 7, now=NOW, max_passes=25)
    check(res8.passes == [], "a two-node cycle terminates without deleting anything")
    check(len(live8) == 2, "both members of a cycle survive")

    cycle_plus_leaf = [
        _v("SYNC_X", parent="SYNC_Y"),
        _v("SYNC_Y", parent="SYNC_X"),
        _v("SYNC_FREE", parent="DEFAULT"),
    ]
    f9, d9, c9, _l9 = _world(cycle_plus_leaf)
    res9 = prune(f9, d9, "%SYNC%", 7, now=NOW, max_passes=25)
    check(c9 == ["gisowner.SYNC_FREE"], "a cycle does not block an unrelated leaf")
    check(len(res9.passes) == 1, "the run stops after the one productive pass")

    # ---- delete failures --------------------------------------------------
    stubborn = [_v("SYNC_LOCKED", parent="DEFAULT"), _v("SYNC_OK", parent="DEFAULT")]

    def failing_delete(v):
        if v["name"] == "SYNC_LOCKED":
            raise RuntimeError("version is locked by another session")
        stubborn[:] = [x for x in stubborn if x["name"] != v["name"]]

    res10 = prune(lambda: list(stubborn), failing_delete, "%SYNC%", 7, now=NOW, max_passes=25)
    check(len(res10.failures) == 1, "a locked version is recorded as a failure exactly once")
    check(res10.failures[0][1].args[0].startswith("version is locked"),
          "the recorded failure carries the underlying database error")
    check(res10.failures[0][0]["name"] == "SYNC_LOCKED", "the failure names the offending version")
    check([v["name"] for p in res10.passes for v in p] == ["SYNC_OK"],
          "a failure does not prevent its siblings from being deleted")
    check(len(res10.passes) <= 2, "a permanently failing version does not loop forever")

    # ---- diamond: one parent, two children --------------------------------
    diamond = [
        _v("SYNC_ROOT", parent="DEFAULT"),
        _v("SYNC_L", parent="SYNC_ROOT"),
        _v("SYNC_R", parent="SYNC_ROOT"),
    ]
    f11, d11, c11, _l11 = _world(diamond)
    res11 = prune(f11, d11, "%SYNC%", 7, now=NOW)
    check(len(res11.passes) == 2, "two siblings collapse in one pass, their parent in the next")
    check(sorted(v["name"] for v in res11.passes[0]) == ["SYNC_L", "SYNC_R"],
          "both sibling leaves are taken in the same pass")
    check(c11[-1] == "gisowner.SYNC_ROOT", "the shared parent is deleted last")

    # ---- max_passes truncation --------------------------------------------
    # A chain deeper than max_passes stops with work outstanding. Without a
    # converged flag that run is indistinguishable from a clean finish and
    # main() would exit 0 on a half-done --apply.
    deep = [
        _v("SYNC_N%02d" % i, parent=("SYNC_N%02d" % (i - 1)) if i else "DEFAULT")
        for i in range(10)
    ]
    f13, d13, _c13, live13 = _world(deep)
    res13 = prune(f13, d13, "%SYNC%", 7, now=NOW, max_passes=3)
    check(res13.converged is False, "hitting max_passes is reported as a NON-converged run")
    check(len(res13.passes) == 3, "the run really did stop at the pass limit")
    check(len(live13) == 7, "a truncated run leaves the un-pruned versions behind")
    check(res13.failures == [], "a truncated run has no failures to signal with")

    f14, d14, _c14, live14 = _world(deep)
    res14 = prune(f14, d14, "%SYNC%", 7, now=NOW, max_passes=100)
    check(res14.converged is True, "the same tree under a sufficient limit converges")
    check(live14 == [], "the converged run emptied the chain")

    # ---- empty input -------------------------------------------------------
    res12 = prune(lambda: [], None, "%SYNC%", 7, now=NOW)
    check(res12.passes == [], "an empty version table produces an empty plan")
    check(res12.versions_seen == 0, "an empty version table reports zero versions read")
    check(res12.converged is True, "an empty version table counts as converged")

    print("-" * 66)
    total = _PASSED[0] + len(_FAILED)
    if _FAILED:
        print("FAILED: %d of %d assertions" % (len(_FAILED), total))
        for label in _FAILED:
            print("   - %s" % label)
        return 1
    print("OK: %d/%d assertions passed" % (_PASSED[0], total))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="gdbprune",
        description=(
            "Delete stale leaf versions from a traditionally-versioned Enterprise "
            "geodatabase, printing a plan first. Requires ArcGIS Pro's Python, a "
            "traditionally-versioned geodatabase, and an admin .sde connection file."
        ),
        epilog=(
            "Config precedence: command-line flag > environment variable > built-in "
            "default. Only --workspace reads an environment variable (%s)." % ENV_WORKSPACE
        ),
    )
    p.add_argument(
        "--workspace",
        help="Path to an admin .sde connection file. Required for a real run. "
             "Falls back to $%s; the flag wins." % ENV_WORKSPACE,
    )
    p.add_argument(
        "--prune-pattern",
        default=DEFAULT_PRUNE_PATTERN,
        help="SQL LIKE pattern a version name must match (default: %(default)s). "
             "A raw single quote is refused.",
    )
    p.add_argument(
        "--prune-days",
        type=int,
        default=DEFAULT_PRUNE_DAYS,
        help="Only versions created more than N days ago (default: %(default)s).",
    )
    p.add_argument(
        "--only-versions",
        help="Comma-separated version names to restrict the run to. "
             "Bare or owner-qualified names both work. An empty value scopes "
             "the run to nothing; omit the flag entirely for no scoping.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Omitted (the default) prints the plan and exits.",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in assertions and exit. No arcpy, no network, no credentials.",
    )
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.self_test:
        return self_test()

    # An explicitly-passed --only-versions "" (an unset variable in a wrapper
    # script) must scope the run to NOTHING, not disable scoping entirely.
    if args.only_versions is not None:
        args.only_versions = [
            s.strip() for s in args.only_versions.split(",") if s.strip()
        ]

    # Validate the pattern before anything touches the database.
    try:
        build_candidate_where(args.prune_pattern, args.prune_days)
    except (PatternError, ValueError) as exc:
        raise SystemExit("error: %s" % exc)

    # Config precedence: flag > environment variable > built-in default.
    workspace = args.workspace or os.environ.get(ENV_WORKSPACE)
    if not workspace:
        raise SystemExit(
            "error: --workspace is required for a real run "
            "(or set $%s). Use --self-test to verify the tool without a database."
            % ENV_WORKSPACE
        )

    now = datetime.now()
    cutoff = now - timedelta(days=args.prune_days)
    deleter = make_deleter(workspace) if args.apply else None

    result = prune(
        fetch=lambda: fetch_versions(workspace),
        delete=deleter,
        pattern=args.prune_pattern,
        prune_days=args.prune_days,
        only_versions=args.only_versions,
        now=now,
    )

    print_plan(result, args, workspace, cutoff, applied=bool(args.apply))
    return 1 if (result.failures or not result.converged) else 0


if __name__ == "__main__":
    sys.exit(main())
