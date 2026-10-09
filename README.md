# gdbprune

Delete stale leaf versions from a traditionally-versioned Enterprise geodatabase, printing a plan first.

A nightly script creates an edit version for each field crew, named `SYNC_` and the date. A
supervisor reviews each one and posts what is wanted. Nobody deletes the versions afterwards.
Two years later the version table holds hundreds of them, and each one still references
database states that a compress cannot remove.

So somebody writes the obvious cleanup: select every `SYNC` version older than a week, then
delete each one. The first parent in that list fails, because the database refuses to drop a
version that still has a child. The script either stops halfway or swallows the error and moves
on. Either way the parents stay, and the next night adds another layer on top of them. The same
pattern also matches versions that replication still uses. Esri says not to delete the
`SYNC_SEND` and `SYNC_RECEIVE` versions by hand. An offline map's replica version is named
after the user and the feature service, so a service named `FieldSync` gives one that matches.
That version stays in use while the map is downloaded.

gdbprune deletes only the versions that are leaves right now, re-reads the version table, and
repeats until a pass finds nothing. It never deletes a version that a registered replica uses,
or a replica system version unless you name a flag. Without `--apply` it prints the plan and
deletes nothing.

**Who this is for.** A paid ArcGIS Enterprise install, a **traditionally-versioned**
geodatabase, and an admin `.sde` connection file. Branch-versioned shops get nothing
from this tool: branch versions live in a different system table and are removed
through a different API. If your data is branch-versioned, stop here.

**The existing alternative.** Esri's own Reconcile Versions tool
(`arcpy.management.ReconcileVersions`) can post each edit version to its target and then
delete it, with `with_post="POST"` and `with_delete="DELETE_VERSION"`. It does well what
gdbprune never does: it moves the edits into the target before the version goes. With its
default settings it does not keep every edit. For traditional versioning the defaults are
`conflict_resolution="FAVOR_TARGET_VERSION"` and `abort_if_conflicts="NO_ABORT"`. Esri's tool
page says "All conflicts will be resolved in favor of the target version", and that with
`NO_ABORT` "The reconcile will not end if conflicts are found." The page does not say what the
post then does. The likely result is that the target's side of each conflict is posted, and the
delete removes the version that held the other side. That result is inferred from Esri's
documentation. It was not run against a geodatabase. If your stale versions might hold edits
somebody wants, use that tool with the settings in [Reconcile order](#reconcile-order).
gdbprune is for versions whose edits you have already decided to throw away.

**Versions that replication uses are held back.** A live run asks `arcpy.da.ListReplicas` for
every replica in the geodatabase, including the replicas that sync-enabled feature services
make for offline maps. A version that one of them uses is never a candidate, and no flag
changes that. The default `%SYNC%` pattern also matches the `SYNC_SEND` and `SYNC_RECEIVE`
replica system versions. gdbprune never makes one of those a candidate unless you pass
`--allow-replica-anchors`. See [Versions that replication uses](#versions-that-replication-uses).

```
$ python gdbprune.py --self-test
gdbprune 1.3.0 self-test (no arcpy, no network, no credentials)
------------------------------------------------------------------
PASS  chain pass 1 selects exactly the leaf SYNC_C
PASS  chain pass 1 does NOT select SYNC_B (has live child)
PASS  chain pass 1 does NOT select SYNC_A (has live child)
...
PASS  naive one-pass selector DOES select the pinned parent SYNC_B
PASS  an old matching version with a live child is never selected  <-- pinned defect
...
PASS  a version created exactly at the cutoff is not old enough  <-- pinned defect
...
PASS  an aware 1900 creation time is planned and exported, not an OSError  <-- pinned defect
...
PASS  an owner-qualified parent pins its bare-named parent  <-- pinned defect
...
PASS  the dry run removes a version by owner and name, not by name alone  <-- pinned defect
...
PASS  a child that does not match the pattern still pins its parent  <-- pinned defect
...
PASS  an EMPTY --only-versions scopes to nothing, it does not disable scoping  <-- pinned defect
...
PASS  the default %SYNC% pattern never selects a SYNC_SEND or SYNC_RECEIVE anchor  <-- pinned defect
...
PASS  a wide-open --apply never asks the database to delete an anchor  <-- pinned defect
...
PASS  a version a registered replica uses is never selected, even with the anchor flag  <-- pinned defect
...
PASS  a chain exactly max_passes deep that empties on the last pass converges  <-- pinned defect
...
PASS  undated counts the matching, in-scope versions it cannot date, and no others  <-- pinned defect
...
PASS  a re-read that fails after deletes returns them, unconverged, with the error  <-- pinned defect
...
PASS  a field outside the five version fields is never copied into the file  <-- pinned defect
...
PASS  a schema 1 snapshot, which records no replica versions, is refused  <-- pinned defect
...
PASS  schema_version true is refused although True == 1  <-- pinned defect
...
PASS  a lone surrogate inside a name is refused  <-- pinned defect
...
PASS  SYNC_A and sync_a are read as two versions, as the live mode reads them  <-- pinned defect
PASS  a snapshot with no DEFAULT version is refused  <-- pinned defect
PASS  a snapshot with no versions at all is refused  <-- pinned defect
PASS  the schema example in this docstring is a snapshot the reader accepts  <-- pinned defect
...
PASS  a file nested 100000 levels deep is refused, not a traceback  <-- pinned defect
...
PASS  --from-versions runs with arcpy unimportable  <-- pinned defect
PASS  the export time is printed in the header and in the verdict
...
PASS  --from-versions refuses --apply: a snapshot cannot delete  <-- pinned defect
...
PASS  a non-cp1252 name prints escaped to a cp1252 stdout, not a crash  <-- pinned defect
PASS  --export-versions without --apply writes nothing  <-- pinned defect
...
PASS  the written file holds no connection string, host, connecting user or password  <-- pinned defect
...
PASS  an export never overwrites a file that is not a snapshot, such as the .sde  <-- pinned defect
...
PASS  an exported snapshot plans exactly what the live dry run plans
...
PASS  an export to the workspace's own connection string is refused, and no file is named after it  <-- pinned defect
PASS  the export and snapshot paths print redacted like the workspace  <-- pinned defect
...
PASS  argparse's usage error redacts a connection string it echoes  <-- pinned defect
...
PASS  an export of a True query result is refused, not written as an empty table  <-- pinned defect
...
PASS  --from-versions refuses an empty snapshot rather than plan it clean  <-- pinned defect
PASS  a table holding SYNC_A and sync_a exports, and plans as the live mode plans it  <-- pinned defect
...
PASS  a failure whose error quotes the workspace prints it with its values hidden  <-- pinned defect
...
PASS  an --apply that deletes a non-cp1252 name still reports what it deleted  <-- pinned defect
...
PASS  with two owners of one name, both plans list exactly what --apply deletes  <-- pinned defect
PASS  an --apply cut off at the pass limit exits 1, not 0  <-- pinned defect
PASS  an arcpy error exits 1 with its text redacted, not a traceback  <-- pinned defect
...
PASS  a live-mode refusal exits 1, as in 1.0.0, not 2  <-- pinned defect
...
PASS  an EZConnect or URL workspace is hidden whole  <-- pinned defect
...
PASS  a live run over a query that returned no rows is refused, not planned clean  <-- pinned defect
PASS  a table of unreadable creation times plans nothing, says 2 are undated and exits 1  <-- pinned defect
PASS  an --apply that could date no matching version exits 1, not 0  <-- pinned defect
PASS  an --apply whose re-read fails exits 1 and names what it deleted  <-- pinned defect
PASS  an --apply whose report cannot be written exits 1 and counts its deletes on stderr  <-- pinned defect
...
PASS  a live --apply deletes no anchor without the flag  <-- pinned defect
...
PASS  a live --apply deletes no version an offline map or a replica uses, even with the flag  <-- pinned defect
PASS  a run that cannot list the replicas deletes nothing and exits 1  <-- pinned defect
...
PASS  a plan from the export holds back the replica versions too  <-- pinned defect
...
PASS  a prefix of --apply (--ap) is refused, and nothing is read or deleted  <-- pinned defect
...
PASS  a prefix of --allow-replica-anchors is refused  <-- pinned defect
...
PASS  the self-test writes no bytecode cache next to the script  <-- pinned defect
...
PASS  check(), raises() and refuses() really do record a failure  <-- pinned defect
PASS  a failed assertion makes the self-test exit 1 and names it
------------------------------------------------------------------
317 assertions, 0 failed
```

The full run prints all 317 assertions. The `...` lines are where this block is cut. It takes
about 0.3 s on plain CPython 3.13 with no arcpy installed, and exits 0.

## Requirements

One file, standard library only, nothing to install.

```
git clone https://github.com/uhsear/gdbprune.git
```

Which modes need arcpy:

| Mode | Needs arcpy | Needs a database | Writes |
|---|---|---|---|
| `--workspace W` (plan) | yes | yes | nothing |
| `--workspace W --apply` | yes | yes | deletes versions |
| `--workspace W --export-versions FILE` | yes | yes | nothing |
| `--workspace W --export-versions FILE --apply` | yes | yes | `FILE` only, and only when `FILE` is absent or already a snapshot |
| `--from-versions FILE` | **no** | **no** | nothing, and it refuses `--apply` |
| `--self-test` | no | no | a temporary folder it deletes again |

For the arcpy modes, use ArcGIS Pro's Python:
`C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe`.
arcpy is imported inside the functions that touch a geodatabase, never at module scope, so
`--from-versions` and `--self-test` run on any Python 3.9 or newer, on Windows or Linux.

The same 317 assertions pass on each interpreter it has been run on: CPython 3.13.2, 3.12.10
and 3.9.25 on Windows, ArcGIS Pro's Python 3.13.7 on Windows, and CPython 3.12.3 on Ubuntu. On
Ubuntu the same count also passes under six `TZ` settings, from UTC-11 to UTC+14, because the
time-zone assertions use an offset that no zone uses. One assertion writes through a dangling
symbolic link. On a Windows host that cannot create one (no Developer Mode, no admin rights),
it prints a `SKIP` line instead, and the count is 316.

## Usage

Plan mode is the default. It reads the version table, computes the full multi-pass plan
in memory, prints it, and deletes nothing.

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" gdbprune.py --workspace C:\conn\admin.sde
```

Once the plan reads correctly, re-run the identical command with `--apply`.

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" gdbprune.py --workspace C:\conn\admin.sde --apply
```

### Planning without the database

Reading the version table needs an admin connection. The person who reviews a cleanup, or a
scheduled check on a Linux box, often should not hold one. So the table can travel as a file.

Somebody with the connection exports it once. Without `--apply` the export prints what it
would write and writes nothing.

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" gdbprune.py --workspace C:\conn\admin.sde --export-versions versions.json --apply
```

Anybody can then plan against that file, with any Python and no arcpy:

```
$ python gdbprune.py --from-versions versions.json

gdbprune 1.3.0  [DRY RUN - nothing is deleted]
  snapshot       : versions.json
  exported at    : 2026-10-07T06:15:00  (1 day(s) 14 hour(s) before this run)
  prune pattern  : %SYNC%
  older than     : 7 day(s)  (created before 2026-10-01 20:48:30)
  scope          : all versions
  candidate rule : name LIKE '%SYNC%' AND name NOT LIKE 'SYNC_SEND%' AND name NOT LIKE 'SYNC_RECEIVE%' AND creation_time < DATEADD(day, -7, GETDATE())
  versions read  : 9
  undated        : 0 matching version(s) with no readable creation time, never pruned
  replica anchors: 1 matching SYNC_SEND/SYNC_RECEIVE version(s) held back, never pruned
  replica in use : 1 matching version(s) that a registered replica uses, never pruned

  pass 1  -  2 leaf version(s)
      "DOMAIN\fieldcrew".SYNC_TABLET_07
      gisowner.SYNC_FIELD_0803
  pass 2  -  1 leaf version(s)
      gisowner.SYNC_FIELD_0802

  3 version(s) would be deleted across 2 pass(es), per the snapshot exported at 2026-10-07T06:15:00.
  A snapshot deletes nothing. To act on this plan, run against the live
  workspace, which re-reads the version table first.
```

That snapshot holds nine synthetic versions. `SYNC_FIELD_0801` is old and matches, but it
stays, because a `QA_REVIEW` version that does not match is still its child. `SYNC_TODAY` is
too young. `SYNC_SEND_41_3` is an old leaf that matches `%SYNC%`, and it stays because it is a
replica system version. `crew2_FieldSync_1404578882000` is an old leaf that matches too, and it
stays because the export marked it `"replica": true`: a registered replica uses it. The same
file planned with `--allow-replica-anchors` lists `sde.SYNC_SEND_41_3` in pass 1, prints
`4 version(s) would be deleted`, and drops the two `NOT LIKE` clauses from the candidate rule.
It still holds back `crew2_FieldSync_1404578882000`. The candidate rule does not show that
check, because it is a list that `ListReplicas` returns, not a column of the version table.
The export time is printed in the header and again in the verdict, because a snapshot can be
stale. `--from-versions` refuses `--apply`: only a live run, which re-reads the
table, may delete.

The `undated` line counts the versions that match the pattern and the scope but have no
readable creation time. Such a version is never pruned. A table whose creation times cannot be
read would otherwise plan `nothing to prune`, exactly like a clean one. So when the count is
above 0, the plan also prints `UNDATED:` and the run exits `1`. That is true for the live plan,
for `--apply` and for `--from-versions`. The self-test reads a table whose two `SYNC` versions
carry the times `01/02/2025 10:00:00 AM` and `2025-01-02 03:04:05 -05:00`. It asserts
`0 version(s) would be deleted`, `undated        : 2` and exit `1`, for the plan and for
`--apply`. gdbprune does not guess at a text date such as `01/02/2025`. In one locale it is
2 January, in another 1 February, and the wrong guess makes a version look older than it is.

| Flag | Default | Meaning |
|---|---|---|
| `--workspace PATH` | none | Admin `.sde` connection file. Required for a live run and for `--export-versions`. Also read from `$SDE_MAINTENANCE_WORKSPACE`; the flag wins. Refused with `--from-versions`. |
| `--prune-pattern TEXT` | `%SYNC%` | SQL `LIKE` pattern a version name must match. `%` and `_` wildcards, case-insensitive. A raw single quote is refused, not escaped. |
| `--prune-days N` | `7` | Only versions created more than N days ago. A negative value is refused, and so is a value that reaches back before the year 1. |
| `--only-versions A,B` | none | Restrict the run to these versions. Bare or owner-qualified names both work. An empty value (`--only-versions ""`) scopes the run to **nothing**; omit the flag for no scoping. Refused with `--export-versions`. |
| `--from-versions FILE` | none | Plan from a snapshot file instead of a live workspace. Read-only, no arcpy. |
| `--export-versions FILE` | none | Write the live version table to `FILE` as a snapshot. Needs arcpy and a workspace. Cannot be combined with `--from-versions`. |
| `--apply` | **off** | Actually delete, or with `--export-versions` actually write the file. Without it, nothing is deleted or written. |
| `--allow-replica-anchors` | **off** | Let `SYNC_SEND...` and `SYNC_RECEIVE...` replica system versions be candidates. Without it they are never pruned, whatever the pattern or `--only-versions` says. It never releases a version that a registered replica uses. Refused with `--export-versions`. |
| `--self-test` | off | Run the built-in assertions and exit. |

A flag must be typed in full. argparse would otherwise accept a prefix, so `--ap` would mean
`--apply` and delete. gdbprune refuses `--ap`, `--appl` and `--allow` with a usage error and
exit `2`, before it reads anything.

Exit codes:

| Code | Live plan and `--apply` | `--from-versions` and `--export-versions` |
|---|---|---|
| `0` | The run finished its work. | The plan finished, or the export ran. |
| `1` | The run left work, or could not run. See below. | The plan stopped at the pass limit, or a matching version has no readable creation time. |
| `2` | A usage error that argparse reports. | The command could not run. See below. |

A live run exits `1` in each of these cases. Every refusal that 1.0.0 had keeps the `1` it had
there.

- a version refused to delete
- the run stopped at the pass limit
- a matching version had no readable creation time, so it was not assessed (`UNDATED`)
- re-reading the table after an `--apply` pass failed. The run prints `STOPPED`, lists the
  versions it had deleted, and tries nothing more.
- the report of an `--apply` run could not be written, for example to a full disk or a closed
  pipe. One line on stderr then gives the number of versions deleted.
- the command could not run: no arcpy, no workspace, a bad `--prune-pattern`, an out-of-range
  `--prune-days`, a workspace that does not exist, a version table with no `DEFAULT` row, a
  replica list that `arcpy.da.ListReplicas` could not return, or a database error that arcpy
  raises

A half-finished `--apply` is never reported as success.

The two snapshot modes are new in 1.1.0, and they exit `2` when they cannot run: an invalid or
unreadable snapshot (a schema 1 file included), no arcpy, no workspace, an out-of-range
`--prune-days`, a version table with no `DEFAULT` row, an export path that holds something
other than a snapshot, an export path that repeats the workspace string, or
`--allow-replica-anchors` given to `--export-versions`.

A plan exits `0` whether or not it lists versions to delete. So a scheduled `--from-versions`
check cannot read "stale versions found" from the exit code. It reads the verdict line,
`N version(s) would be deleted`, instead. The exit code tells a finished plan (`0`) from a plan
cut off at the pass limit or one with undated versions (`1`), and from a broken input (`2`).

An unexpected error prints `error: <type>: <text>` and exits with the code for "could not run"
in its mode. Its text is redacted like the workspace. In 1.0.0 an arcpy error printed a
traceback, which could quote the connection string.

Everything gdbprune itself reports is escaped to ASCII: plans, errors and names. A version
named with the CJK character U+4E2D prints as `SYNC_\u4e2d`, so no name can crash the report,
even on a Windows stdout redirected to a file. argparse's own usage errors are the exception:
they echo a bad argument as typed, except that a connection string in one is redacted. The
values of a connection-string workspace are hidden:
`SERVER=***;USER=***;PASSWORD=***`. A value in braces or quotes can itself hold a `;`, as in
`PWD={a;b}`, so a string with a brace or a quote prints as `(connection string hidden)`. So
does any string with `@` or `://`, such as an EZConnect string (`user/password@host`) or a URL,
because neither has a `KEY=value` form to mask. The `--from-versions` and `--export-versions`
paths are printed the same way, so a `"$WS"` typed where a file belongs stays hidden. A file
name that holds `=` is therefore shown masked after the first `=`.

## The snapshot file

`--export-versions` writes this, and `--from-versions` reads nothing else:

```json
{
  "format": "gdbprune-versions",
  "schema_version": 2,
  "exported_at": "2026-09-22T06:15:00",
  "versions": [
    {"name": "DEFAULT", "parent": null, "owner": "sde", "created": "2019-03-01T08:00:00",
     "replica": false},
    {"name": "SYNC_A", "parent": "DEFAULT", "owner": "gisowner",
     "created": "2026-08-01T02:00:00.250000", "replica": false}
  ]
}
```

| Field | Type | Rule |
|---|---|---|
| `format` | text | exactly `gdbprune-versions` |
| `schema_version` | integer | exactly `2`. `true` is refused, although Python treats it as 1. |
| `exported_at` | timestamp | when the export ran |
| `versions` | list | one object per row of `sde.SDE_versions`, every row, unfiltered. It must hold `DEFAULT`. |
| `versions[].name` | text | not empty, no surrounding space, every character printable |
| `versions[].parent` | text or null | the same rules as `name`; null for a version with no parent |
| `versions[].owner` | text | no surrounding space, every character printable. Empty text means no owner. |
| `versions[].created` | timestamp or null | null means unknown, and such a version is never pruned |
| `versions[].replica` | `true` or `false` | `true` for a version that a registered replica uses. Such a version is never pruned. |

Schema 1, which gdbprune 1.1.0 and 1.2.0 wrote, has no `replica` field. A plan from it could
select a version that a replica uses, so the reader refuses it with exit `2`. Export the table
again.

A timestamp is `YYYY-MM-DDTHH:MM:SS`, with an optional six-digit `.ffffff`, and no offset.
`exported_at` is the local wall clock of the machine that ran the export. A creation time that
arcpy returns as an aware `datetime` object is converted to that clock first. A creation time
without an offset, which is what `ArcSDESQLExecute` usually returns, is written exactly as the
database returned it. Its clock is the database's, often the database server's local time or
UTC. A seventh fraction digit, as SQL Server's `datetime2` gives, rounds up to the microsecond,
so a version never reads older than it is.

A creation time that arrives as text with anything after the time, such as `-10:00`, is not
guessed at. It is written as `null` and the version is never pruned. So is an aware time that
leaves Python's date range once converted, such as one in the year 1. For an aware time before
1970 or after 2999, the local offset of the nearest end of that range is used, because Windows
cannot look up an offset outside it.

"Printable" means Python's `str.isprintable()`. It refuses every control character, C0 and C1
alike, every format character such as a zero-width space, a lone UTF-16 surrogate, private-use
and unassigned characters, and every space except the ordinary one. Each of these can hide
inside a name that looks right on screen. Printable non-ASCII text, such as a Polish owner
name, is accepted. "Unassigned" follows the Unicode version of the Python that reads the file.
A character new in Unicode 15.1 is printable to Python 3.13 and unassigned to 3.12 and 3.9, so
a snapshot that 3.13 accepts can be refused, with exit `2`, by an older Python.

The reader is strict. A missing field, an unknown field, a field repeated in one object, a
wrong type, a `replica` flag that is not `true` or `false`, `NaN`, a date that does not exist,
a space in place of the `T`, a trailing newline, non-ASCII digits, a creation time of `""`,
`false` or `0`, an unprintable character, a version listed twice with the same owner and name,
a list with no `DEFAULT` version, and a file nested thousands of levels deep are all refused.
The error names the field, or the offending value where no field applies, and the run exits
`2`. A UTF-8 byte order mark is accepted. The self-test holds one assertion for each of these.

Every traditionally-versioned geodatabase has a `DEFAULT` version, so a list without one is not
a whole version table. `ArcSDESQLExecute` can return `True`, `None` or an empty list for a
query. Read as an empty table, that planned `nothing to prune` and exited `0`. The export, the
reader and the live run now all refuse a table without `DEFAULT`.

The duplicate check compares names with their exact letter case. A case-sensitive database can
hold `SYNC_A` and `sync_a` side by side. The export writes both, and a plan from the snapshot
lists both, as the live plan does.

The file records no connection string, host or password. The only names in it beyond the
version names are the owners, a column of the version table. An owner is a database account,
and the account that ran the export is often one of them: `sde` owns `DEFAULT`, and an admin
connection is usually made as `sde`. So the file can name the connecting account, as an owner.
The self-test exports through a workspace string that carries a host, a user and a password.
It asserts that the host and password reach neither the file nor stdout. It also exports as
`USER=sde` and asserts that `sde` is written as an owner while the host and password are not.

The export reads back its own text before it writes, so it never writes a file that
`--from-versions` on the same Python version would refuse. It overwrites only an earlier
snapshot, a schema 1 snapshot from 1.1.0 or 1.2.0 included, and it never writes through a
symbolic link whose target is missing. If `FILE` is
the workspace string itself, the export refuses before it reads the table and exits `2`. That
stops a repeated connection string, which is not a file yet, from becoming a file named after
the password. If `FILE` exists and is not a snapshot, for example the `.sde` file spelled
another way, the export refuses and exits `2`.

## Configuration

Precedence, highest first:

1. the command-line flag
2. the environment variable `SDE_MAINTENANCE_WORKSPACE` (only `--workspace` reads one)
3. the `CONFIGURATION` block near the top of `gdbprune.py`

That block holds the settings that are deliberately not flags: `RESERVED_VERSIONS` (names that
are never candidates, `DEFAULT` out of the box), `REPLICA_ANCHOR_PATTERNS` (the replica system
version names held back unless `--allow-replica-anchors` is given), `VERSION_TABLE`,
`MAX_PASSES` (100), the two flag defaults, and the snapshot schema's field lists. The same
precedence is stated in the module docstring and enforced in `main()`. The environment variable
does not block `--from-versions`; only the `--workspace` flag does.

## Versions that replication uses

### Replica versions in use

Esri's
[Offline maps and versioned data](https://enterprise.arcgis.com/en/server/11.4/publish-services/windows/offline-maps-and-versioned-data.htm)
page says that "a replica version is generated from the published version each time you take
offline a map". Its name joins the user name, the feature service name and a unique ID. With one
version per user it is the user name and the service name. An anonymous user's version is named
`Esri_Anonymous_<feature service name>_<ID>`. The page says that "A user's replica version
remains as long as the user has a map downloaded." When the service name holds `Sync`, the
default `%SYNC%` pattern matches that version. gdbprune 1.2.0 then planned it and, under
`--apply`, deleted it.

gdbprune 1.3.0 does not guess from the name. Each time it reads the version table, it also
calls `arcpy.da.ListReplicas(workspace, True)`, before any delete. With `True` the list also
holds `SyncReplica` objects, which Esri describes as "a replica created through a sync-enabled
feature service". A replica's `version` property is "The version from which the replica was
created (replica version)." gdbprune marks every version named there, compared by unqualified
name in any letter case. A marked version is never a candidate, whatever `--prune-pattern`,
`--only-versions` or `--allow-replica-anchors` says. It stays in the tree, so it still pins its
parent. The plan counts the marked versions that match on the `replica in use` line. If
`ListReplicas` raises an error, the run stops before it deletes anything and exits `1`. The
export writes the mark as `"replica": true`, so a plan from the snapshot holds back the same
versions.

To delete such a version, unregister its replica first. The version then drops off the list.
For distributed collaboration, the same Esri page says that "no replica version is created when
data is copied during distributed collaboration workflows".

The self-test pins this through the stand-in arcpy. It registers
`gisowner.crew_FieldSync_1404578882000` and `sde.Esri_Anonymous_WaterSync` as feature service
replicas, and `SYNC_EDIT_9` as a geodatabase replica. A live `--apply` with
`--allow-replica-anchors` then deletes only the one ordinary version. The stand-in returns the
feature service replicas only when it is called with `True`, so a reader that drops the
argument fails the test. A `ListReplicas` that raises makes the run delete nothing and exit `1`.

### Replica system versions

Geodatabase replication records system versions in the same version table. Esri's knowledge
base article 000009436,
["What are the SYNC_SEND and SYNC_RECEIVE versions in the versions table?"](https://support.esri.com/en-us/knowledge-base/what-are-the-sync-send-and-sync-receive-versions-in-the-000009436),
says: "In geodatabase replication, on a successful synchronization between primary and
secondary replicas, SYNC_RECEIVE and SYNC_SEND versions are recorded in the sde versions
table. These temporary versions are managed by ArcSDE and should not be manually deleted from
the table. They are deleted when the replica is unregistered by way of the Replica Manager
dialog box in ArcCatalog or ArcMap." The article is marked for ArcGIS 9.x and 10. The current
ArcGIS Pro page
[Synchronization and versioning](https://pro.arcgis.com/en/pro-app/3.4/help/data/geodatabases/overview/synchronization-and-versioning.htm)
still says that when a replica sends changes, "the replica version (defined during replica
creation) and system versions are analyzed". It does not name the system versions.

Both names match the default `%SYNC%` pattern, so gdbprune 1.1.0 planned and deleted these
versions like any other. In a chain of them the newest is the leaf, so a leaf-first prune
deletes the newest first and walks down the chain one pass at a time. Run through the stand-in
arcpy, 1.1.0's `--apply` deleted all four versions of a synthetic replica: `SYNC_RECEIVE_7_3`
and `SYNC_SEND_7_2` in pass 1, then `SYNC_SEND_7_1`, then `SYNC_SEND_7_0`. It exited `0`.
1.1.0 also read `--ap` as `--apply`.

gdbprune 1.2.0 holds them back. A version is a replica anchor when its unqualified name
matches the SQL `LIKE` pattern `SYNC_SEND%` or `SYNC_RECEIVE%`, in any letter case. Such a
version is never a candidate unless `--allow-replica-anchors` is given. That is true for every
`--prune-pattern`, including `%` and `SYNC_SEND%`, and for an anchor named in
`--only-versions`. A held-back anchor stays in the tree, so it still pins its parent. In `LIKE`,
`_` matches any one character, so a look-alike such as `SYNCXSEND_1` is held back too. The
plan prints the count of matching anchors it held back on the `replica anchors` line, and the
two `NOT LIKE` clauses in the candidate rule.

The self-test pins this on a synthetic chain of three `SYNC_SEND_7_*` versions, a
`SYNC_RECEIVE_7_3`, an owner-qualified lower-case `gisowner.sync_send_9_1` and one ordinary
`SYNC_FIELD_01`. The naive selector picks the newest anchor. The default plan picks only
`SYNC_FIELD_01` and counts 5 anchors held back. A wide-open run (`%`, 0 days) asks the stand-in
database to delete only `SYNC_FIELD_01`. A live `--apply` through the stand-in arcpy deletes no
anchor without the flag, and with it deletes them leaf first.

Release an anchor only after its replica is gone. Esri's article says these versions are
deleted when the replica is unregistered.

## Reconcile order

gdbprune throws edits away. When the edits in some stale versions must survive, reconcile and
post those versions first, then prune what is left. The settings below come from Esri's
[Reconcile Versions](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/reconcile-versions.htm)
tool reference and the
[RecommendedReconcileOrder](https://desktop.arcgis.com/en/arcobjects/10.7/net/IVersionedWorkspace2_RecommendedReconcileOrder.htm)
reference. They were not run against a geodatabase for this README.

1. Reconcile every version whose edits must survive. The default
   `reconcile_mode="ALL_VERSIONS"` reconciles every edit version with the target.
   `BLOCKING_VERSIONS` reconciles only the "Versions that are blocking the target version from
   compressing", in the recommended reconcile order. That order sorts versions by their common
   ancestor state with `DEFAULT`, so each reconcile lets a later compress move more rows
   (ArcObjects `IVersionedWorkspace2.RecommendedReconcileOrder`). It leaves out a version that
   does not block a compress, and such a version can still hold unposted edits. Use
   `BLOCKING_VERSIONS` to free the compress, not to keep edits.
2. Do not rely on the defaults to keep conflicting edits. For traditional versioning
   `conflict_resolution` defaults to `FAVOR_TARGET_VERSION` and `abort_if_conflicts` to
   `NO_ABORT`. With `with_post="POST"` and `with_delete="DELETE_VERSION"`, every conflict is
   resolved for the target. Esri does not say what the post then does. The likely result is
   that the target's side is posted and the version that held the other side is deleted.
   Pass `abort_if_conflicts="ABORT_CONFLICTS"` instead. Esri describes it as "The
   reconcile will end if conflicts are found." Pass `out_log` too, and read it.
3. Resolve each version that stopped at a conflict by hand.
4. Run gdbprune without `--apply`, read the plan, then run it with `--apply` for the versions
   whose edits you have decided to discard.
5. Compress. gdbprune does not.

gdbprune's own order is a different thing. It deletes leaf first because the database refuses
to drop a version that still has a child. It does not look at states.

## Why the obvious version is wrong

The naive tool is one query and one loop: select every version matching the pattern and
older than N days, then delete them. On any version tree deeper than one level that
selection contains parents whose children are still alive, and the database rejects
every one of them. Sorting the list does not save it either, because a child that does
*not* match the pattern still pins a parent that does.

gdbprune reads the **entire** version table unfiltered, so leafness is computed over
every row, then selects only versions that nothing else names as a parent. After each
pass it re-reads the table, because a parent becomes eligible only once its children
are genuinely gone. A chain `A -> B -> C` therefore takes exactly three passes, and the
loop ends when a pass takes nothing.

The self-test pins this. It builds that chain, runs a naive one-pass selector alongside
the real one, and asserts that the naive one picks the pinned parent while the real one
does not. Rewrite the algorithm as a single pass and those assertions fail.

Cycles and self-parenting versions can never produce a leaf, so they end the loop on
their own. `MAX_PASSES` only bounds pathologically deep trees, and a run cut off there
prints `INCOMPLETE` and exits 1. A tree that empties on the last allowed pass counts as
finished, not cut off.

A snapshot goes through the same `prune()` and `select_candidates()` as a live run. The
self-test exports a table, plans it live and from the file, and asserts that the two plans list
the same passes and the same versions.

## Limitations

Real refusals, not a wishlist.

1. **Branch versioning is not supported and will not be.** Different system table,
   different API, different failure modes. This tool reads `sde.SDE_versions` only.
2. **No reconcile, post, or compress.** Deleting versions is the whole job. Unposted
   edits in a pruned version go with it, and the space those deletes free is reclaimed
   by a compress you still have to run yourself.
3. **One workspace per run.** No cross-database or multi-connection mode. Point it at
   another `.sde` and run it again.
4. **The candidate filter is deliberately not pushed into SQL,** so every pass reads the
   whole version table. On a geodatabase with tens of thousands of versions, that is the
   cost of computing leafness correctly.
5. **Leafness is keyed on the unqualified version name.** The owner, any quotes and the
   letter case are removed from both the name and the parent before they are compared, so
   `gisowner.SYNC_P`, `"SYNC_P"` and `sync_p` are one node. Two owners holding versions with
   the identical name collapse into one node, which can make a genuine leaf look pinned. The
   error direction is a skipped candidate, never a wrong delete. Everything else tracks a
   version by owner and name: the dry-run walk, the delete, and the list of refused deletes.
   So when `crew1.SYNC_X` goes, `crew2.SYNC_X` stays in the plan and still pins its parent.
6. **The printed candidate rule uses SQL Server date syntax** (`DATEADD`/`GETDATE`). It
   is informational only, since the real comparison happens in Python against the client
   clock, but it will read wrong to an Oracle or PostgreSQL DBA.
7. **No pre-flight lock check and no undo.** A version locked by another session is
   attempted, its database error is recorded, its siblings still go, and the run exits 1.
   Output is stdout only, so redirect it if a scheduler runs this. The plan of an `--apply`
   run is printed after its deletes, so a run that is killed part way prints nothing. If
   stdout cannot be written, the list of deleted versions is lost too. Only their number
   reaches stderr, and the run exits 1.
8. **A snapshot is a picture of the past.** `--from-versions` plans against the tree as it
   was at `exported_at`, and measures ages against the clock of the machine that reads it.
   Versions created or deleted since the export are not in the plan. That is why it
   refuses `--apply`.
9. **Snapshot timestamps carry no time zone.** They are the exporting machine's wall clock.
   The example file above, read on a Windows host at UTC-4, reported `1 day(s) 14 hour(s)`.
   Read in the same minute on an Ubuntu host that runs on UTC, it reported `1 day(s) 18 hour(s)`.
   Read a snapshot in the time zone it was written in, or allow for the difference.
10. **The export has not been run against a live geodatabase.** No enterprise geodatabase was
    available for this release. With ArcGIS Pro's real arcpy, the export was run as far as
    the workspace check, and it refused a missing workspace with exit 2 and wrote nothing.
    The live plan refused the same workspace with exit 1. The read, the
    file and its contents were tested through the self-test's stand-in arcpy module, which
    returns rows in the column order `ArcSDESQLExecute` is asked for. Run the export without
    `--apply` first, and check its version count against ArcGIS Pro.
11. **The snapshot names version owners.** Treat it as internal. The repository's
    `.gitignore` ignores `*.json` so that a snapshot is not committed by accident.
12. **Replica system versions are recognised by name only.** A system version renamed away
    from `SYNC_SEND...` or `SYNC_RECEIVE...`, or one from a future release that Esri names
    differently, is not held back. Esri's current documentation does not name these versions;
    the names come from article 000009436, which is marked for ArcGIS 9.x and 10. Replica
    versions in use are read from `arcpy.da.ListReplicas`. ArcGIS Pro 3.6's arcpy accepts the
    call with `True` and returned an empty list for a scratch file geodatabase. It was not run
    against an enterprise geodatabase that holds replicas. If an installed arcpy's
    `ListReplicas` does not accept the second argument, the call raises an error, and the live
    run deletes nothing and exits `1`. Before an `--apply`, read the plan for any version that a
    replica owns.
13. **Smaller edge cases.** Each of these was reproduced against this release, except the
    export race, which follows from the code.
    - `--only-versions` compares names without regard to letter case. On a case-sensitive
      database, `--only-versions SYNC_A` also scopes in, and can delete, `sync_a`.
    - `--only-versions` does not report a name that matches no version. A typo plans
      `nothing to prune` and exits `0`, so check the plan for each name you gave.
    - Naming a held-back version in `--only-versions` or `--prune-pattern` also plans
      `nothing to prune` and exits `0`. The `replica anchors` or `replica in use` line is the
      only sign that it was refused.
    - The pattern treats `[` and `]` as plain characters, but SQL Server `LIKE` reads them as a
      character class. The printed candidate rule can then describe a different selection
      than the one gdbprune makes.
    - Pattern and anchor matching use Python's Unicode case rules, so a non-ASCII look-alike
      such as U+017F (long s) matches `S`.
    - The anchor test uses the upper-cased last dotted part of the name. A look-alike such as
      `SYNCXSEND_1` is held back, but a bracketed name such as `[SYNC_SEND_1]` is not.
    - An owner that holds a dot but no backslash, such as `first.last`, is not quoted.
      `DeleteVersion` then receives `first.last.SYNC_A` and is likely to refuse it.
    - With `--allow-replica-anchors`, a partial run deletes the newest anchors first and leaves
      the oldest. That breaks a replica that is still registered, so release anchors only
      after the replica is gone.
    - A secret typed into `--prune-pattern`, `--only-versions` or a stray bare argument is
      printed as typed, in the plan header or in a usage error. Only the `--workspace`,
      `--from-versions` and `--export-versions` values are masked.
    - A password with an unquoted semicolon in a connection-string workspace is masked only up
      to that semicolon. Use an `.sde` file.
    - Database error text is shown with connection values masked, but it can still name the
      database account that failed to log in.
    - In a live run, exit `1` means that the tool could not run or that it left work. Read
      stderr to tell them apart.
    - The export checks the target file before it writes, but not atomically. Do not point it
      at a path that another process is changing.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [svcguard](https://github.com/uhsear/svcguard) - stops an ArcGIS Server service around
  maintenance and restarts it even when the maintenance fails. Wrap `--apply` in it when a
  published service holds a lock on a version.
- [taskpulse](https://github.com/uhsear/taskpulse) - reports which Windows scheduled tasks are
  silently failing. A scheduled gdbprune that exits `1` or `2` is one of them.
- [compressfloor](https://github.com/uhsear/compressfloor) - names what still holds the compress floor after the
  prune: a detached or stalled replica, a pinned version, or an orphaned state.
