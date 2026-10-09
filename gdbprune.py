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

MODES
    --workspace W                 plan against the live table       (arcpy)
    --workspace W --apply         delete, leaf first                (arcpy)
    --workspace W --export-versions OUT.json [--apply]
                                  write a version snapshot; the file
                                  is written only under --apply     (arcpy)
    --from-versions FILE.json     plan from a snapshot. Read-only,
                                  refuses --apply, never imports arcpy
    --self-test                   the assertions below              (none)
    --allow-replica-anchors       let SYNC_SEND / SYNC_RECEIVE replica
                                  system versions be candidates. Off by
                                  default

SNAPSHOT SCHEMA (format "gdbprune-versions", schema_version 2)
    {"format": "gdbprune-versions", "schema_version": 2,
     "exported_at": "2026-09-26T09:30:00",
     "versions": [{"name": "DEFAULT", "parent": null, "owner": "sde",
                   "created": "2019-03-01T08:00:00", "replica": false},
                  {"name": "SYNC_A", "parent": "DEFAULT", "owner": "gisowner",
                   "created": "2026-09-01T12:00:00", "replica": false}]}
    Exactly these fields, no more and no fewer, at both levels. Timestamps are
    YYYY-MM-DDTHH:MM:SS with an optional .ffffff and no offset. exported_at
    and a creation time arcpy returns as an aware datetime are the exporting
    machine's local wall clock; a naive creation time is written as the
    database returned it. A creation time returned as text with an offset is
    written as null (unknown, never pruned). parent and created may be null.
    replica is true for a version that a registered replica uses
    (arcpy.da.ListReplicas). Schema 1 had no replica field and is refused.
    The list must hold DEFAULT. The connection string, host and password are
    never written. owner values are database accounts, and can include the account
    that ran the export.

EXIT CODES
    0 clean. 1 the run left work: a delete refused, the pass limit was hit,
    the table could not be re-read after an applied pass, or a matching
    version had no readable creation time and was not assessed. A live run
    (plan or --apply) that cannot run exits 1 too, as in 1.0.0. The two
    snapshot modes exit 2 when they cannot run: an invalid snapshot, no
    arcpy, no workspace, an export path that holds something other than a
    snapshot. argparse exits 2 on a usage error in every mode.

CONFIG PRECEDENCE (same order is stated in the README)
    command-line flag  >  environment variable  >  built-in default
    Only --workspace has an environment variable: SDE_MAINTENANCE_WORKSPACE.

SAFETY
    Without --apply the tool prints the plan, deleting nothing and
    writing nothing. A version that arcpy.da.ListReplicas names as a
    registered replica's version is never a candidate. The guard sees only
    the replicas that list returns to the connecting account. Replica
    system versions (SYNC_SEND..., SYNC_RECEIVE...) are
    never candidates, whatever the pattern, unless --allow-replica-anchors
    is given. Flags cannot be abbreviated.

    python gdbprune.py --self-test     # no arcpy, no network, no credentials
"""

from __future__ import print_function

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
from collections import namedtuple
from datetime import datetime, timedelta, timezone

__version__ = "1.3.0"

# --------------------------------------------------------------------------
# CONFIGURATION - tunables that are deliberately not command-line flags.
# Flag > environment variable > the values below.
# --------------------------------------------------------------------------

# Version names that are never candidates, no matter what the flags say.
# Compared case-insensitively against the unqualified version name.
RESERVED_VERSIONS = ("DEFAULT",)

# Replica system versions, held back unless --allow-replica-anchors is given.
# Esri: "SYNC_RECEIVE and SYNC_SEND versions are recorded in the sde versions
# table ... should not be manually deleted" (knowledge base article 000009436).
# They are SQL LIKE patterns matched against the unqualified name, so the
# "_" wildcard also holds back a look-alike such as SYNCXSEND_1. The error
# direction is a version kept, never an anchor deleted.
REPLICA_ANCHOR_PATTERNS = ("SYNC_SEND%", "SYNC_RECEIVE%")

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

# The version snapshot. Bump SNAPSHOT_SCHEMA_VERSION on any change to the
# field lists: a reader must refuse a file whose shape it does not know.
SNAPSHOT_FORMAT = "gdbprune-versions"
SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_FIELDS = ("format", "schema_version", "exported_at", "versions")
VERSION_FIELDS = ("name", "parent", "owner", "created", "replica")


# --------------------------------------------------------------------------
# Pure core - no arcpy, no database, no clock of its own.
# A "version" is a dict: {"name", "parent", "owner", "created"}
#   name    unqualified version name            e.g. "SYNC_20260714_A"
#   parent  unqualified name of its parent      e.g. "DEFAULT" or None
#   owner   database or domain user that owns it
#   created datetime the version was created (None = unknown, never pruned)
#   replica True when a registered replica uses it (never pruned); a dict
#           without the key is not one
# --------------------------------------------------------------------------

PruneResult = namedtuple("PruneResult",
                         "passes failures versions_seen converged undated error anchors replicas")


class ToolError(Exception):
    """The command cannot run: bad input, no arcpy, no workspace, a file it
    will not overwrite. main() prints it and exits 1 in a live run, as
    1.0.0 did, and 2 in a snapshot mode. Any other exception does the same."""


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


_ANCHOR_MATCHERS = [compile_like_pattern(a) for a in REPLICA_ANCHOR_PATTERNS]


def build_candidate_where(pattern, prune_days, allow_anchors=False):
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
    held = "" if allow_anchors else "".join(
        " AND name NOT LIKE '%s'" % a for a in REPLICA_ANCHOR_PATTERNS)
    return "name LIKE '%s'%s AND creation_time < DATEADD(day, -%d, GETDATE())" % (
        pattern,
        held,
        int(prune_days),
    )


def compute_cutoff(now, prune_days):
    """now minus prune_days days: versions created before this are old.

    A negative value pushes the cutoff into the FUTURE and makes versions
    created yesterday eligible, so it is refused. A value so large that the
    cutoff leaves the datetime range is refused too, as a ValueError rather
    than an OverflowError traceback.
    """
    days = int(prune_days)
    if days < 0:
        raise ValueError("--prune-days must not be negative")
    try:
        return now - timedelta(days=days)
    except OverflowError:
        raise ValueError("--prune-days %d reaches back before the year 1" % days)


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


def node_key(name):
    """The unqualified, unquoted, upper-cased form of a version name.

    Some backends hand back `sde.DEFAULT` rather than `DEFAULT`, in the name
    column or in parent_name, and a snapshot can carry either form. Leafness
    and the reserved check both compare this key, so `gisowner.SYNC_P`,
    `"SYNC_P"` and `SYNC_P` are one node. Merging nodes can only add
    children to a node, so the failure direction is "skipped a leaf", never
    "deleted a parent".
    """
    name = (name or "").strip().strip('"')
    return name.rsplit(".", 1)[-1].strip().strip('"').upper()


def is_reserved(version):
    """True for a version that is never a candidate. Compared on node_key(),
    so an owner-qualified `sde.DEFAULT` is still DEFAULT."""
    return node_key(version.get("name")) in {r.upper() for r in RESERVED_VERSIONS}


def is_replica_anchor(version):
    """True for a SYNC_SEND / SYNC_RECEIVE replica version. Compared on
    node_key(), so `gisowner.sync_send_7_1` is one too."""
    key = node_key(version.get("name"))
    return any(m(key) for m in _ANCHOR_MATCHERS)


def _in_scope(version, only_upper):
    name = (version.get("name") or "").strip().upper()
    return name in only_upper or qualified_name(version).strip().upper() in only_upper


def _scope(only_versions):
    """The --only-versions set, upper-cased, or None for no scoping.

    `is not None`, not truthiness: an empty list means the caller asked to
    scope the run and named nothing, which must select nothing. Treating it
    as "no scoping" would silently widen a destructive run to every version."""
    if only_versions is None:
        return None
    return {s.strip().upper() for s in only_versions if s and s.strip()}


def _matches(version, matcher, only_upper):
    """Not reserved, inside the scope, and matching the pattern."""
    return (not is_reserved(version)
            and (only_upper is None or _in_scope(version, only_upper))
            and matcher(version.get("name") or ""))


def _eligible(version, matcher, only_upper, allow_anchors=False):
    """_matches(), not a version a registered replica uses, and not a
    held-back replica anchor. No flag releases a registered replica's
    version: unregistering the replica is what releases it."""
    return (_matches(version, matcher, only_upper)
            and not version.get("replica")
            and (allow_anchors or not is_replica_anchor(version)))


# The instants between which this module asks the OS for the local offset.
# Windows refuses datetime.astimezone() outside roughly 1970-3000 with
# OSError [Errno 22]; Linux does not. Clamping the lookup makes both hosts
# give the same answer for a sentinel such as 1900-01-01+00:00.
_OFFSET_LO = datetime(1970, 1, 2, tzinfo=timezone.utc)
_OFFSET_HI = datetime(2999, 12, 30, tzinfo=timezone.utc)


def local_naive(dt):
    """An aware datetime as this machine's local wall clock, naive. A naive
    one, or None, is returned as is. Outside 1970-2999 the offset of the
    nearest end of that range is used. A time that then leaves the datetime
    range (year 1 or 9999) is None: unknown, never pruned."""
    if dt is None or dt.tzinfo is None:
        return dt
    try:
        utc = dt.astimezone(timezone.utc)
        offset = min(max(utc, _OFFSET_LO), _OFFSET_HI).astimezone().utcoffset()
        return (utc + offset).replace(tzinfo=None)
    except OverflowError:
        return None


def has_default(versions):
    """True when the list holds DEFAULT. Every traditionally-versioned
    geodatabase has it, so a list without it is not a whole version table:
    a query that returned no rows, or a file cut down by hand. Planning such
    a list prints "nothing to prune", exactly like a clean table."""
    return any(node_key(v.get("name")) == "DEFAULT" for v in versions)


def select_candidates(versions, matcher, cutoff, only_versions=None, allow_anchors=False):
    """The one function that decides what may be deleted right now.

    A version is a candidate when ALL of these hold:
      * it is not a reserved version (sde.DEFAULT)
      * no registered replica uses it (the "replica" key)
      * it is not a replica anchor (SYNC_SEND..., SYNC_RECEIVE...), unless
        allow_anchors is True
      * it is inside --only-versions, when that scoping was supplied
        (an EMPTY only_versions list scopes to nothing, not to everything)
      * its creation date is strictly older than `cutoff`
      * its name matches the LIKE pattern
      * NOTHING in `versions` names it as a parent, i.e. it is a leaf

    That last clause is the whole point. Dropping it yields the naive
    one-pass selector, which happily selects a parent with a live child and
    fails against the database.
    """
    # A null parent adds the key "", which no real version name has.
    parents = {node_key(v.get("parent")) for v in versions}
    only_upper = _scope(only_versions)

    out = []
    for v in versions:
        if not _eligible(v, matcher, only_upper, allow_anchors):
            continue
        # `cutoff` is naive; a backend column such as SQL Server's
        # datetimeoffset can hand back an aware datetime, and comparing the
        # two raises TypeError. local_naive() makes it local wall clock.
        created = local_naive(v.get("created"))
        if created is None or created >= cutoff:
            continue
        if node_key(v.get("name")) in parents:
            continue  # still a parent: not a leaf, not eligible this pass
        out.append(v)
    out.sort(key=lambda v: (qualified_name(v).upper()))
    return out


def count_undated(versions, matcher, only_versions=None, allow_anchors=False):
    """How many versions select_candidates() would weigh but cannot date.

    Such a version is never pruned. Without this count, a table whose every
    creation time is unreadable plans "nothing to prune", exactly like a
    clean one, although not a single version was assessed."""
    only_upper = _scope(only_versions)
    return sum(1 for v in versions
               if _eligible(v, matcher, only_upper, allow_anchors)
               and local_naive(v.get("created")) is None)


def count_anchors(versions, matcher, only_versions=None):
    """How many replica anchors match the pattern and the scope: the
    versions the anchor guard holds back, or releases under the flag. An
    anchor that a registered replica uses is left to count_replicas(), since
    the flag cannot release it."""
    only_upper = _scope(only_versions)
    return sum(1 for v in versions
               if is_replica_anchor(v) and _eligible(v, matcher, only_upper, True))


def count_replicas(versions, matcher, only_versions=None):
    """How many versions that a registered replica uses match the pattern
    and the scope: the versions held back whatever the flags say."""
    only_upper = _scope(only_versions)
    return sum(1 for v in versions if v.get("replica") and _matches(v, matcher, only_upper))


def prune(
    fetch,
    delete=None,
    pattern=DEFAULT_PRUNE_PATTERN,
    prune_days=DEFAULT_PRUNE_DAYS,
    only_versions=None,
    now=None,
    max_passes=MAX_PASSES,
    allow_anchors=False,
):
    """Leaf-first iterative prune.

    fetch()   -> the current version list. Re-invoked after every applied
                 pass, so the next pass sees the real post-delete tree.
    delete(v) -> performs the deletion. None means dry run: nothing is
                 called, and the tree is walked down in memory instead.

    Returns PruneResult(passes, failures, versions_seen, converged, undated,
    error, anchors, replicas) where `passes` is a list of lists of version dicts, one entry per
    pass that removed anything, `converged` is False when the run stopped
    with work still outstanding, `undated` is count_undated() of the first
    read, `error` is the exception that stopped an applied run after it
    had deleted something (None otherwise), and `anchors` is count_anchors()
    of the first read, and `replicas` is count_replicas() of the first read.
    A replica anchor is a candidate only when allow_anchors is True, and a
    registered replica's version never is; held back, each still pins its
    parent.
    """
    # Validated here, not only in the CLI: this function is the
    # destructive-selection entry point, so the guard belongs where every
    # caller routes through it.
    cutoff = compute_cutoff(now or datetime.now(), prune_days)
    matcher = compile_like_pattern(pattern)

    survivors = list(fetch())
    versions_seen = len(survivors)
    undated = count_undated(survivors, matcher, only_versions, allow_anchors)
    anchors = count_anchors(survivors, matcher, only_versions)
    replicas = count_replicas(survivors, matcher, only_versions)
    passes = []
    failures = []
    # A version that refused to delete once will refuse again on every later
    # pass. Remember it so the run neither retries it nor logs it twice.
    # Keyed on the exact owner.name, like `gone` below: two owners can each
    # hold a SYNC_X, and a case-sensitive backend can hold SYNC_A and sync_a.
    # One refusing must not hide the other.
    refused = set()

    def pending():
        return [v for v in select_candidates(survivors, matcher, cutoff, only_versions,
                                             allow_anchors)
                if qualified_name(v) not in refused]

    for _ in range(int(max_passes)):
        candidates = pending()
        if not candidates:
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
                refused.add(qualified_name(v))
                failures.append((v, exc))

        if not removed:
            break  # every candidate failed; another pass would repeat it
        passes.append(removed)

        if delete is None:
            # By the exact owner.name, the name --apply deletes: removing
            # crew1.SYNC_X must not also remove crew2.SYNC_X, nor SYNC_A
            # remove sync_a, whose parent would then look like a leaf the
            # live run never deletes.
            gone = {qualified_name(v) for v in removed}
            survivors = [v for v in survivors if qualified_name(v) not in gone]
        else:
            try:
                survivors = list(fetch())
            except Exception as exc:
                # The deletes above cannot be undone. Return them, so the
                # report lists them and the run exits 1 (left work), not 2
                # (could not run) with nothing named.
                return PruneResult(passes, failures, versions_seen, False, undated, exc,
                                   anchors, replicas)

    # The one convergence verdict. Work still pending means max_passes cut
    # the loop off, and main() turns that into a non-zero exit so an
    # incomplete --apply is never reported as success. Computed here, not in
    # the loop: the last allowed pass may take the last candidates without
    # ever running the empty pass that proves it.
    converged = not pending()
    return PruneResult(passes, failures, versions_seen, converged, undated, None, anchors,
                       replicas)


# --------------------------------------------------------------------------
# Version snapshot - pure. build_snapshot() and dump_snapshot() make the
# text --export-versions writes; parse_snapshot() is the strict reader that
# --from-versions feeds into prune().
# --------------------------------------------------------------------------


class SnapshotError(ValueError):
    """Raised for a snapshot that does not match the schema exactly."""


class OldSchemaError(SnapshotError):
    """A schema 1 snapshot. It is refused for planning, but an export may
    replace it, as it replaces any earlier snapshot."""


# [0-9], not \d: \d also matches non-ASCII digits, which strptime then
# accepts. fullmatch(), not match() with a trailing $: $ also matches just
# before a final newline.
_STAMP_RX = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?")


def format_stamp(dt):
    """A naive datetime as snapshot text. build_snapshot() passes an aware
    value through local_naive() first, the same conversion
    select_candidates() applies, so the file never carries an offset."""
    return dt.isoformat()


def parse_stamp(value, where):
    """The strict inverse of format_stamp(). `where` names the field."""
    if not isinstance(value, str) or not _STAMP_RX.fullmatch(value):
        raise SnapshotError(
            "%s: expected YYYY-MM-DDTHH:MM:SS[.ffffff] with no offset, got %r"
            % (where, value)
        )
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in value else "%Y-%m-%dT%H:%M:%S"
    try:
        return datetime.strptime(value, fmt)
    except ValueError:
        raise SnapshotError("%s: not a real date and time: %r" % (where, value))


def build_snapshot(versions, exported_at):
    """The snapshot dict for `versions`. The clock is an argument.

    The five version fields are copied by name and nothing else is. No
    connection detail - workspace path, host, password - is an input to this
    function, so none of it can reach the file. An owner is a database
    account, and the connecting account often owns versions too.
    """
    rows = []
    for v in versions:
        created = local_naive(v.get("created"))
        rows.append({
            "name": v["name"],
            "parent": v.get("parent") or None,
            "owner": v.get("owner") or "",
            "created": None if created is None else format_stamp(created),
            "replica": bool(v.get("replica")),
        })
    return {
        "format": SNAPSHOT_FORMAT,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "exported_at": format_stamp(exported_at),
        "versions": rows,
    }


def dump_snapshot(snapshot):
    """Pure-ASCII JSON text for a snapshot dict."""
    return json.dumps(snapshot, indent=2, ensure_ascii=True) + "\n"


def _unique_keys(pairs):
    # json.loads keeps the LAST of two equal keys and says nothing. A file
    # with "created" twice would be read under whichever came second.
    out = {}
    for key, value in pairs:
        if key in out:
            raise SnapshotError("field %r appears twice in one object" % (key,))
        out[key] = value
    return out


def _no_constant(token):
    raise SnapshotError("%s is not a JSON value" % (token,))


def _exact_fields(obj, fields, where):
    if not isinstance(obj, dict):
        raise SnapshotError("%s: expected an object, got %s" % (where, type(obj).__name__))
    missing = [f for f in fields if f not in obj]
    if missing:
        raise SnapshotError("%s: missing field(s): %s" % (where, ", ".join(missing)))
    unknown = sorted(k for k in obj if k not in fields)
    if unknown:
        raise SnapshotError("%s: unknown field(s): %s" % (where, ", ".join(unknown)))


def _clean_text(value, allow_empty=False):
    # A JSON escape such as \f, \u0085, \u200b or \ud800 decodes to a
    # character nobody can see, and a lone surrogate cannot even be printed.
    # Inside an owner it silently changes the name a delete would be issued
    # against. str.isprintable() is False for every control (C0 and C1),
    # format, surrogate, private-use and unassigned character, and for every
    # space except U+0020. "Unassigned" follows the running Python's Unicode
    # version, so an older Python can refuse a name a newer one accepted.
    return (isinstance(value, str) and (allow_empty or value.strip() != "")
            and value == value.strip() and value.isprintable())


def parse_snapshot(text):
    """Validate snapshot text and return (exported_at, versions).

    Strict on purpose. The versions it returns decide what a later --apply
    is expected to delete, so a field that is missing, extra, mistyped or
    half-parsed is refused, never defaulted.
    """
    try:
        doc = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except SnapshotError:
        raise
    except (ValueError, RecursionError) as exc:
        # RecursionError: a file nested thousands of levels deep. It is a
        # RuntimeError, not a ValueError, and would otherwise escape as a
        # traceback.
        raise SnapshotError("not valid JSON: %s" % exc)

    _exact_fields(doc, SNAPSHOT_FIELDS, "snapshot")
    if doc["format"] != SNAPSHOT_FORMAT:
        raise SnapshotError("format: expected %r, got %r" % (SNAPSHOT_FORMAT, doc["format"]))
    # type() is int, not isinstance(): True is an int and True == 1.
    version = doc["schema_version"]
    if type(version) is not int or version != SNAPSHOT_SCHEMA_VERSION:
        # Schema 1 (gdbprune 1.1.0) records no replica versions,
        # so a plan from it could select one. It is refused, not upgraded.
        raise (OldSchemaError if type(version) is int and version == 1 else SnapshotError)(
            "schema_version: expected %d, got %r. Export the table again with this release."
            % (SNAPSHOT_SCHEMA_VERSION, doc["schema_version"])
        )
    exported_at = parse_stamp(doc["exported_at"], "exported_at")
    if not isinstance(doc["versions"], list):
        raise SnapshotError("versions: expected a list, got %s" % type(doc["versions"]).__name__)

    versions = []
    seen = set()
    for i, row in enumerate(doc["versions"]):
        where = "versions[%d]" % i
        _exact_fields(row, VERSION_FIELDS, where)
        if not _clean_text(row["name"]):
            raise SnapshotError("%s.name: expected a non-empty name with no surrounding "
                                "space or unprintable character, got %r" % (where, row["name"]))
        if row["parent"] is not None and not _clean_text(row["parent"]):
            raise SnapshotError("%s.parent: expected null or a non-empty name with no "
                                "surrounding space or unprintable character, got %r"
                                % (where, row["parent"]))
        if not _clean_text(row["owner"], allow_empty=True):
            raise SnapshotError("%s.owner: expected text with no surrounding space or "
                                "unprintable character, got %r" % (where, row["owner"]))
        # type() is bool, not truthiness: "false" and 0 must not read as False.
        if type(row["replica"]) is not bool:
            raise SnapshotError("%s.replica: expected true or false, got %r"
                                % (where, row["replica"]))
        created = row["created"]
        v = {
            "name": row["name"],
            "parent": row["parent"],
            "owner": row["owner"],
            "created": None if created is None else parse_stamp(created, where + ".created"),
            "replica": row["replica"],
        }
        # The exact owner.name, as prune() keys it: a case-sensitive backend
        # can hold SYNC_A and sync_a, and the live mode plans both.
        key = qualified_name(v)
        if key in seen:
            raise SnapshotError("%s: %s appears twice" % (where, qualified_name(v)))
        seen.add(key)
        versions.append(v)
    if not has_default(versions):
        raise SnapshotError("versions: no DEFAULT version, so this is not a whole version "
                            "table (%d version(s) listed)" % len(versions))
    return exported_at, versions


def describe_age(exported_at, now):
    """How old the snapshot is, for the plan header."""
    delta = now - exported_at
    if delta < timedelta(0):
        return "later than this machine's clock; check both clocks"
    return "%d day(s) %d hour(s) before this run" % (delta.days, delta.seconds // 3600)


# --------------------------------------------------------------------------
# Execution layer - the only place arcpy or the file system is touched.
# --------------------------------------------------------------------------


def _import_arcpy():
    """Import arcpy on demand so --self-test runs on plain CPython."""
    try:
        import arcpy  # noqa: F401  (imported for its side effect + return)
    except ModuleNotFoundError:
        raise ToolError(
            "arcpy is not available in this interpreter (%s).\n"
            "gdbprune needs the ArcGIS Pro conda environment:\n"
            "    %s gdbprune.py --workspace <admin.sde>\n"
            "Only --self-test and --from-versions run without arcpy."
            % (sys.executable, PRO_PYTHON)
        )
    return arcpy


# A creation time returned as text: a date, optionally a time, optionally a
# fraction of up to seven digits (SQL Server datetime2), and nothing after it.
_DB_TIME_RX = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:[ T]([0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,7}))?)?")


def _coerce_datetime(value):
    """A creation time as a datetime, or None (unknown, never pruned).

    Text with anything after the time, such as a UTC offset, is None: an
    offset that was cut off silently would move the time by hours. Only an
    aware datetime object is converted from its offset (see local_naive).
    A seventh fraction digit rounds UP to the microsecond, so a version
    never reads older than it is."""
    if value is None or isinstance(value, datetime):
        return value
    m = _DB_TIME_RX.fullmatch(str(value).strip())
    if not m:
        return None
    day, clock, frac = m.groups()
    try:
        dt = datetime.strptime(day + " " + (clock or "00:00:00"), "%Y-%m-%d %H:%M:%S")
        # int(frac.ljust(7, "0")) is in units of 100 ns; -(-n // 10) rounds up.
        return dt + timedelta(microseconds=-(-int((frac or "0").ljust(7, "0")) // 10))
    except (ValueError, OverflowError):
        return None


def fetch_versions(workspace):
    """Read the entire traditional version table as version dicts.

    The whole table is read, unfiltered, on purpose: leafness depends on
    every row. See build_candidate_where() for why the pattern is not
    pushed into this query.
    """
    arcpy = _import_arcpy()
    if not arcpy.Exists(workspace):
        raise ToolError("workspace does not exist or is not readable: %s" % redact(workspace))

    sql = arcpy.ArcSDESQLExecute(workspace)
    rows = sql.execute(
        "SELECT name, parent_name, owner, creation_time FROM %s" % VERSION_TABLE
    )
    if not rows or rows is True:
        rows = []
    elif not isinstance(rows[0], (list, tuple)):
        rows = [rows]

    # The version each registered replica uses. Esri returns a feature
    # service replica on traditionally versioned data, such as an offline
    # map's, as a Replica even with all_replicas False; True adds the
    # SyncReplica objects (branch versioned or nonversioned data), so the
    # guard reads the widest list Esri offers. Read on every re-read, before
    # any delete. An error here stops the run: a run that cannot tell which
    # versions replicas use must not delete any.
    in_use = {node_key(r.version) for r in arcpy.da.ListReplicas(workspace, True)}

    versions = []
    for row in rows:
        name, parent, owner, created = (list(row) + [None, None, None, None])[:4]
        if not name:
            continue
        name = str(name).strip()
        versions.append(
            {
                "name": name,
                "parent": str(parent).strip() if parent else None,
                "owner": str(owner).strip().strip('"') if owner else "",
                "created": _coerce_datetime(created),
                "replica": node_key(name) in in_use,
            }
        )
    if not has_default(versions):
        raise ToolError("the version table read from %s holds no DEFAULT version, so it is "
                        "not a whole version table (%d row(s) read). Nothing was planned."
                        % (redact(workspace), len(versions)))
    return versions


def make_deleter(workspace):
    """Return a delete callback bound to a real geodatabase."""
    arcpy = _import_arcpy()

    def _delete(version):
        arcpy.management.DeleteVersion(workspace, qualified_name(version))

    return _delete


def read_snapshot(path):
    """Read and validate a snapshot file. utf-8-sig accepts a BOM, which
    Notepad and PowerShell add to a file they save."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        try:
            text = fh.read()
        except UnicodeDecodeError as exc:
            raise SnapshotError("not UTF-8 text: %s" % exc)
    return parse_snapshot(text)


def write_snapshot(path, text):
    """Write a snapshot. main() calls this only under --apply."""
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _say(text, stream=None):
    """print() for everything the tool reports. Names, owners, paths and
    database errors can hold any character, and a stdout redirected to a
    file on Windows encodes cp1252 strictly: one such name used to crash the
    report of an --apply run AFTER its deletes. Escaping to ASCII here means
    no text can do that, on any console or code page."""
    print(text.encode("ascii", "backslashreplace").decode("ascii"), file=stream or sys.stdout)


def _mute_stdout():
    """Point a stdout that failed to write (a full disk, a closed pipe) at
    the null device. Python flushes stdout again at exit, and a second
    failure there would replace the exit code with 120."""
    try:
        target = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return  # no file descriptor, so no flush at exit that could fail
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, target)
    os.close(null)


def redact(text):
    """A workspace, or an error that may quote one, for display. The values
    of a connection string (KEY=value;...) are hidden, so a password, user
    or host never reaches stdout, stderr or a scheduler's log. A .sde file
    path is shown as is.

    Hidden whole instead: a string with a brace or a quote next to an '='
    (PWD={a;b} holds a ';'), and any string with '@' or '://'. A URL
    (scheme://user:password@host) or an EZConnect string (user/password@host)
    has no KEY=value form, and a password may itself hold ':', '/' or '@'."""
    if ("=" in text and re.search(r"[{}'\"]", text)) or "@" in text or "://" in text:
        return "(connection string hidden)"
    return re.sub(r"=[^;]*", "=***", text)


def print_plan(result, args, workspace, cutoff, applied, exported_at=None, now=None):
    """Print a prune plan. With `exported_at` the plan came from a snapshot
    file, and the export time is printed in the header and the verdict."""
    header = "APPLY - versions are being deleted" if applied else "DRY RUN - nothing is deleted"
    _say("")
    _say("gdbprune %s  [%s]" % (__version__, header))
    if exported_at is None:
        _say("  workspace      : %s" % redact(workspace))
    else:
        _say("  snapshot       : %s" % redact(workspace))
        _say("  exported at    : %s  (%s)"
              % (format_stamp(exported_at), describe_age(exported_at, now)))
    _say("  prune pattern  : %s" % args.prune_pattern)
    _say(
        "  older than     : %d day(s)  (created before %s)"
        % (args.prune_days, cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    )
    if args.only_versions is None:
        scope = "all versions"
    else:
        scope = ", ".join(args.only_versions) or "(empty - nothing is eligible)"
    _say("  scope          : %s" % scope)
    _say("  candidate rule : %s" % build_candidate_where(args.prune_pattern, args.prune_days,
                                                         args.allow_replica_anchors))
    _say("  versions read  : %d" % result.versions_seen)
    _say("  undated        : %d matching version(s) with no readable creation time,"
         " never pruned" % result.undated)
    if args.allow_replica_anchors:
        _say("  replica anchors: ALLOWED by --allow-replica-anchors; %d matching"
             " SYNC_SEND/SYNC_RECEIVE version(s) can be pruned" % result.anchors)
    else:
        _say("  replica anchors: %d matching SYNC_SEND/SYNC_RECEIVE version(s) held back,"
             " never pruned" % result.anchors)
    _say("  replica in use : %d matching version(s) that a registered replica uses,"
         " never pruned" % result.replicas)
    _say("")

    total = 0
    for i, batch in enumerate(result.passes, 1):
        _say("  pass %d  -  %d leaf version(s)" % (i, len(batch)))
        for v in batch:
            _say("      %s" % qualified_name(v))
        total += len(batch)

    if not result.passes:
        _say("  nothing to prune.")
    _say("")

    if result.failures:
        _say("  %d failure(s):" % len(result.failures))
        for v, exc in result.failures:
            _say("      %s  ->  %s" % (qualified_name(v), redact(str(exc))))
        _say("")

    if result.error is not None:
        _say(
            "  STOPPED: re-reading the version table after pass %d failed, so the\n"
            "  versions listed above were deleted and the rest were not tried.\n"
            "      %s: %s"
            % (len(result.passes), type(result.error).__name__, redact(str(result.error)))
        )
        _say("")
    elif not result.converged:
        _say(
            "  INCOMPLETE: stopped at the %d-pass limit with work still\n"
            "  outstanding. Re-run to continue, or raise MAX_PASSES." % MAX_PASSES
        )
        _say("")

    if result.undated:
        _say(
            "  UNDATED: %d matching version(s) have no readable creation time, so\n"
            "  they were not assessed. The run exits 1." % result.undated
        )
        _say("")

    verb = "deleted" if applied else "would be deleted"
    if exported_at is not None:
        _say(
            "  %d version(s) %s across %d pass(es), per the snapshot exported at %s."
            % (total, verb, len(result.passes), format_stamp(exported_at))
        )
        _say("  A snapshot deletes nothing. To act on this plan, run against the live\n"
              "  workspace, which re-reads the version table first.")
    else:
        _say(
            "  %d version(s) %s across %d pass(es)."
            % (total, verb, len(result.passes))
        )
        if not applied:
            _say("  Re-run with --apply to perform the deletions.")
    _say("")


def print_export(workspace, out_path, snapshot, text, applied, undated):
    header = "EXPORT - writing the snapshot" if applied else "EXPORT DRY RUN - nothing is written"
    _say("")
    _say("gdbprune %s  [%s]" % (__version__, header))
    _say("  workspace      : %s" % redact(workspace))
    _say("  output         : %s" % redact(out_path))
    _say("  exported at    : %s" % snapshot["exported_at"])
    _say("  versions read  : %d" % len(snapshot["versions"]))
    _say("  undated        : %d version(s) matching the pattern with no readable"
         " creation time, written null" % undated)
    _say("  fields written : %s per version; no connection details" % ", ".join(VERSION_FIELDS))
    _say("")
    if applied:
        _say("  wrote %d byte(s) to %s" % (len(text), redact(out_path)))
    else:
        _say("  Re-run with --apply to write the file.")
    _say("  The export holds the whole version table: filters apply when it is read.")
    _say("")


# --------------------------------------------------------------------------
# Self-test - pure Python, no arcpy, no network, no credentials. The arcpy
# paths run against an in-memory stand-in module; see _FakeGdb.
# --------------------------------------------------------------------------


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


class _FakeGdb(object):
    """A stand-in arcpy module over an in-memory SDE_versions table.

    rows are [name, parent_name, owner, creation_time], the column order
    fetch_versions() selects. DeleteVersion removes the row it names, so a
    re-read after an applied pass sees the smaller tree, as a database would.
    """

    def __init__(self, rows, exists=True, locked=(), replicas=()):
        self.rows = [list(r) for r in rows]
        self.replicas = replicas
        self.sql = []
        self.deleted = []
        self.answer = lambda: [list(r) for r in self.rows]
        gdb = self

        class _SQL(object):
            def __init__(self, workspace):
                gdb.sql.append(("connect", workspace))

            def execute(self, statement):
                gdb.sql.append(("execute", statement))
                return gdb.answer()

        def delete_version(workspace, name):
            if name in locked:
                # Quotes the workspace, as a real DeleteVersion error can.
                raise RuntimeError("version is locked by another session on %s" % workspace)
            gdb.deleted.append(name)
            gdb.rows = [r for r in gdb.rows
                        if qualified_name({"name": r[0], "owner": r[2]}) != name]

        self.module = types.ModuleType("arcpy")
        self.module.Exists = lambda path: exists
        self.module.ArcSDESQLExecute = _SQL
        self.module.management = types.SimpleNamespace(DeleteVersion=delete_version)

        def list_replicas(workspace, all_replicas=False):
            # Replica objects, which Esri returns whatever all_replicas says:
            # geodatabase replicas and feature service replicas on
            # traditionally versioned data. The call is recorded so that the
            # self-test can pin all_replicas=True.
            gdb.sql.append(("replicas", all_replicas))
            if isinstance(gdb.replicas, Exception):
                raise gdb.replicas
            return [types.SimpleNamespace(version=v) for v in gdb.replicas]

        self.module.da = types.SimpleNamespace(ListReplicas=list_replicas)


def _with_module(name, module, fn):
    """Run fn() with sys.modules[name] set to `module` (None poisons the
    import, so `import name` raises ModuleNotFoundError), then restore."""
    had = name in sys.modules
    old = sys.modules.get(name)
    sys.modules[name] = module
    try:
        return fn()
    finally:
        if had:
            sys.modules[name] = old
        else:
            del sys.modules[name]


def _run_cli(argv, arcpy_module=None, encoding=None):
    """main(argv) with stdout and stderr captured. Returns (code, output);
    code is main's return value, or SystemExit's code (2 for an argparse
    usage error). With `encoding` the streams encode strictly, as a stdout
    redirected to a file does on Windows (cp1252)."""
    if encoding is None:
        out = io.StringIO()
    else:
        out = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
        out.getvalue = lambda: (out.flush(), out.buffer.getvalue().decode(encoding))[1]
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = out

    def call():
        try:
            return main(argv)
        except SystemExit as exc:
            return exc.code

    try:
        code = _with_module("arcpy", arcpy_module, call)
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue()


def _footer(total, failed):
    """The self-test verdict: (exit code, lines to print)."""
    if failed:
        return 1, (["%d assertions, %d failed" % (total, len(failed))]
                   + ["  FAILED: %s" % label for label in failed])
    return 0, ["%d assertions, 0 failed" % total]


def self_test():
    passed = [0]
    failed = []
    # The arcpy entry as the caller left it: absent, or the real one in the
    # ArcGIS Pro Python window. Every stand-in must leave it so.
    arcpy_at_start = sys.modules.get("arcpy")

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, exc_type=ValueError):
        try:
            fn()
        except exc_type:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

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

    # ---- the pinned defect -------------------------------------------------
    # SYNC_B matches the pattern and is old enough, but has a live child.
    # The naive one-pass selector takes it. This selector must not.
    naive = [v["name"] for v in _naive_select(chain, M, CUTOFF)]
    ours = [v["name"] for v in select_candidates(chain, M, CUTOFF)]
    check("SYNC_B" in naive, "naive one-pass selector DOES select the pinned parent SYNC_B")
    check("SYNC_B" not in ours,
          "an old matching version with a live child is never selected  <-- pinned defect")
    check("SYNC_A" in naive, "naive one-pass selector also selects the root SYNC_A")
    check("SYNC_A" not in ours, "a root with a live child is never selected  <-- pinned defect")
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
          "a timezone-aware creation_time is compared without raising TypeError"
          "  <-- pinned defect")
    check(select_candidates(tz_new, M, CUTOFF) == [],
          "a timezone-aware version inside the window is still preserved")

    # "Strictly older than the cutoff": a version created exactly at the
    # cutoff stays, one microsecond earlier goes.
    at_cut = [{"name": "SYNC_EDGE", "parent": "DEFAULT", "owner": "gisowner", "created": CUTOFF}]
    check(select_candidates(at_cut, M, CUTOFF) == [],
          "a version created exactly at the cutoff is not old enough  <-- pinned defect")
    check(len(select_candidates(at_cut, M, CUTOFF + timedelta(microseconds=1))) == 1,
          "a version created one microsecond before the cutoff is old enough")

    # An aware time is compared as LOCAL wall clock, not as its own wall
    # clock. +13:47 is an offset no time zone uses, so on every host the two
    # readings differ, and the cutoff is set right at the local reading.
    ODD = timezone(timedelta(hours=13, minutes=47))
    odd_created = datetime(2025, 12, 20, 12, 0, 0, tzinfo=ODD)
    odd_local = odd_created.astimezone().replace(tzinfo=None)
    odd = [{"name": "SYNC_ODD", "parent": "DEFAULT", "owner": "gisowner", "created": odd_created}]
    check(odd_local != odd_created.replace(tzinfo=None)
          and select_candidates(odd, M, odd_local) == []
          and len(select_candidates(odd, M, odd_local + timedelta(microseconds=1))) == 1,
          "an aware creation time is compared as this machine's local wall clock"
          "  <-- pinned defect")

    # Windows raises OSError from astimezone() before 1970 and after 3000;
    # Linux does not. A 1900 sentinel used to crash the plan on Windows only.
    y1900 = datetime(1900, 1, 1, tzinfo=timezone.utc)
    old_row = [{"name": "SYNC_1900", "parent": "DEFAULT", "owner": "", "created": y1900},
               {"name": "DEFAULT", "parent": None, "owner": "sde", "created": None}]
    check(len(select_candidates(old_row, M, CUTOFF)) == 1
          and abs(local_naive(y1900) - datetime(1900, 1, 1)) < timedelta(hours=15)
          and parse_snapshot(dump_snapshot(build_snapshot(old_row, NOW)))[1][0]["created"]
          == local_naive(y1900),
          "an aware 1900 creation time is planned and exported, not an OSError"
          "  <-- pinned defect")
    year1 = [{"name": "SYNC_Y1", "parent": "DEFAULT", "owner": "",
              "created": datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))}]
    check(select_candidates(year1, M, CUTOFF) == []
          and build_snapshot(year1, NOW)["versions"][0]["created"] is None,
          "an aware time that leaves the datetime range is unknown: never pruned, written null")

    # A creation time returned as text. The old reader cut the text to the
    # format's length, which dropped an offset after a fraction and kept
    # the wall clock: hours off, toward deletion.
    check(_coerce_datetime("2025-01-02 03:04:05.123456 -10:00") is None
          and _coerce_datetime("2025-01-02 03:04:05.1234567 -10:00") is None
          and _coerce_datetime("2025-01-02 03:04:05 -10:00") is None,
          "a text time with an offset is unknown, with or without a fraction  <-- pinned defect")
    check(_coerce_datetime("2025-01-02 03:04:05.1234567") == datetime(2025, 1, 2, 3, 4, 5, 123457)
          and _coerce_datetime("2025-01-02 03:04:05.1234560") == datetime(2025, 1, 2, 3, 4, 5, 123456)
          and _coerce_datetime("2025-01-02T03:04:05.25") == datetime(2025, 1, 2, 3, 4, 5, 250000),
          "a seven-digit fraction rounds up to the microsecond, never toward deletion"
          "  <-- pinned defect")
    check(_coerce_datetime("2025-01-02 03:04:05.12345678") is None
          and _coerce_datetime("2025-01-02 03:04:05.123456789") is None,
          "a text time with eight or nine fraction digits is unknown, never pruned")
    check(_coerce_datetime("2025-02-30") is None
          and _coerce_datetime("9999-12-31 23:59:59.9999999") is None,
          "a text date that does not exist or leaves the datetime range is unknown")

    # A --prune-days so large the cutoff leaves the datetime range is a
    # refusal, not an OverflowError traceback.
    raises(lambda: compute_cutoff(NOW, 1000000),
           "a --prune-days reaching before the year 1 is refused  <-- pinned defect")
    raises(lambda: compute_cutoff(NOW, 10 ** 20),
           "a 21-digit --prune-days is refused, not an int overflow  <-- pinned defect")
    raises(lambda: prune(lambda: [], None, "%SYNC%", 1000000, now=NOW),
           "prune() itself refuses a --prune-days out of the datetime range")
    check(compute_cutoff(NOW, 7) == CUTOFF and compute_cutoff(NOW, 0) == NOW,
          "the cutoff is now minus --prune-days days")

    # Leafness compares the unqualified, unquoted name on BOTH sides. A
    # name or parent that carries its owner, or quotes, used to make a
    # parent with a live child look like a leaf.
    def qual_case(parent_row, child_parent):
        rows = [dict(parent_row), _v("SYNC_KID", parent=child_parent, age_days=1)]
        return [qualified_name(v) for v in select_candidates(rows, M, CUTOFF)]

    check(qual_case(_v("gisowner.SYNC_P", parent="DEFAULT"), "SYNC_P") == [],
          "an owner-qualified name with a live child is not a leaf  <-- pinned defect")
    check(qual_case(_v("SYNC_P", parent="DEFAULT"), "gisowner.SYNC_P") == [],
          "an owner-qualified parent pins its bare-named parent  <-- pinned defect")
    check(qual_case(_v("SYNC_P", parent="DEFAULT"), '"SYNC_P"') == [],
          "a quoted parent pins its unquoted parent  <-- pinned defect")
    check(qual_case(_v("SYNC_P", parent="DEFAULT"), '"gisowner"."sync_p"') == [],
          "a quoted, qualified, lower-case parent still pins its parent")
    check(qual_case(_v("SYNC_P", parent="DEFAULT"), '"CORP\\jane.doe".SYNC_P') == [],
          "a parent whose owner holds a dot still pins its parent")
    check(qual_case(_v("SYNC_P", parent="DEFAULT"), "SYNC_OTHER") == ["gisowner.SYNC_P"],
          "a parent named by nobody is still a leaf")
    check(node_key(' "sde"."DEFAULT" ') == "DEFAULT" and node_key(None) == "",
          "node_key strips the owner, the quotes and the case")

    # The negative-prune_days guard belongs to the core, not just the CLI:
    # a negative value pushes the cutoff into the future and makes versions
    # created yesterday eligible.
    fresh = [_v("SYNC_YESTERDAY", parent="DEFAULT", age_days=1)]
    raises(lambda: prune(lambda: list(fresh), None, "%SYNC%", -30, now=NOW),
           "prune() itself refuses a negative --prune-days, not just the CLI"
           "  <-- pinned defect")
    raises(lambda: prune(lambda: list(fresh), None, "%SYNC%", -1, now=NOW),
           "prune() refuses --prune-days -1, the boundary of the guard  <-- pinned defect")

    # Two owners can each hold a version called SYNC_X. The young
    # crew2.SYNC_X pins SYNC_P; planning the old crew1.SYNC_X used to drop
    # both from the dry-run tree, so SYNC_P showed as "would be deleted"
    # although --apply never deletes it.
    twins = [_v("DEFAULT", owner="sde", age_days=900),
             _v("SYNC_P", parent="DEFAULT"),
             _v("SYNC_X", parent="SYNC_P", owner="crew2", age_days=0),
             _v("SYNC_X", parent="DEFAULT", owner="crew1")]
    twin_plan = prune(lambda: list(twins), None, "%SYNC%", 7, now=NOW)
    check([[qualified_name(v) for v in p] for p in twin_plan.passes] == [["crew1.SYNC_X"]],
          "the dry run removes a version by owner and name, not by name alone"
          "  <-- pinned defect")
    # Same-named versions share leafness, so they are candidates in the same
    # pass. crew2.SYNC_X therefore shows up only on the re-read, as it
    # would when another session changes the table between passes.
    twin_table = [_v("SYNC_X", parent="DEFAULT", owner="crew1"), _v("SYNC_OK", parent="DEFAULT")]
    twin_calls = []

    def crew1_locked(v):
        twin_calls.append(qualified_name(v))
        if v["owner"] == "crew1":
            raise RuntimeError("locked")
        twin_table[:] = [x for x in twin_table if x is not v]
        if v["name"] == "SYNC_OK":
            twin_table.append(_v("SYNC_X", parent="DEFAULT", owner="crew2"))

    twin_res = prune(lambda: list(twin_table), crew1_locked, "%SYNC%", 7, now=NOW)
    check(twin_calls == ["crew1.SYNC_X", "gisowner.SYNC_OK", "crew2.SYNC_X"]
          and len(twin_res.failures) == 1,
          "a refused crew1.SYNC_X does not skip a crew2.SYNC_X  <-- pinned defect")

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
    check(picked == [], "a child that does not match the pattern still pins its parent"
                        "  <-- pinned defect")

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
    check(dry.failures == [], "without --apply prune() never attempts a delete call"
                              "  <-- pinned defect")
    check(len(dry.passes) == 3, "the dry run still walks the full 3-pass plan in memory")
    check([v["name"] for p in dry.passes for v in p] == ["SYNC_C", "SYNC_B", "SYNC_A"],
          "the dry-run plan matches the order --apply would use")

    f5, d5, c5, _l5 = _world(chain)
    applied = prune(f5, d5, "%SYNC%", 7, now=NOW)
    check(len(c5) == 3, "with --apply the callback is invoked once per real candidate")
    check(len(c5) == sum(len(p) for p in applied.passes),
          "callback invocations equal the number of planned deletions")

    # ---- pattern rejects a raw single quote ------------------------------
    raises(lambda: compile_like_pattern("%SY'NC%"),
           "--prune-pattern containing a single quote raises PatternError", PatternError)
    raises(lambda: build_candidate_where("%'; DROP--", 7),
           "where-clause builder raises rather than emitting broken SQL", PatternError)

    check("SYNC" in build_candidate_where("%SYNC%", 7), "a clean pattern builds a where clause")
    check("-7" in build_candidate_where("%SYNC%", 7), "the where clause carries the day threshold")

    raises(lambda: compile_like_pattern(""), "an empty --prune-pattern is refused", PatternError)

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

    picked = [qualified_name(v) for v in select_candidates(scoped, M, CUTOFF)]
    check(picked == ['"DOMAIN\\jsmith".SYNC_THREE', "gisowner.SYNC_ONE", "gisowner.SYNC_TWO"],
          "a pass lists its versions sorted by qualified name, whatever the table order")

    picked = [v["name"] for v in select_candidates(scoped, M, CUTOFF, None)]
    check(len(picked) == 3, "no --only-versions means no scoping")

    # --only-versions "" in a wrapper script (an unset variable) must scope
    # to nothing. Falling back to "no scoping" would silently widen the run
    # to every matching version.
    check(select_candidates(scoped, M, CUTOFF, []) == [],
          "an EMPTY --only-versions scopes to nothing, it does not disable scoping"
          "  <-- pinned defect")
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
          "a DEFAULT returned owner-qualified by the driver is never a candidate"
          "  <-- pinned defect")

    f6, d6, c6, live6 = _world(default_only)
    prune(f6, d6, "%", 7, now=NOW)
    check(c6 == [], "a full run never asks the database to delete DEFAULT")
    check(len(live6) == 1, "DEFAULT survives a wide-open prune")

    # ---- replica anchors ---------------------------------------------------
    # SYNC_SEND / SYNC_RECEIVE versions are replica system versions. The
    # default %SYNC% pattern matches them, and in a chain of anchors every
    # NEWER anchor is a leaf, so a leaf-first prune used to delete exactly
    # those. Replica ids 7 and 9 are synthetic.
    anchor_tree = [
        _v("DEFAULT", owner="sde", age_days=900),
        _v("SYNC_SEND_7_0", parent="DEFAULT", owner="sde"),
        _v("SYNC_SEND_7_1", parent="SYNC_SEND_7_0", owner="sde"),
        _v("SYNC_SEND_7_2", parent="SYNC_SEND_7_1", owner="sde"),
        _v("SYNC_RECEIVE_7_3", parent="DEFAULT", owner="sde"),
        _v("gisowner.sync_send_9_1", parent="DEFAULT", owner=""),
        _v("SYNC_FIELD_01", parent="DEFAULT"),
    ]
    naive_anchor = [v["name"] for v in _naive_select(anchor_tree, M, CUTOFF)]
    anchor_plan = prune(lambda: list(anchor_tree), None, "%SYNC%", 7, now=NOW)
    check("SYNC_SEND_7_2" in naive_anchor
          and [[v["name"] for v in p] for p in anchor_plan.passes] == [["SYNC_FIELD_01"]],
          "the default %SYNC% pattern never selects a SYNC_SEND or SYNC_RECEIVE anchor"
          "  <-- pinned defect")
    check(anchor_plan.anchors == 5 and anchor_plan.converged is True,
          "the plan counts the 5 matching anchors it holds back")
    fa, da, ca, live_a = _world(anchor_tree)
    prune(fa, da, "%", 0, now=NOW)
    check(ca == ["gisowner.SYNC_FIELD_01"] and len(live_a) == 6,
          "a wide-open --apply never asks the database to delete an anchor  <-- pinned defect")
    allowed = prune(lambda: list(anchor_tree), None, "%SYNC%", 7, now=NOW, allow_anchors=True)
    check([[v["name"] for v in p] for p in allowed.passes]
          == [["SYNC_FIELD_01", "gisowner.sync_send_9_1", "SYNC_RECEIVE_7_3", "SYNC_SEND_7_2"],
              ["SYNC_SEND_7_1"], ["SYNC_SEND_7_0"]] and allowed.anchors == 5,
          "with allow_anchors the anchors are candidates, still leaf first")
    check(prune(lambda: list(anchor_tree), None, "SYNC_SEND%", 7, now=NOW).passes == []
          and select_candidates(anchor_tree, M, CUTOFF, ["SYNC_SEND_7_2"]) == [],
          "an anchor pattern or an anchor named in --only-versions still selects nothing"
          "  <-- pinned defect")
    pinned_by_anchor = [_v("SYNC_P", parent="DEFAULT"),
                        _v("SYNC_SEND_7_5", parent="SYNC_P", owner="sde")]
    check(select_candidates(pinned_by_anchor, M, CUTOFF) == [],
          "a held-back anchor still pins its parent")
    check(is_replica_anchor({"name": '"sde"."sync_receive_4_2"'})
          and is_replica_anchor({"name": "SYNCXSEND_1"})
          and is_replica_anchor({"name": "SYNC_RECEIVE_REC_12_3"})
          and not is_replica_anchor({"name": "SYNC_SENT_1"})
          and not is_replica_anchor({"name": "MY_SYNC_SEND_1"})
          and not is_replica_anchor({"name": None}),
          "the anchor test is by unqualified name, case-insensitive, with LIKE wildcards")
    # A version a registered replica uses: an offline map's replica version
    # takes the user and service names, so "%SYNC%" matches FieldSync's.
    in_use = [_v("DEFAULT", owner="sde", age_days=900),
              dict(_v("crew_FieldSync_1404578882000", parent="DEFAULT"), replica=True),
              dict(_v("SYNC_HELD_P", parent="DEFAULT"), replica=True),
              _v("SYNC_UNDER", parent="SYNC_HELD_P"),
              _v("SYNC_FREE", parent="DEFAULT")]
    in_use_plan = prune(lambda: list(in_use), None, "%", 0, now=NOW, allow_anchors=True)
    check([[v["name"] for v in p] for p in in_use_plan.passes] == [["SYNC_FREE", "SYNC_UNDER"]]
          and in_use_plan.replicas == 2,
          "a version a registered replica uses is never selected, even with the anchor flag"
          "  <-- pinned defect")
    check(prune(lambda: list(in_use), None, "%SYNC%", 7, ["SYNC_HELD_P", "SYNC_FREE"],
                now=NOW).replicas == 1
          and count_replicas(in_use, compile_like_pattern("crew%")) == 1
          and count_undated([dict(in_use[1], created=None)], M) == 0,
          "the replica-in-use count follows the pattern and the scope, and is not undated")
    check(count_anchors(anchor_tree, compile_like_pattern("SYNC_RECEIVE%")) == 1
          and count_anchors(anchor_tree, M, ["SYNC_SEND_7_1"]) == 1
          and count_anchors(anchor_tree, M, []) == 0,
          "the anchor count follows the pattern and the scope")
    undated_anchor = [{"name": "SYNC_SEND_7_9", "parent": "DEFAULT", "owner": "", "created": None}]
    check(count_undated(undated_anchor, M) == 0
          and count_undated(undated_anchor, M, allow_anchors=True) == 1,
          "an undated anchor counts as undated only when anchors are allowed")
    undated_tree = [_v("DEFAULT", owner="sde")] + undated_anchor
    check(prune(lambda: list(undated_tree), None, "%SYNC%", 7, now=NOW).undated == 0
          and prune(lambda: list(undated_tree), None, "%SYNC%", 7, now=NOW,
                    allow_anchors=True).undated == 1
          and prune(lambda: list(anchor_tree), None, "%SYNC%", 7, ["SYNC_SEND_7_1"],
                    now=NOW).anchors == 1,
          "prune() passes the anchor flag to undated and the scope to the anchor count"
          "  <-- pinned defect")
    check("NOT LIKE 'SYNC_SEND%'" in build_candidate_where("%SYNC%", 7)
          and "NOT LIKE 'SYNC_RECEIVE%'" in build_candidate_where("%SYNC%", 7)
          and "NOT LIKE" not in build_candidate_where("%SYNC%", 7, allow_anchors=True),
          "the printed candidate rule names the held-back anchors")

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
    check(res13.converged is False, "hitting max_passes is reported as a NON-converged run"
                                    "  <-- pinned defect")
    check(len(res13.passes) == 3, "the run really did stop at the pass limit")
    check(len(live13) == 7, "a truncated run leaves the un-pruned versions behind")
    check(res13.failures == [], "a truncated run has no failures to signal with")

    f14, d14, _c14, live14 = _world(deep)
    res14 = prune(f14, d14, "%SYNC%", 7, now=NOW, max_passes=100)
    check(res14.converged is True, "the same tree under a sufficient limit converges")
    check(live14 == [], "the converged run emptied the chain")
    # A chain exactly max_passes deep empties on the last allowed pass, so the
    # loop never runs the empty pass that proves it; only the post-loop
    # recompute calls it converged.
    f14b, d14b, _c14b, live14b = _world(deep[:3])
    res14b = prune(f14b, d14b, "%SYNC%", 7, now=NOW, max_passes=3)
    check(res14b.converged is True and len(res14b.passes) == 3 and live14b == [],
          "a chain exactly max_passes deep that empties on the last pass converges"
          "  <-- pinned defect")

    # ---- empty input -------------------------------------------------------
    res12 = prune(lambda: [], None, "%SYNC%", 7, now=NOW)
    check(res12.passes == [], "an empty version table produces an empty plan")
    check(res12.versions_seen == 0, "an empty version table reports zero versions read")
    check(res12.converged is True, "an empty version table counts as converged")

    # ---- undated: the count that tells an unreadable table from a clean one
    y1 = datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    undated_rows = [
        {"name": "DEFAULT", "parent": None, "owner": "sde", "created": None},
        {"name": "SYNC_U1", "parent": "DEFAULT", "owner": "gisowner", "created": None},
        {"name": "SYNC_U2", "parent": "DEFAULT", "owner": "gisowner", "created": y1},
        {"name": "MANUAL_U", "parent": "DEFAULT", "owner": "gisowner", "created": None},
        {"name": "SYNC_OUT", "parent": "DEFAULT", "owner": "gisowner", "created": None},
        _v("SYNC_DATED", parent="DEFAULT"),
    ]
    res_u = prune(lambda: list(undated_rows), None, "%SYNC%", 7,
                  only_versions=["SYNC_U1", "SYNC_U2", "SYNC_DATED"], now=NOW)
    check(res_u.undated == 2 and [[v["name"] for v in p] for p in res_u.passes] == [["SYNC_DATED"]],
          "undated counts the matching, in-scope versions it cannot date, and no others"
          "  <-- pinned defect")
    check(count_undated(undated_rows, M) == 3,
          "an aware creation time that leaves the datetime range counts as undated")

    # ---- a re-read that fails after an applied pass -----------------------
    stop_table = [_v("SYNC_A", parent="DEFAULT"), _v("SYNC_B", parent="SYNC_A")]
    reads = [0]

    def flaky_fetch():
        reads[0] += 1
        if reads[0] > 1:
            raise RuntimeError("connection lost")
        return list(stop_table)

    res_s = prune(flaky_fetch, lambda v: None, "%SYNC%", 7, now=NOW)
    check(res_s.converged is False and str(res_s.error) == "connection lost"
          and [[v["name"] for v in p] for p in res_s.passes] == [["SYNC_B"]],
          "a re-read that fails after deletes returns them, unconverged, with the error"
          "  <-- pinned defect")

    # ======================================================================
    # 1.1.0: core edges the 1.0.0 run never reached
    # ======================================================================
    raises(lambda: compile_like_pattern(None), "a missing --prune-pattern is refused", PatternError)
    raises(lambda: build_candidate_where("%SYNC%", -1),
           "the where-clause builder refuses a negative --prune-days")
    lone_lock = [_v("SYNC_ONLY", parent="DEFAULT")]

    def always_locked(v):
        raise RuntimeError("locked")

    res15 = prune(lambda: list(lone_lock), always_locked, "%SYNC%", 7, now=NOW)
    check(res15.passes == [] and len(res15.failures) == 1,
          "a run where every candidate refuses stops after one attempt, with the failure recorded")
    check(res15.converged is True,
          "a run where every candidate refuses is finished, not cut off at the pass limit")

    # ======================================================================
    # Snapshot schema: build, dump, parse
    # ======================================================================
    snap_versions = [
        {"name": "DEFAULT", "parent": None, "owner": "sde", "created": datetime(2020, 1, 1),
         "replica": False},
        {"name": "SYNC_A", "parent": "DEFAULT", "owner": "gisowner",
         "created": datetime(2025, 6, 1, 8, 30, 0, 250000), "replica": False},
        {"name": "SYNC_B", "parent": "SYNC_A", "owner": "DOMAIN\\jsmith", "created": None,
         "replica": True},
    ]
    stamp = datetime(2026, 1, 1, 9, 30, 0)
    built = build_snapshot(snap_versions, stamp)
    text = dump_snapshot(built)
    got_at, got = parse_snapshot(text)
    check(got == snap_versions and got_at == stamp,
          "a snapshot round-trips every version field and the export time exactly")
    check(list(built) == list(SNAPSHOT_FIELDS)
          and all(list(r) == list(VERSION_FIELDS) for r in built["versions"]),
          "the snapshot carries exactly the documented fields at both levels")
    check(built["exported_at"] == "2026-01-01T09:30:00",
          "the export time is written as YYYY-MM-DDTHH:MM:SS")
    check(built["versions"][1]["created"] == "2025-06-01T08:30:00.250000",
          "sub-second creation times are kept, not truncated toward deletion")
    check(all(ord(ch) < 128 for ch in dump_snapshot(build_snapshot(
        [{"name": "SYNC_\u00e9", "parent": None, "owner": "", "created": None}], stamp))),
        "the written text is pure ASCII even for a non-ASCII version name")
    leaky = [{"name": "SYNC_L", "parent": None, "owner": "gisowner", "created": None,
              "password": "hunter2", "server": "db-host-01"}]
    leak_text = dump_snapshot(build_snapshot(leaky, stamp))
    check("hunter2" not in leak_text and "db-host-01" not in leak_text,
          "a field outside the five version fields is never copied into the file"
          "  <-- pinned defect")
    # +13:47 again: in the author's own zone the old UTC-5 fixture read the
    # same under the right and the wrong conversion, so it could not fail.
    aware = [{"name": "SYNC_TZ", "parent": None, "owner": "", "created": odd_created},
             {"name": "DEFAULT", "parent": None, "owner": "sde", "created": None}]
    aware_row = build_snapshot(aware, stamp)["versions"][0]["created"]
    check(parse_snapshot(dump_snapshot(build_snapshot(aware, stamp)))[1][0]["created"]
          == odd_local and aware_row == odd_local.isoformat()
          and aware_row != odd_created.replace(tzinfo=None).isoformat()
          and "+" not in aware_row,
          "an aware creation time is written as local wall clock with no offset"
          "  <-- pinned defect")
    check(format_stamp(datetime(2025, 1, 2, 3, 4, 5)) == "2025-01-02T03:04:05",
          "a naive creation time is written exactly as the database returned it")
    check(build_snapshot([{"name": "SYNC_E", "parent": "", "owner": None, "created": None}],
                         stamp)["versions"][0] == {"name": "SYNC_E", "parent": None,
                                                   "owner": "", "created": None,
                                                   "replica": False},
          "an empty parent is written as null and a missing owner as empty text")
    check(parse_stamp("2026-01-01T00:00:00.000001", "t") == datetime(2026, 1, 1, 0, 0, 0, 1),
          "a six-digit fraction parses to microseconds")

    def mutated(fn):
        doc = json.loads(text)
        fn(doc)
        return json.dumps(doc)

    def refuses(doc_text, label, needle=None):
        try:
            parse_snapshot(doc_text)
        except SnapshotError as exc:
            check(needle is None or needle in str(exc), label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (accepted)" % label)

    def set_top(key, value):
        return mutated(lambda d: d.__setitem__(key, value))

    def set_row(i, key, value):
        return mutated(lambda d: d["versions"][i].__setitem__(key, value))

    refuses(mutated(lambda d: d.pop("exported_at")),
            "a snapshot with no export time is refused  <-- pinned defect", "exported_at")
    refuses(set_top("server", "db-host-01"),
            "an unknown top-level field is refused, not ignored", "unknown field(s): server")
    refuses(set_row(1, "extra", 1), "an unknown version field is refused", "versions[1]")
    refuses(mutated(lambda d: d["versions"][0].pop("owner")),
            "a version with a missing field is refused", "missing field(s): owner")
    refuses(set_top("format", "something-else"), "a file of another format is refused", "format")
    refuses(set_top("schema_version", 3), "an unknown schema version is refused", "schema_version")
    refuses(set_top("schema_version", 1),
            "a schema 1 snapshot, which records no replica versions, is refused"
            "  <-- pinned defect", "Export the table again")
    raises(lambda: parse_snapshot(set_top("schema_version", 1)),
           "a schema 1 snapshot raises the old-schema error, which an export may replace", OldSchemaError)
    refuses(set_row(1, "replica", "false"),
            "a replica flag given as text is refused, not read as true  <-- pinned defect",
            "versions[1].replica")
    refuses(set_row(1, "replica", 0), "a replica flag given as 0 is refused", "versions[1].replica")
    refuses(mutated(lambda d: d["versions"][1].pop("replica")),
            "a version with no replica flag is refused", "missing field(s): replica")
    refuses(set_top("schema_version", True),
            "schema_version true is refused although True == 1  <-- pinned defect")
    refuses(set_top("schema_version", 2.0),
            "schema_version 2.0 is refused although 2.0 == 2  <-- pinned defect")
    refuses(set_top("schema_version", "1"), "a schema_version given as text is refused")
    refuses(set_top("exported_at", None), "a null export time is refused")
    refuses(set_top("exported_at", "2026-01-01 09:30:00"),
            "a space in place of the T is refused  <-- pinned defect")
    refuses(set_top("exported_at", "2026-01-01T09:30:00+00:00"),
            "a timestamp carrying an offset is refused")
    refuses(set_top("exported_at", "2026-01-01"), "a date without a time is refused")
    refuses(set_top("exported_at", "2026-02-30T09:30:00"),
            "a date that does not exist is refused", "not a real date")
    refuses(set_top("exported_at", "2026-01-01T09:30:00\n"),
            "a timestamp with a trailing newline is refused  <-- pinned defect")
    refuses(set_top("exported_at", "\uff12\uff10\uff12\uff16-01-01T09:30:00"),
            "non-ASCII digits in a timestamp are refused  <-- pinned defect")
    refuses(set_top("exported_at", "2026-01-01T09:30:00.250"),
            "a fraction that is not six digits is refused")
    refuses(set_row(1, "created", 20250601), "a creation time given as a number is refused",
            "versions[1].created")
    refuses(set_row(1, "name", ""), "an empty version name is refused", "versions[1].name")
    refuses(set_row(1, "name", " SYNC_A"), "a version name with surrounding space is refused")
    refuses(set_row(1, "name", 7), "a version name that is not text is refused")
    refuses(set_row(1, "parent", ""), "an empty-text parent is refused; null means none")
    refuses(set_row(1, "owner", None), "a null owner is refused; empty text means none")
    refuses(set_row(1, "owner", "gisowner "), "an owner with surrounding space is refused")
    refuses(set_row(2, "owner", "DOMAIN\x0cieldcrew"),  # JSON text DOMAIN\fieldcrew
            "a control character inside an owner is refused  <-- pinned defect",
            "versions[2].owner")
    refuses(set_row(1, "name", "SYNC\x00A"), "a control character inside a name is refused")
    refuses(set_row(1, "parent", "DEF\x7fAULT"), "a control character inside a parent is refused")
    refuses(set_row(2, "owner", "DOMAIN\u0085x"),
            "a C1 control character (NEL) inside an owner is refused  <-- pinned defect",
            "versions[2].owner")
    refuses(set_row(1, "name", "SYNC\u200bA"),
            "a zero-width space inside a name is refused  <-- pinned defect", "versions[1].name")
    refuses(set_row(1, "name", "SYNC_\ud800"),
            "a lone surrogate inside a name is refused  <-- pinned defect", "versions[1].name")
    refuses(set_row(1, "parent", "\udc80DEFAULT"),
            "a lone surrogate inside a parent is refused", "versions[1].parent")
    refuses(set_row(2, "owner", "DOMAIN\u00a0x"),
            "a no-break space inside an owner is refused", "versions[2].owner")
    check(parse_snapshot(set_row(2, "owner", "DOMAIN\\\u0141ukasz"))[1][2]["owner"]
          == "DOMAIN\\\u0141ukasz"
          and parse_snapshot(set_row(1, "name", "SYNC \u4e2d"))[1][1]["name"] == "SYNC \u4e2d",
          "printable non-ASCII text and an inner space are accepted")
    case_pair = parse_snapshot(mutated(
        lambda d: d["versions"].append(dict(d["versions"][1], name="sync_a"))))[1]
    check([v["name"] for v in case_pair] == ["DEFAULT", "SYNC_A", "SYNC_B", "sync_a"],
          "SYNC_A and sync_a are read as two versions, as the live mode reads them"
          "  <-- pinned defect")
    refuses(mutated(lambda d: d["versions"].pop(0)),
            "a snapshot with no DEFAULT version is refused  <-- pinned defect", "no DEFAULT")
    refuses(set_top("versions", []),
            "a snapshot with no versions at all is refused  <-- pinned defect", "no DEFAULT")
    doc_example = re.search(r"SNAPSHOT SCHEMA[^\n]*\n(.*?)\n\s*Exactly", __doc__, re.S).group(1)
    check(len(parse_snapshot(doc_example)[1]) == 2,
          "the schema example in this docstring is a snapshot the reader accepts"
          "  <-- pinned defect")
    check(len(parse_snapshot(set_row(0, "name", "sde.DEFAULT"))[1]) == 3,
          "an owner-qualified DEFAULT counts as DEFAULT")
    refuses(set_row(1, "created", ""), "an empty-text creation time is refused, not read as null"
            "  <-- pinned defect", "versions[1].created")
    refuses(set_row(1, "created", False), "a false creation time is refused, not read as null",
            "versions[1].created")
    refuses(set_row(1, "created", 0), "a zero creation time is refused, not read as null",
            "versions[1].created")
    refuses(text.replace('"versions": [', '"versions": [' + "[" * 100000 + "]" * 100000 + ",", 1),
            "a file nested 100000 levels deep is refused, not a traceback  <-- pinned defect",
            "not valid JSON")
    check(parse_snapshot(set_row(1, "owner", ""))[1][1]["owner"] == "",
          "an empty owner is still accepted")
    refuses(mutated(lambda d: d["versions"].append(dict(d["versions"][1]))),
            "the same version listed twice is refused", "appears twice")
    refuses(text.replace('"format": "gdbprune-versions",',
                         '"format": "gdbprune-versions", "format": "gdbprune-versions",'),
            "a field repeated inside one object is refused  <-- pinned defect", "twice")
    refuses(text.replace('"schema_version": 2', '"schema_version": NaN'),
            "a NaN in the file is refused", "NaN")
    refuses("[]", "a top-level list is refused", "expected an object")
    refuses(set_top("versions", None),
            "a versions field that is not a list is refused by name  <-- pinned defect",
            "versions: expected a list, got NoneType")
    refuses(mutated(lambda d: d["versions"].append("SYNC_Z")),
            "a version entry that is not an object is refused", "versions[3]")
    refuses(text[:-10], "a truncated file is refused as invalid JSON", "not valid JSON")
    check(describe_age(datetime(2026, 1, 1), datetime(2026, 1, 4, 5, 0, 0))
          == "3 day(s) 5 hour(s) before this run", "the snapshot age is given in days and hours")
    check("check both clocks" in describe_age(datetime(2026, 1, 2), datetime(2026, 1, 1)),
          "a snapshot dated after this machine's clock is called out")
    check(describe_age(datetime(2026, 1, 1), datetime(2026, 1, 1))
          == "0 day(s) 0 hour(s) before this run",
          "a snapshot read the moment it was written is not called out")

    # ======================================================================
    # The CLI, end to end. arcpy is poisoned where a mode must not touch it,
    # and replaced by _FakeGdb where a mode needs it.
    # ======================================================================
    tmp = tempfile.mkdtemp(prefix="gdbprune-selftest-")
    # The operator's own workspace variable is set aside for the CLI checks
    # and put back afterwards, whatever happens.
    env_before = {ENV_WORKSPACE: os.environ[ENV_WORKSPACE]} if ENV_WORKSPACE in os.environ else {}
    os.environ.pop(ENV_WORKSPACE, None)

    def put(name, content, encoding="utf-8"):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding=encoding, newline="\n") as fh:
            fh.write(content)
        return path

    try:
        real_now = datetime.now().replace(microsecond=0)
        live_rows = [
            ["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
            ["SYNC_A", "DEFAULT", "gisowner", "2025-01-01 12:00:00"],
            ["SYNC_B", "SYNC_A", "gisowner", "2025-01-02 12:00:00.500000"],
            ["SYNC_C", "SYNC_B", "gisowner", datetime(2025, 1, 3, 12, 0, 0)],
            ["SYNC_FRESH", "DEFAULT", "gisowner", real_now - timedelta(days=1)],
            ["MANUAL", "DEFAULT", "gisowner", "2025-01-01"],
        ]
        chain_snapshot = dump_snapshot(build_snapshot(
            _versions_from_rows(live_rows), real_now - timedelta(days=3)))
        snap_path = put("chain.json", chain_snapshot)

        # --from-versions: read-only, no arcpy
        code, out = _run_cli(["--from-versions", snap_path], None)
        stamp_text = format_stamp(real_now - timedelta(days=3))
        check(code == 0 and "pass 3  -  1 leaf version(s)" in out and "gisowner.SYNC_A" in out,
              "--from-versions plans the full leaf-first chain from a file")
        check(code == 0, "--from-versions runs with arcpy unimportable  <-- pinned defect")
        check(out.count(stamp_text) == 2 and "exported at    : %s" % stamp_text in out
              and "per the snapshot exported at %s." % stamp_text in out,
              "the export time is printed in the header and in the verdict")
        check("3 day(s) 0 hour(s) before this run" in out, "the plan says how old the snapshot is")
        check("DRY RUN - nothing is deleted" in out and "A snapshot deletes nothing" in out
              and "Re-run with --apply" not in out,
              "a snapshot plan never suggests --apply as the next step")
        check("gisowner.SYNC_FRESH" not in out and "gisowner.MANUAL" not in out,
              "the snapshot plan keeps the young and the non-matching versions")
        shown = re.search(r"older than     : 7 day\(s\)  \(created before (.{19})\)", out)
        check(shown is not None and abs((datetime.strptime(shown.group(1), "%Y-%m-%d %H:%M:%S")
                                         - (datetime.now() - timedelta(days=7))).total_seconds())
              < 120, "the snapshot plan prints the cutoff it used, --prune-days before now")

        code, out = _run_cli(["--from-versions", snap_path, "--apply"], None)
        check(code == 2 and "refuses --apply" in out,
              "--from-versions refuses --apply: a snapshot cannot delete  <-- pinned defect")
        code, out = _run_cli(["--from-versions", snap_path, "--workspace", "conn"], None)
        check(code == 2 and "Drop --workspace" in out,
              "--from-versions refuses a --workspace it would not use")
        os.environ[ENV_WORKSPACE] = "from-the-environment"
        code, out = _run_cli(["--from-versions", snap_path], None)
        os.environ.pop(ENV_WORKSPACE, None)
        check(code == 0, "a workspace in the environment does not block --from-versions")
        code, out = _run_cli(["--from-versions", snap_path, "--export-versions", "x.json"], None)
        check(code == 2 and "not allowed with argument" in out,
              "--from-versions and --export-versions cannot be combined")
        code, out = _run_cli(["--from-versions", os.path.join(tmp, "missing.json")], None)
        check(code == 2 and "error: cannot read" in out,
              "a missing snapshot file is an error, not an empty plan")
        bad_path = put("bad.json", set_row(1, "created", "2025-06-01"))
        code, out = _run_cli(["--from-versions", bad_path], None)
        check(code == 2 and "bad.json is not a valid gdbprune snapshot" in out
              and "versions[1].created" in out,
              "an invalid snapshot names the file and the field it failed on")
        latin_path = os.path.join(tmp, "latin.json")
        with open(latin_path, "wb") as fh:
            fh.write(b'{"format": "gdbprune-versions\xe9"}')
        code, out = _run_cli(["--from-versions", latin_path], None)
        check(code == 2 and "not UTF-8 text" in out,
              "a snapshot that is not UTF-8 is refused")
        bom_path = put("bom.json", "\ufeff" + chain_snapshot, encoding="utf-8")
        code, bom_out = _run_cli(["--from-versions", bom_path], None)
        check(code == 0 and "gisowner.SYNC_A" in bom_out,
              "a snapshot saved with a UTF-8 BOM reads the same as one without")
        code, out = _run_cli(["--from-versions", snap_path, "--only-versions", ""], None)
        check(code == 0 and "nothing to prune" in out and "(empty - nothing is eligible)" in out
              and "per the snapshot exported at %s." % stamp_text in out,
              "an empty --only-versions plans nothing, and the verdict still carries the export time")
        code, out = _run_cli(["--from-versions", snap_path, "--prune-days", "-1"], None)
        check(code == 2 and "must not be negative" in out,
              "a negative --prune-days is refused before the snapshot is read")
        code, out = _run_cli(["--from-versions", snap_path, "--prune-pattern", "%'x"], None)
        check(code == 2 and "single quote" in out,
              "a quoted --prune-pattern is refused before the snapshot is read")
        deep_rows = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"]] + [
            ["SYNC_D%03d" % i, ("SYNC_D%03d" % (i - 1)) if i else "DEFAULT",
             "gisowner", "2025-01-01 00:00:00"] for i in range(MAX_PASSES + 1)]
        deep_path = put("deep.json", dump_snapshot(build_snapshot(
            _versions_from_rows(deep_rows), real_now)))
        code, out = _run_cli(["--from-versions", deep_path], None)
        check(code == 1 and "INCOMPLETE" in out
              and "per the snapshot exported at %s." % format_stamp(real_now) in out,
              "a snapshot deeper than the pass limit exits 1 as INCOMPLETE")
        future_path = put("future.json", dump_snapshot(build_snapshot(
            _versions_from_rows(live_rows), real_now + timedelta(days=2))))
        code, out = _run_cli(["--from-versions", future_path], None)
        check(code == 0 and "check both clocks" in out,
              "a snapshot from the future is read, and the clock mismatch is printed")
        for days in ("1000000", "99999999999999999999"):
            code, out = _run_cli(["--from-versions", snap_path, "--prune-days", days], None)
            check(code == 2 and "before the year 1" in out and "Traceback" not in out,
                  "--prune-days %s is refused with exit 2, not an overflow traceback"
                  "  <-- pinned defect" % (days[:3] + "..."))
        nest_path = put("nest.json", '{"format": "gdbprune-versions", "schema_version": 2, '
                        '"exported_at": "2026-01-01T00:00:00", "versions": '
                        + "[" * 100000 + "]" * 100000 + "}")
        code, out = _run_cli(["--from-versions", nest_path], None)
        check(code == 2 and "nest.json is not a valid gdbprune snapshot" in out,
              "a deeply nested snapshot is refused with exit 2, not a traceback  <-- pinned defect")
        sur_path = put("sur.json", set_row(1, "name", "SYNC_\ud800"))
        code, out = _run_cli(["--from-versions", sur_path], None, encoding="utf-8")
        check(code == 2 and "versions[1].name" in out and "\\ud800" in out,
              "a lone surrogate in a snapshot is refused by field, not a print crash"
              "  <-- pinned defect")
        wide_rows = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                     ["SYNC_\u4e2d", "DEFAULT", "DOMAIN\\\u0141ukasz", "2025-01-01 00:00:00"]]
        wide_path = put("wide.json", dump_snapshot(build_snapshot(
            _versions_from_rows(wide_rows), real_now)))
        code, out = _run_cli(["--from-versions", wide_path], None, encoding="cp1252")
        check(code == 0 and '"DOMAIN\\\\u0141ukasz".SYNC_\\u4e2d' in out
              and "1 version(s) would be deleted" in out,
              "a non-cp1252 name prints escaped to a cp1252 stdout, not a crash  <-- pinned defect")

        # --export-versions: arcpy, writes only under --apply
        out_path = os.path.join(tmp, "export.json")
        secret_ws = "SERVER=db-host-01;USER=sde_admin;PASSWORD=hunter2"
        gdb = _FakeGdb(live_rows)
        code, out_dry = _run_cli(["--export-versions", out_path, "--workspace", secret_ws],
                                 gdb.module)
        check(code == 0 and not os.path.exists(out_path) and "EXPORT DRY RUN" in out_dry,
              "--export-versions without --apply writes nothing  <-- pinned defect")
        check("versions read  : 6" in out_dry, "the export dry run says how many versions it read")
        code, out = _run_cli(["--export-versions", out_path, "--workspace", secret_ws,
                              "--apply"], gdb.module)
        with open(out_path, "r", encoding="ascii") as fh:
            written = fh.read()
        exported_at, exported = parse_snapshot(written)
        check(code == 0 and len(exported) == 6 and "wrote %d byte(s)" % len(written) in out,
              "--export-versions --apply writes a snapshot of every version")
        check(not any(s in written for s in ("db-host-01", "sde_admin", "hunter2", "SERVER",
                                             "PASSWORD", secret_ws)),
              "the written file holds no connection string, host, connecting user or password"
              "  <-- pinned defect")
        check(not any(s in out_dry + out for s in ("db-host-01", "sde_admin", "hunter2"))
              and "SERVER=***;USER=***;PASSWORD=***" in out,
              "the export prints the workspace with its values hidden  <-- pinned defect")
        # The owner column holds database accounts, and the account that
        # connects is often one of them: the README says so, and this pins it.
        sde_ws = "SERVER=db-host-01;USER=sde;PASSWORD=hunter2"
        code, out = _run_cli(["--export-versions", out_path, "--workspace", sde_ws, "--apply"],
                             _FakeGdb(live_rows).module)
        with open(out_path, "r", encoding="ascii") as fh:
            sde_written = fh.read()
        check(code == 0 and '"owner": "sde"' in sde_written
              and not any(s in sde_written + out for s in ("db-host-01", "hunter2", "USER=sde")),
              "an owner that is also the connecting user is written; host and password are not")
        check(code == 0 and "wrote" in out,
              "an export overwrites an earlier snapshot at the same path")
        sde_file = put("admin.sde", "SDE-CONNECTION-BYTES")
        # The same file spelled another way passes the string-equality refusal in run_export().
        code, out = _run_cli(["--export-versions", sde_file, "--workspace",
                              os.path.join(tmp, ".", "admin.sde"), "--apply"],
                             _FakeGdb(live_rows).module)
        with open(sde_file, "r", encoding="utf-8") as fh:
            sde_after = fh.read()
        check(code == 2 and "is not a gdbprune snapshot" in out
              and sde_after == "SDE-CONNECTION-BYTES",
              "an export never overwrites a file that is not a snapshot, such as the .sde"
              "  <-- pinned defect")
        code, out = _run_cli(["--export-versions", tmp, "--workspace", "conn", "--apply"],
                             _FakeGdb(live_rows).module)
        check(code == 2 and "is not a gdbprune snapshot" in out and os.path.isdir(tmp),
              "an export never replaces a folder")
        link, target = os.path.join(tmp, "link.json"), os.path.join(tmp, "target.json")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):  # pragma: no cover
            # ponytail: Windows without Developer Mode cannot create a symlink.
            # The README names the one assertion this skips.
            print("SKIP  an export never writes through a dangling symlink (no symlink rights)")
        else:
            code, out = _run_cli(["--export-versions", link, "--workspace", "conn", "--apply"],
                                 _FakeGdb(live_rows).module)
            check(code == 2 and not os.path.exists(target),
                  "an export never writes through a dangling symlink  <-- pinned defect")
        sur_out = os.path.join(tmp, "sur-export.json")
        code, out = _run_cli(["--export-versions", sur_out, "--workspace", "conn", "--apply"],
                             _FakeGdb(live_rows + [["SYNC_\ud800", "DEFAULT", "gisowner",
                                                    "2025-01-01 12:00:00"]]).module)
        check(code == 2 and "would not read back" in out and not os.path.exists(sur_out),
              "an export never writes a name --from-versions would refuse  <-- pinned defect")
        check(abs((datetime.now() - exported_at).total_seconds()) < 120
              and re.search(r'"exported_at": "[0-9-]{10}T[0-9:]{8}"', written) is not None,
              "the written file records the export time, to the whole second")
        check(gdb.deleted == [], "an export deletes nothing")
        check(("execute", "SELECT name, parent_name, owner, creation_time FROM sde.SDE_versions")
              in gdb.sql, "the export reads the whole version table with no WHERE clause")

        # The snapshot plan equals the live plan for the same table.
        code_live, out_live = _run_cli(["--workspace", "conn"], _FakeGdb(live_rows).module)
        code_snap, out_snap = _run_cli(["--from-versions", out_path], None)

        def pass_lines(s):
            return [ln for ln in s.splitlines() if ln.startswith("  pass ") or ln.startswith("      ")]

        check(code_live == 0 and code_snap == 0 and pass_lines(out_live) == pass_lines(out_snap)
              and len(pass_lines(out_live)) == 6,
              "an exported snapshot plans exactly what the live dry run plans")

        code, out = _run_cli(["--export-versions", out_path], _FakeGdb(live_rows).module)
        check(code == 2 and "--workspace is required" in out,
              "--export-versions needs a workspace")
        code, out = _run_cli(["--export-versions", out_path, "--workspace", "conn",
                              "--only-versions", "SYNC_A"], _FakeGdb(live_rows).module)
        check(code == 2 and "always holds the whole version table" in out,
              "--export-versions refuses --only-versions rather than ignoring it")
        nowhere = os.path.join(tmp, "no-such-dir", "export.json")
        code, out = _run_cli(["--export-versions", nowhere, "--workspace", "conn", "--apply"],
                             _FakeGdb(live_rows).module)
        check(code == 2 and "error: cannot write" in out,
              "an unwritable --export-versions path is an error")

        # A FILE path is shown redacted like the workspace: "$WS" typed where
        # a snapshot path belongs is a connection string.
        secret_ws = os.path.join(tmp, "SERVER=db-host-01;UID=sde_admin;PWD=hunter2")
        code, out = _run_cli(["--export-versions", secret_ws, "--workspace", secret_ws,
                              "--apply"], _FakeGdb(live_rows).module)
        check(code == 2 and "names the workspace itself" in out and "hunter2" not in out
              and not os.path.lexists(secret_ws),
              "an export to the workspace's own connection string is refused, and no file"
              " is named after it  <-- pinned defect")
        secret_out = os.path.join(tmp, "PWD=hunter2.json")
        code, out_dry = _run_cli(["--export-versions", secret_out, "--workspace", "conn"],
                                 _FakeGdb(live_rows).module)
        code_w, out_w = _run_cli(["--export-versions", secret_out, "--workspace", "conn",
                                  "--apply"], _FakeGdb(live_rows).module)
        code_r, out_r = _run_cli(["--from-versions", secret_out], None)
        check(code == code_w == code_r == 0 and "PWD=***" in out_dry + out_w + out_r
              and "hunter2" not in out_dry + out_w + out_r,
              "the export and snapshot paths print redacted like the workspace  <-- pinned defect")
        secret_dir = os.path.join(tmp, "PWD=hunter2.d")
        os.mkdir(secret_dir)
        for argv, needle, label in (
                (["--from-versions", os.path.join(tmp, "PWD=hunter2-missing.json")],
                 "cannot read", "a missing snapshot"),
                (["--from-versions", put("PWD=hunter2-bad.json", "[]")],
                 "is not a valid gdbprune snapshot", "an invalid snapshot"),
                (["--export-versions", secret_dir, "--workspace", "conn", "--apply"],
                 "is not a gdbprune snapshot", "an export path that is not a snapshot"),
                (["--export-versions", os.path.join(secret_dir, "no", "x.json"),
                  "--workspace", "conn", "--apply"],
                 "cannot write", "an unwritable export path")):
            code, out = _run_cli(argv, _FakeGdb(live_rows).module)
            check(code == 2 and needle in out and "hunter2" not in out,
                  "%s is reported with its path redacted  <-- pinned defect" % label)
        fmt_path = put("fmt.json", '{"format": "PWD=hunter2", "schema_version": 2, '
                       '"exported_at": "2026-01-01T00:00:00", "versions": []}')
        code, out = _run_cli(["--from-versions", fmt_path], None)
        check(code == 2 and "is not a valid gdbprune snapshot" in out and "hunter2" not in out,
              "a KEY=value field quoted from an invalid snapshot is masked  <-- pinned defect")
        code, out = _run_cli(["--workspce", "SERVER=db-host-01;UID=sde_admin;PWD=hunter2"])
        check(code == 2 and "unrecognized arguments: --workspce SERVER=***;UID=***;PWD=***" in out
              and "hunter2" not in out,
              "argparse's usage error redacts a connection string it echoes  <-- pinned defect")
        dup_path = os.path.join(tmp, "dup.json")
        code, out = _run_cli(["--export-versions", dup_path, "--workspace", "conn", "--apply"],
                             _FakeGdb(live_rows + [live_rows[1]]).module)
        check(code == 2 and "appears twice" in out and not os.path.exists(dup_path),
              "an export its own reader would refuse is not written")
        empty_path = os.path.join(tmp, "empty.json")
        for answer in (True, None, []):
            gdb = _FakeGdb([])
            gdb.answer = lambda answer=answer: answer
            code, out = _run_cli(["--export-versions", empty_path, "--workspace", "conn",
                                  "--apply"], gdb.module)
            check(code == 2 and "no DEFAULT version" in out and not os.path.exists(empty_path),
                  "an export of a %r query result is refused, not written as an empty table"
                  "  <-- pinned defect" % (answer,))
        code, out = _run_cli(["--export-versions", empty_path, "--workspace", "conn", "--apply"],
                             _FakeGdb(live_rows[1:]).module)
        check(code == 2 and "(5 row(s) read)" in out and not os.path.exists(empty_path),
              "an export of a table without DEFAULT is refused")
        empty_snap = put("nodefault.json", set_top("versions", []))
        code, out = _run_cli(["--from-versions", empty_snap], None)
        check(code == 2 and "no DEFAULT version" in out and "nothing to prune" not in out,
              "--from-versions refuses an empty snapshot rather than plan it clean"
              "  <-- pinned defect")
        case_rows = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                     ["SYNC_A", "DEFAULT", "gis", "2020-01-01 00:00:00"],
                     ["sync_a", "DEFAULT", "gis", "2020-01-01 00:00:00"]]
        case_path = os.path.join(tmp, "case.json")
        code_x, out_x = _run_cli(["--export-versions", case_path, "--workspace", "conn",
                                  "--apply"], _FakeGdb(case_rows).module)
        code_live, out_live = _run_cli(["--workspace", "conn"], _FakeGdb(case_rows).module)
        code_snap, out_snap = _run_cli(["--from-versions", case_path], None)
        check(code_x == code_live == code_snap == 0
              and pass_lines(out_live) == pass_lines(out_snap)
              == ["  pass 1  -  2 leaf version(s)", "      gis.SYNC_A", "      gis.sync_a"],
              "a table holding SYNC_A and sync_a exports, and plans as the live mode plans it"
              "  <-- pinned defect")
        undated_live = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                        ["SYNC_A", "DEFAULT", "gisowner", "01/02/2025 10:00:00 AM"],
                        ["SYNC_B", "DEFAULT", "gisowner", "2025-01-02 03:04:05 -05:00"],
                        ["MANUAL", "DEFAULT", "gisowner", "junk"]]
        code, out = _run_cli(["--export-versions", empty_path, "--workspace", "conn"],
                             _FakeGdb(undated_live).module)
        check(code == 0 and "undated        : 2 version(s) matching the pattern" in out,
              "the export counts the matching versions it writes with a null creation time")

        # live mode against the stand-in arcpy
        gdb = _FakeGdb(live_rows)
        code, out = _run_cli(["--workspace", "conn"], gdb.module)
        check(code == 0 and gdb.deleted == [] and "DRY RUN" in out
              and "Re-run with --apply" in out,
              "the live plan mode deletes nothing and says how to apply")
        gdb = _FakeGdb(live_rows)
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 0 and gdb.deleted == ["gisowner.SYNC_C", "gisowner.SYNC_B",
                                            "gisowner.SYNC_A"]
              and "APPLY - versions are being deleted" in out and "DRY RUN" not in out,
              "--apply deletes through DeleteVersion strictly leaf to root, under an APPLY header")
        check(len([s for s in gdb.sql if s[0] == "execute"]) == 4,
              "--apply re-reads the version table after every pass")
        gdb = _FakeGdb(live_rows, locked=("gisowner.SYNC_C",))
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 1 and "1 failure(s):" in out and "version is locked" in out
              and gdb.deleted == [],
              "a locked leaf exits 1, is reported, and pins its whole chain")
        gdb = _FakeGdb(live_rows, locked=("gisowner.SYNC_C",))
        code, out = _run_cli(["--workspace", secret_ws, "--apply"], gdb.module)
        check(code == 1 and "version is locked" in out
              and not any(s in out for s in ("db-host-01", "sde_admin", "hunter2")),
              "a failure whose error quotes the workspace prints it with its values hidden"
              "  <-- pinned defect")
        gdb = _FakeGdb(live_rows)
        os.environ[ENV_WORKSPACE] = "env-conn"
        code, out = _run_cli([], gdb.module)
        check(code == 0 and ("connect", "env-conn") in gdb.sql,
              "the workspace falls back to the environment variable")
        gdb = _FakeGdb(live_rows)
        code, out = _run_cli(["--workspace", "flag-conn"], gdb.module)
        os.environ.pop(ENV_WORKSPACE, None)
        check(("connect", "flag-conn") in gdb.sql and ("connect", "env-conn") not in gdb.sql,
              "the --workspace flag wins over the environment variable")
        code, out = _run_cli([], _FakeGdb(live_rows).module)
        check(code == 1 and "--workspace is required" in out
              and "--from-versions" in out,
              "with no workspace anywhere the tool refuses and names the offline mode")
        gdb = _FakeGdb(live_rows)
        code, out = _run_cli(["--workspace", "conn", "--prune-pattern", "%'x", "--apply"],
                             gdb.module)
        check(code == 1 and gdb.sql == [],
              "a bad pattern is refused before the database is touched")
        code, out = _run_cli(["--workspace", "gone"], _FakeGdb(live_rows, exists=False).module)
        check(code == 1 and "does not exist" in out,
              "a workspace arcpy cannot see is refused")
        code, out = _run_cli(["--workspace", "conn"], None)
        check(code == 1 and "arcpy is not available" in out and PRO_PYTHON in out,
              "without arcpy a live run names the ArcGIS Pro interpreter")
        code, out = _run_cli(["--export-versions", out_path, "--workspace", "conn"], None)
        check(code == 2 and "arcpy is not available" in out,
              "--export-versions needs arcpy and says so")

        # live mode: credentials, names outside cp1252, qualified parents
        code, out = _run_cli(["--workspace", secret_ws], _FakeGdb(live_rows).module)
        check(code == 0 and "hunter2" not in out and "sde_admin" not in out
              and "db-host-01" not in out,
              "the live plan never prints the workspace's password, user or host"
              "  <-- pinned defect")
        code, out = _run_cli(["--workspace", secret_ws], _FakeGdb(live_rows, exists=False).module)
        check(code == 1 and "does not exist" in out and "hunter2" not in out,
              "the missing-workspace error hides the password too")
        gdb = _FakeGdb(wide_rows)
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module, encoding="cp1252")
        check(code == 0 and gdb.deleted == ['"DOMAIN\\\u0141ukasz".SYNC_\u4e2d']
              and "1 version(s) deleted across 1 pass(es)." in out,
              "an --apply that deletes a non-cp1252 name still reports what it deleted"
              "  <-- pinned defect")
        gdb = _FakeGdb([["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                        ["SYNC_P", "DEFAULT", "gisowner", "2025-01-01 00:00:00"],
                        ["SYNC_KID", "gisowner.SYNC_P", "gisowner", real_now]])
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 0 and gdb.deleted == [],
              "a live parent named owner-qualified by its child is never deleted"
              "  <-- pinned defect")
        gdb = _FakeGdb(live_rows)
        code, out = _run_cli(["--workspace", "conn", "--prune-days", "1000000", "--apply"],
                             gdb.module)
        check(code == 1 and gdb.sql == [] and "before the year 1" in out,
              "an out-of-range --prune-days is refused before the database is touched")

        # The live plan, the snapshot plan and --apply agree when two owners
        # share a version name.
        twin_rows = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                     ["SYNC_P", "DEFAULT", "gisowner", "2025-01-01 00:00:00"],
                     ["SYNC_X", "SYNC_P", "crew2", real_now - timedelta(hours=1)],
                     ["SYNC_X", "DEFAULT", "crew1", "2025-01-01 00:00:00"]]
        code_plan, out_plan = _run_cli(["--workspace", "conn"], _FakeGdb(twin_rows).module)
        twin_path = put("twins.json", dump_snapshot(build_snapshot(
            _versions_from_rows(twin_rows), real_now)))
        code_snap, out_snap = _run_cli(["--from-versions", twin_path], None)
        gdb = _FakeGdb(twin_rows)
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code_plan == code_snap == code == 0 and gdb.deleted == ["crew1.SYNC_X"]
              and pass_lines(out_plan) == pass_lines(out_snap) == pass_lines(out)
              == ["  pass 1  -  1 leaf version(s)", "      crew1.SYNC_X"],
              "with two owners of one name, both plans list exactly what --apply deletes"
              "  <-- pinned defect")

        # The live --apply exit code for a run cut off at the pass limit.
        gdb = _FakeGdb(deep_rows)
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 1 and "INCOMPLETE" in out and len(gdb.deleted) == MAX_PASSES
              and len(gdb.rows) == 2,
              "an --apply cut off at the pass limit exits 1, not 0  <-- pinned defect")

        # An arcpy error is "could not run" (2), not "left work" (1), and its
        # text is redacted like every other message.
        gdb = _FakeGdb(live_rows)

        def unreachable():
            raise RuntimeError("Failed to connect to %s" % secret_ws)

        gdb.answer = unreachable
        code, out = _run_cli(["--workspace", secret_ws], gdb.module)
        check(code == 1 and "error: RuntimeError: Failed to connect" in out
              and "hunter2" not in out and "Traceback" not in out,
              "an arcpy error exits 1 with its text redacted, not a traceback"
              "  <-- pinned defect")

        # A value in braces or quotes can hold ';'.
        check(redact("SERVER=db-host-01;UID=sde;PWD={hunter2;tail-of-secret}")
              == redact("SERVER=h;PASSWORD='p;w-secret'") == "(connection string hidden)"
              and redact(r"C:\conn\admin.sde") == r"C:\conn\admin.sde",
              "a braced or quoted connection value is hidden whole  <-- pinned defect")
        code, out = _run_cli(["--workspace", "SERVER=h;PWD={a;secret}"],
                             _FakeGdb(live_rows, exists=False).module)
        check(code == 1 and "does not exist" in out and "secret" not in out,
              "the missing-workspace error hides a braced password too")

        # A live-mode refusal exits 1, as 1.0.0's SystemExit("error: ...")
        # did; 2 is only for the two new snapshot modes and argparse.
        codes = [_run_cli(argv, None)[0] for argv in (
            ["--workspace", "x.sde"], [], ["--workspace", "x.sde", "--prune-pattern", "a'b"],
            ["--workspace", "x.sde", "--prune-days", "-1"])]
        check(codes == [1, 1, 1, 1],
              "a live-mode refusal exits 1, as in 1.0.0, not 2  <-- pinned defect")
        code, out = _run_cli(["--workspace", "x.sde", "--prune-days", "x"], None)
        check(code == 2 and "invalid int value" in out, "an argparse usage error exits 2")

        # A URL or an EZConnect string has no KEY=value form to mask.
        check(redact("dbuser/hunter2@dbhost:1521/ORCL") == "(connection string hidden)"
              and redact("postgresql://db-host-01:5432/gis") == "(connection string hidden)",
              "an EZConnect or URL workspace is hidden whole  <-- pinned defect")
        code, out = _run_cli(["--workspace", "dbuser/hunter2@dbhost:1521/ORCL"],
                             _FakeGdb(live_rows, exists=False).module)
        check(code == 1 and "does not exist" in out and "hunter2" not in out
              and "dbhost" not in out,
              "the missing-workspace error hides an EZConnect password and host")

        # A table read with no DEFAULT row is refused in the live mode too.
        gdb = _FakeGdb([])
        gdb.answer = lambda: True
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 1 and "no DEFAULT version" in out and "nothing to prune" not in out,
              "a live run over a query that returned no rows is refused, not planned clean"
              "  <-- pinned defect")

        # undated, end to end: the one line that tells this plan from a clean one
        code, out = _run_cli(["--workspace", "conn"], _FakeGdb(undated_live).module)
        check(code == 1 and "undated        : 2 matching version(s)" in out
              and "0 version(s) would be deleted" in out and "UNDATED: 2 matching" in out,
              "a table of unreadable creation times plans nothing, says 2 are undated and"
              " exits 1  <-- pinned defect")
        gdb = _FakeGdb(undated_live)
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 1 and gdb.deleted == [] and "UNDATED: 2 matching" in out,
              "an --apply that could date no matching version exits 1, not 0  <-- pinned defect")

        # A re-read that fails after an applied pass: exit 1, the deletes named.
        stop_rows = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                     ["SYNC_A", "DEFAULT", "gisowner", "2025-01-01 00:00:00"],
                     ["SYNC_B", "SYNC_A", "gisowner", "2025-01-01 00:00:00"]]
        gdb = _FakeGdb(stop_rows)
        first_read = gdb.answer
        gdb_reads = [0]

        def reread_fails():
            gdb_reads[0] += 1
            if gdb_reads[0] > 1:
                raise RuntimeError("lost connection to %s" % secret_ws)
            return first_read()

        gdb.answer = reread_fails
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 1 and gdb.deleted == ["gisowner.SYNC_B"]
              and "STOPPED: re-reading the version table after pass 1 failed" in out
              and "      gisowner.SYNC_B" in out and "1 version(s) deleted" in out
              and "hunter2" not in out,
              "an --apply whose re-read fails exits 1 and names what it deleted"
              "  <-- pinned defect")

        # The same stop when ListReplicas, not the table query, fails on the re-read.
        gdb = _FakeGdb(stop_rows)
        first_list = gdb.module.da.ListReplicas
        list_calls = [0]

        def relist_fails(workspace, all_replicas=False):
            list_calls[0] += 1
            if list_calls[0] > 1:
                raise RuntimeError("ListReplicas failed")
            return first_list(workspace, all_replicas)

        gdb.module.da.ListReplicas = relist_fails
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 1 and gdb.deleted == ["gisowner.SYNC_B"]
              and "STOPPED: re-reading the version table after pass 1 failed" in out
              and "      gisowner.SYNC_B" in out and "ListReplicas failed" in out,
              "an --apply whose replica re-list fails exits 1 and names what it deleted"
              "  <-- pinned defect")

        # A stdout that cannot be written after --apply has deleted: exit 1,
        # the count on stderr, and stdout muted so the exit flush cannot fail.
        class _FullStdout(io.StringIO):
            def write(self, text):
                raise OSError(28, "No space left on device")

        sink_path = os.path.join(tmp, "stdout.bin")
        sink = os.open(sink_path, os.O_RDWR | os.O_CREAT)
        try:
            for fileno in (None, sink):
                full, err = _FullStdout(), io.StringIO()
                if fileno is not None:
                    full.fileno = lambda fileno=fileno: fileno
                gdb = _FakeGdb(stop_rows)
                saved = sys.stdout, sys.stderr
                sys.stdout, sys.stderr = full, err
                try:
                    code = _with_module("arcpy", gdb.module,
                                        lambda: main(["--workspace", "conn", "--apply"]))
                finally:
                    sys.stdout, sys.stderr = saved
                check(code == 1 and len(gdb.deleted) == 2
                      and "2 version(s) had been deleted" in err.getvalue()
                      and "error: OSError" in err.getvalue(),
                      "an --apply whose report cannot be written exits 1 and counts its"
                      " deletes on stderr%s  <-- pinned defect"
                      % ("" if fileno is None else ", stdout muted"))
            os.write(sink, b"after")
        finally:
            os.close(sink)
        check(os.path.getsize(sink_path) == 0,
              "a stdout that failed is pointed at the null device")

        # A buffered stdout fails at the flush, not at the write. main()
        # flushes inside its handlers, so the run exits 2, not 120 at exit.
        class _FlushFails(io.StringIO):
            def flush(self):
                raise OSError(28, "No space left on device")

        late, err = _FlushFails(), io.StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = late, err
        try:
            code = main(["--from-versions", snap_path])
        finally:
            sys.stdout, sys.stderr = saved
        check(code == 2 and "error: OSError" in err.getvalue(),
              "a --from-versions plan whose stdout cannot be flushed exits 2")

        # fetch_versions: the shapes ArcSDESQLExecute can hand back
        def fetched(answer):
            gdb = _FakeGdb([])
            gdb.answer = lambda: answer
            return _with_module("arcpy", gdb.module, lambda: fetch_versions("conn"))

        for answer in (True, [], None):
            raises(lambda: fetched(answer),
                   "a %r query result is refused, not read as an empty table  <-- pinned defect"
                   % (answer,), ToolError)
        check([v["name"] for v in fetched(["DEFAULT", None, "sde", "2020-01-01"])]
              == ["DEFAULT"], "a single row returned flat is read as one version")
        odd = fetched([[None, "DEFAULT", "x", None], ["", "DEFAULT", "x", None],
                       ["  SYNC_S  ", "", None, "junk"], ["DEFAULT", None, "sde", None]])
        check(odd[0] == {"name": "SYNC_S", "parent": None, "owner": "", "created": None,
                         "replica": False}
              and len(odd) == 2,
              "a nameless row is skipped and blank fields normalise to none")
        when = datetime(2025, 1, 2, 3, 4, 5)
        check(_coerce_datetime("2025-01-02 03:04:05.500000") == when.replace(microsecond=500000)
              and _coerce_datetime("2025-01-02 03:04:05") == when
              and _coerce_datetime("2025-01-02T03:04:05") == when
              and _coerce_datetime("2025-01-02") == datetime(2025, 1, 2)
              and _coerce_datetime(when) is when
              and _coerce_datetime(None) is None and _coerce_datetime("junk") is None,
              "creation times read as datetime, text in four formats, or none")

        # replica anchors, end to end, and the abbreviation guard
        anchor_rows = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                       ["SYNC_SEND_7_0", "DEFAULT", "sde", "2025-01-01 00:00:00"],
                       ["SYNC_SEND_7_1", "SYNC_SEND_7_0", "sde", "2025-01-02 00:00:00"],
                       ["SYNC_RECEIVE_7_2", "DEFAULT", "sde", "2025-01-03 00:00:00"],
                       ["SYNC_FIELD_01", "DEFAULT", "gisowner", "2025-01-01 00:00:00"]]
        anchor_path = put("anchors.json", dump_snapshot(build_snapshot(
            _versions_from_rows(anchor_rows), real_now)))
        code, out = _run_cli(["--from-versions", anchor_path], None)
        check(code == 0 and pass_lines(out) == ["  pass 1  -  1 leaf version(s)",
                                                "      gisowner.SYNC_FIELD_01"]
              and "replica anchors: 3 matching SYNC_SEND/SYNC_RECEIVE version(s) held back" in out
              and "NOT LIKE 'SYNC_SEND%'" in out,
              "the default plan holds back the anchors and says how many  <-- pinned defect")
        code, out = _run_cli(["--from-versions", anchor_path, "--allow-replica-anchors"], None)
        check(code == 0 and "sde.SYNC_SEND_7_0" in out and "4 version(s) would be deleted" in out
              and "replica anchors: ALLOWED by --allow-replica-anchors; 3 matching" in out
              and "NOT LIKE" not in out,
              "--allow-replica-anchors plans the anchors and says it allowed them")
        gdb = _FakeGdb(anchor_rows)
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 0 and gdb.deleted == ["gisowner.SYNC_FIELD_01"],
              "a live --apply deletes no anchor without the flag  <-- pinned defect")
        gdb = _FakeGdb(anchor_rows)
        code, out = _run_cli(["--workspace", "conn", "--allow-replica-anchors"], gdb.module)
        check(code == 0 and gdb.deleted == [] and "sde.SYNC_SEND_7_0" in out,
              "a live plan with --allow-replica-anchors and no --apply deletes nothing"
              "  <-- pinned defect")
        gdb = _FakeGdb(anchor_rows, replicas=["SYNC_RECEIVE_7_2"])
        code, out = _run_cli(["--workspace", "conn", "--allow-replica-anchors"], gdb.module)
        check(code == 0 and "ALLOWED by --allow-replica-anchors; 2 matching" in out
              and "replica in use : 1 matching" in out and "SYNC_RECEIVE_7_2" not in out,
              "an anchor a replica uses is counted as in use, not as one the flag can prune"
              "  <-- pinned defect")
        gdb = _FakeGdb(anchor_rows)
        code, out = _run_cli(["--workspace", "conn", "--apply", "--allow-replica-anchors"],
                             gdb.module)
        check(code == 0 and gdb.deleted == ["gisowner.SYNC_FIELD_01", "sde.SYNC_RECEIVE_7_2",
                                            "sde.SYNC_SEND_7_1", "sde.SYNC_SEND_7_0"],
              "a live --apply with the flag deletes the anchors leaf first")
        gdb = _FakeGdb(anchor_rows)
        code, out = _run_cli(["--export-versions", os.path.join(tmp, "a.json"), "--workspace",
                              "conn", "--allow-replica-anchors"], gdb.module)
        check(code == 2 and "replica anchors included" in out and gdb.sql == [],
              "--export-versions refuses --allow-replica-anchors rather than ignoring it")
        code, out = _run_cli(["--from-versions", anchor_path, "--only-versions",
                              "SYNC_SEND_7_1,SYNC_FIELD_01"], None)
        check(code == 0 and "replica anchors: 1 matching" in out,
              "the plan's anchor count follows --only-versions  <-- pinned defect")
        undated_anchor_rows = anchor_rows + [["SYNC_SEND_8_0", "DEFAULT", "sde", None]]
        ua_path = put("undated-anchor.json", dump_snapshot(build_snapshot(
            _versions_from_rows(undated_anchor_rows), real_now)))
        code, out = _run_cli(["--from-versions", ua_path], None)
        code_f, out_f = _run_cli(["--from-versions", ua_path, "--allow-replica-anchors"], None)
        check(code == 0 and "undated        : 0" in out
              and code_f == 1 and "undated        : 1" in out_f and "UNDATED: 1" in out_f,
              "an undated anchor is undated, and exits 1, only under the flag  <-- pinned defect")

        # replica versions in use (arcpy.da.ListReplicas), end to end
        fs_name = "gisowner.crew_FieldSync_1404578882000"
        in_use_rows = [["DEFAULT", None, "sde", "2020-01-01 00:00:00"],
                       ["crew_FieldSync_1404578882000", "DEFAULT", "gisowner",
                        "2025-01-01 00:00:00"],
                       ["Esri_Anonymous_WaterSync", "DEFAULT", "sde", None],
                       ["SYNC_EDIT_9", "DEFAULT", "gisowner", "2025-01-01 00:00:00"],
                       ["SYNC_FIELD_02", "DEFAULT", "gisowner", "2025-01-01 00:00:00"]]

        def in_use_gdb():
            return _FakeGdb(in_use_rows, replicas=["GISOWNER.SYNC_EDIT_9", fs_name,
                                                   "sde.Esri_Anonymous_WaterSync"])

        gdb = in_use_gdb()
        code, out = _run_cli(["--workspace", "conn", "--apply", "--allow-replica-anchors"],
                             gdb.module)
        check(code == 0 and gdb.deleted == ["gisowner.SYNC_FIELD_02"]
              and "replica in use : 3 matching" in out and ("replicas", True) in gdb.sql,
              "a live --apply deletes no version an offline map or a replica uses, even with"
              " the flag  <-- pinned defect")
        gdb = in_use_gdb()
        gdb.replicas = RuntimeError("cannot list replicas on SERVER=db-host-01;PASSWORD=hunter2")
        code, out = _run_cli(["--workspace", "conn", "--apply"], gdb.module)
        check(code == 1 and gdb.deleted == [] and "hunter2" not in out
              and "cannot list replicas" in out,
              "a run that cannot list the replicas deletes nothing and exits 1  <-- pinned defect")
        in_use_path = put("in-use.json", set_top("schema_version", 1))
        gdb = in_use_gdb()
        code, out = _run_cli(["--export-versions", in_use_path, "--workspace", "conn",
                              "--apply"], gdb.module)
        check(code == 0 and [v["name"] for v in read_snapshot(in_use_path)[1] if v["replica"]]
              == ["crew_FieldSync_1404578882000", "Esri_Anonymous_WaterSync", "SYNC_EDIT_9"]
              and "undated        : 1 version(s)" in out,
              "the export marks the replica versions, counts an undated one, and replaces a"
              " schema 1 snapshot  <-- pinned defect")
        code, out = _run_cli(["--from-versions", in_use_path], None)
        check(code == 0 and pass_lines(out) == ["  pass 1  -  1 leaf version(s)",
                                                "      gisowner.SYNC_FIELD_02"]
              and "replica in use : 3 matching" in out,
              "a plan from the export holds back the replica versions too  <-- pinned defect")
        gdb = _FakeGdb(undated_anchor_rows + in_use_rows[2:3],
                       replicas=["sde.Esri_Anonymous_WaterSync"])
        code, out = _run_cli(["--export-versions", os.path.join(tmp, "ua.json"), "--workspace",
                              "conn"], gdb.module)
        check(code == 0 and "undated        : 2 version(s) matching" in out,
              "the export counts an undated anchor and an undated replica version"
              "  <-- pinned defect")

        for prefix in ("--ap", "--appl"):
            gdb = _FakeGdb(live_rows)
            code, out = _run_cli(["--workspace", "conn", prefix], gdb.module)
            check(code == 2 and "unrecognized arguments: %s" % prefix in out
                  and gdb.deleted == [] and gdb.sql == [],
                  "a prefix of --apply (%s) is refused, and nothing is read or deleted"
                  "  <-- pinned defect" % prefix)
        code, out = _run_cli(["--from-versions", anchor_path, "--allow"], None)
        check(code == 2 and "unrecognized arguments: --allow" in out,
              "a prefix of --allow-replica-anchors is refused  <-- pinned defect")
        # An unset shell variable gives an empty FILE. The mode is chosen by
        # "is not None", so "" still picks the snapshot mode, never the delete.
        gdb = _FakeGdb(live_rows)
        code, out = _run_cli(["--export-versions", "", "--apply", "--workspace", "conn"],
                             gdb.module)
        check(code == 2 and gdb.deleted == [],
              "--export-versions \"\" --apply is an export that fails, not a delete"
              "  <-- pinned defect")
        gdb = _FakeGdb(live_rows)
        os.environ[ENV_WORKSPACE] = "env-conn"
        code, out = _run_cli(["--from-versions", "", "--apply"], gdb.module)
        os.environ.pop(ENV_WORKSPACE, None)
        check(code == 2 and "refuses --apply" in out and gdb.deleted == [] and gdb.sql == [],
              "--from-versions \"\" --apply with the workspace variable set is refused,"
              " not a delete  <-- pinned defect")

        # importing the module runs nothing and never imports arcpy. Bytecode
        # is off for the probe: the loader would otherwise write __pycache__
        # next to the script, outside tmp. The check compares the cache
        # file's state, so it bites on a checkout that has no cache yet.
        here = os.path.abspath(__file__)
        cache = importlib.util.cache_from_source(here)

        def cache_state():
            return os.path.getmtime(cache) if os.path.exists(cache) else None

        cache_before = cache_state()
        spec = importlib.util.spec_from_file_location("gdbprune_import_probe", here)
        probe = importlib.util.module_from_spec(spec)
        bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            _with_module("arcpy", None, lambda: spec.loader.exec_module(probe))
        finally:
            sys.dont_write_bytecode = bytecode
        check(probe.__name__ == "gdbprune_import_probe" and callable(probe.main),
              "importing gdbprune runs nothing and imports no arcpy")
        check(cache_state() == cache_before,
              "the self-test writes no bytecode cache next to the script  <-- pinned defect")
        outer, inner = _FakeGdb([]).module, _FakeGdb([]).module
        seen = _with_module("arcpy", outer, lambda: (
            _with_module("arcpy", inner, lambda: sys.modules["arcpy"]), sys.modules["arcpy"]))
        check(seen == (inner, outer) and sys.modules.get("arcpy") is arcpy_at_start,
              "a stand-in arcpy is removed afterwards, and a nested one restores the outer"
              "  <-- pinned defect")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop(ENV_WORKSPACE, None)
        os.environ.update(env_before)

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass. The probe's own output is
    # swallowed so a green run prints no FAIL line.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
        refuses(text, "probe: a valid snapshot must not count as refused")
        refuses("[", "probe: the wrong message must fail", "no such text")
        refuses(None, "probe: an error that is not SnapshotError must fail")
    finally:
        sys.stdout = quiet
    probe_failed = failed[mark:]
    del failed[mark:]
    label = "check(), raises() and refuses() really do record a failure  <-- pinned defect"
    check(len(probe_failed) == 6, label)
    # The verdict cannot rest on check() alone: a check() that never records
    # a failure would pass its own probe. A short probe is a failure here.
    failed.extend([] if len(probe_failed) == 6 else ["%s (probe recorded %d of 6)"
                                                     % (label, len(probe_failed))])
    check(_footer(3, ["x"]) == (1, ["3 assertions, 1 failed", "  FAILED: x"]),
          "a failed assertion makes the self-test exit 1 and names it  <-- pinned defect")

    print("-" * 66)
    code, lines = _footer(passed[0] + len(failed), failed)
    for line in lines:
        print(line)
    # The exit code is taken from failed too, so a _footer() that always
    # returned 0 cannot turn a red run green.
    return 1 if failed else code


def _versions_from_rows(rows):
    """fetch_versions() over literal rows, for fixtures. Uses the real row
    handling, so a fixture cannot drift from what a live read produces."""
    gdb = _FakeGdb(rows)
    return _with_module("arcpy", gdb.module, lambda: fetch_versions("fixture"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse echoes a mistyped flag's arguments, and the next one is
    often the connection string. Its usage errors are redacted like the
    workspace; the exit code stays 2."""

    def error(self, message):
        argparse.ArgumentParser.error(self, redact(message))


def build_parser():
    # allow_abbrev=False: argparse would otherwise read --ap as --apply, so a
    # typed prefix would delete. A prefix is a usage error, exit 2.
    p = _Parser(
        prog="gdbprune",
        allow_abbrev=False,
        description=(
            "Delete stale leaf versions from a traditionally-versioned Enterprise "
            "geodatabase, printing a plan first. Live modes need ArcGIS Pro's Python, "
            "a traditionally-versioned geodatabase, and an admin .sde connection file. "
            "--from-versions and --self-test need neither."
        ),
        epilog=(
            "Config precedence: command-line flag > environment variable > built-in "
            "default. Only --workspace reads an environment variable (%s)." % ENV_WORKSPACE
        ),
    )
    p.add_argument(
        "--workspace",
        help="Path to an admin .sde connection file. Required for a live run and for "
             "--export-versions. Falls back to $%s; a non-empty flag wins." % ENV_WORKSPACE,
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
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--from-versions",
        metavar="FILE",
        help="Plan from a version snapshot instead of a live workspace. Read-only: "
             "refuses --apply and never imports arcpy.",
    )
    mode.add_argument(
        "--export-versions",
        metavar="FILE",
        help="Write the live version table to FILE as a snapshot. Needs arcpy and "
             "--workspace. Writes only under --apply.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete, or with --export-versions actually write. Omitted "
             "(the default) prints the plan and exits.",
    )
    p.add_argument(
        "--allow-replica-anchors",
        action="store_true",
        help="Let SYNC_SEND and SYNC_RECEIVE replica system versions be candidates. Off "
             "by default, because Esri says they should not be deleted by hand. A version "
             "that a registered replica uses is never a candidate.",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in assertions and exit. No arcpy, no network, no credentials.",
    )
    return p


def run_from_versions(args, now, cutoff):
    """Plan from a snapshot file. No arcpy, no database, no write."""
    if args.apply:
        raise ToolError(
            "--from-versions is a read-only dry run and refuses --apply. A snapshot "
            "can be stale; delete against the live workspace, which re-reads the table."
        )
    if args.workspace:
        raise ToolError("--from-versions reads a file, not a workspace. Drop --workspace.")
    try:
        exported_at, versions = read_snapshot(args.from_versions)
    except SnapshotError as exc:
        raise ToolError("%s is not a valid gdbprune snapshot: %s"
                         % (redact(args.from_versions), redact(str(exc))))
    except OSError as exc:
        # A FILE path is redacted like the workspace: "$WS" passed here by
        # mistake is a connection string, and OSError quotes it.
        raise ToolError("cannot read %s: %s" % (redact(args.from_versions), redact(str(exc))))

    result = prune(
        fetch=lambda: versions,
        delete=None,
        pattern=args.prune_pattern,
        prune_days=args.prune_days,
        only_versions=args.only_versions,
        now=now,
        allow_anchors=args.allow_replica_anchors,
    )
    print_plan(result, args, args.from_versions, cutoff, applied=False,
               exported_at=exported_at, now=now)
    return 1 if (not result.converged or result.undated) else 0


def run_export(args, workspace, now):
    """Write the live version table as a snapshot, under --apply only."""
    if args.only_versions is not None:
        raise ToolError(
            "--export-versions always holds the whole version table, because "
            "leafness needs every row. Pass --only-versions when you read the snapshot."
        )
    if args.allow_replica_anchors:
        raise ToolError(
            "--export-versions always writes every version, replica anchors included. "
            "Pass --allow-replica-anchors when you read the snapshot."
        )
    if args.export_versions == workspace:
        # The typo that repeats the workspace. A connection string is not a
        # file yet, so the overwrite guard below would not stop it, and the
        # file created would carry the password in its name.
        raise ToolError("--export-versions names the workspace itself; give a snapshot file.")
    versions = fetch_versions(workspace)
    snapshot = build_snapshot(versions, now.replace(microsecond=0))
    text = dump_snapshot(snapshot)
    try:
        parse_snapshot(text)  # never write a file --from-versions would refuse
    except SnapshotError as exc:
        raise ToolError("export refused, the snapshot would not read back: %s" % exc)
    if args.apply:
        # Overwrite only an earlier snapshot. A typo that repeats the
        # workspace path would otherwise truncate the admin .sde file the
        # export has just read from, and exit 0.
        if os.path.lexists(args.export_versions):
            try:
                read_snapshot(args.export_versions)
            except OldSchemaError:
                pass  # a 1.1.0 snapshot, replaced like any earlier one
            except (SnapshotError, OSError) as exc:
                raise ToolError("%s exists and is not a gdbprune snapshot, so it is not "
                                "overwritten (%s)"
                                % (redact(args.export_versions), redact(str(exc))))
        try:
            write_snapshot(args.export_versions, text)
        except OSError as exc:
            raise ToolError("cannot write %s: %s"
                            % (redact(args.export_versions), redact(str(exc))))
    # Every matching undated row it writes null, anchors and replica
    # versions included, whatever a later plan holds back.
    matcher = compile_like_pattern(args.prune_pattern)
    undated = sum(1 for v in versions
                  if _matches(v, matcher, None) and local_naive(v.get("created")) is None)
    print_export(workspace, args.export_versions, snapshot, text, bool(args.apply), undated)
    return 0


def main(argv=None):
    """Exit codes: 0 clean, 1 the run left work (a refused delete, the pass
    limit, a failed re-read after deletes). A command that cannot run exits
    1 in a live run, as 1.0.0 did, and 2 in the two snapshot modes, which
    are new. argparse exits 2 on a usage error."""
    args = build_parser().parse_args(argv)
    cannot_run = 2 if (args.from_versions is not None or args.export_versions is not None) else 1
    try:
        code = _main(args)
        # A full disk or a closed pipe fails here, inside the handlers
        # below, not in the flush at interpreter exit.
        sys.stdout.flush()
        return code
    except ToolError as exc:
        _say("error: %s" % exc, sys.stderr)
        return cannot_run
    except Exception as exc:
        # An arcpy error while connecting or reading the table, or a stdout
        # that cannot be written. The text is redacted: it can quote the
        # workspace.
        if isinstance(exc, OSError):
            _mute_stdout()
        _say("error: %s: %s" % (type(exc).__name__, redact(str(exc))), sys.stderr)
        return cannot_run


def _main(args):

    if args.self_test:
        return self_test()

    # An explicitly-passed --only-versions "" (an unset variable in a wrapper
    # script) must scope the run to NOTHING, not disable scoping entirely.
    if args.only_versions is not None:
        args.only_versions = [
            s.strip() for s in args.only_versions.split(",") if s.strip()
        ]

    # Validate the pattern and the day count before anything touches the
    # database or the snapshot.
    now = datetime.now()
    try:
        build_candidate_where(args.prune_pattern, args.prune_days, args.allow_replica_anchors)
        cutoff = compute_cutoff(now, args.prune_days)
    except ValueError as exc:  # PatternError is a ValueError
        raise ToolError(str(exc))

    if args.from_versions is not None:
        return run_from_versions(args, now, cutoff)

    # Config precedence: flag > environment variable > built-in default.
    workspace = args.workspace or os.environ.get(ENV_WORKSPACE)
    if not workspace:
        raise ToolError(
            "--workspace is required for a real run "
            "(or set $%s). Use --from-versions to plan from a snapshot, or "
            "--self-test to verify the tool without a database." % ENV_WORKSPACE
        )

    if args.export_versions is not None:
        return run_export(args, workspace, now)

    deleter = make_deleter(workspace) if args.apply else None

    result = prune(
        fetch=lambda: fetch_versions(workspace),
        delete=deleter,
        pattern=args.prune_pattern,
        prune_days=args.prune_days,
        only_versions=args.only_versions,
        now=now,
        allow_anchors=args.allow_replica_anchors,
    )

    try:
        print_plan(result, args, workspace, cutoff, applied=bool(args.apply))
        sys.stdout.flush()
    except OSError:
        # The deletes cannot be undone, and the report that names them is
        # lost. Say how many on stderr; main() then exits 1, never 0.
        _say("error: the report could not be written; %d version(s) had been deleted"
             % (sum(len(p) for p in result.passes) if args.apply else 0), sys.stderr)
        raise
    # undated: a matching version that could not be dated was not assessed,
    # so "nothing to prune" would be a guess. Exit 0 means every one was.
    return 1 if (result.failures or not result.converged or result.undated) else 0


if __name__ == "__main__":
    sys.exit(main())
