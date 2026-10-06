# gdbprune

Delete stale leaf versions from a traditionally-versioned Enterprise geodatabase, printing a plan first.

A mobile editing crew syncs every night, and every sync leaves a version behind with `SYNC` in
its name. Nobody deletes them. Two years later the version table holds hundreds, and each one
still references database states that a compress cannot remove.

So somebody writes the obvious cleanup: select every `SYNC` version older than a week, then
delete each one. The first parent in that list fails, because the database refuses to drop a
version that still has a child. The script either stops halfway or swallows the error and moves
on. Either way the parents stay, and the next night adds another layer on top of them.

gdbprune deletes only the versions that are leaves right now, re-reads the version table, and
repeats until a pass finds nothing. Without `--apply` it prints the plan and deletes nothing.

**Who this is for.** A paid ArcGIS Enterprise install, a **traditionally-versioned**
geodatabase, and an admin `.sde` connection file. Branch-versioned shops get nothing
from this tool: branch versions live in a different system table and are removed
through a different API. If your data is branch-versioned, stop here.

**The existing alternative.** Esri's own Reconcile Versions tool
(`arcpy.management.ReconcileVersions`) can post each edit version to its target and then
delete it, with `with_post="POST"` and `with_delete="DELETE_VERSION"`. That keeps the edits,
which gdbprune never does. If your stale versions might hold edits somebody wants, use that
tool. gdbprune is for versions whose edits you have already decided to throw away.

```
$ python gdbprune.py --self-test
gdbprune 1.1.0 self-test (no arcpy, no network, no credentials)
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
PASS  a chain exactly max_passes deep that empties on the last pass converges  <-- pinned defect
...
PASS  undated counts the matching, in-scope versions it cannot date, and no others  <-- pinned defect
...
PASS  a re-read that fails after deletes returns them, unconverged, with the error  <-- pinned defect
...
PASS  a field outside the four version fields is never copied into the file  <-- pinned defect
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
PASS  a table of unreadable creation times plans nothing and says 2 are undated  <-- pinned defect
PASS  an --apply whose re-read fails exits 1 and names what it deleted  <-- pinned defect
PASS  an --apply whose report cannot be written exits 1 and counts its deletes on stderr  <-- pinned defect
...
PASS  the self-test writes no bytecode cache next to the script  <-- pinned defect
...
PASS  check(), raises() and refuses() really do record a failure  <-- pinned defect
PASS  a failed assertion makes the self-test exit 1 and names it
------------------------------------------------------------------
283 assertions, 0 failed
```

The full run prints all 283 assertions. The `...` lines are where this block is cut. It takes
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

The same 283 assertions pass on each interpreter it has been run on: CPython 3.13.2, 3.12.10
and 3.9.25 on Windows, ArcGIS Pro's Python 3.13.7 on Windows, and CPython 3.12.3 on Ubuntu. On
Ubuntu the same count also passes under six `TZ` settings, from UTC-11 to UTC+14, because the
time-zone assertions use an offset that no zone uses. One assertion writes through a dangling
symbolic link. On a Windows host that cannot create one (no Developer Mode, no admin rights),
it prints a `SKIP` line instead, and the count is 282.

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

gdbprune 1.1.0  [DRY RUN - nothing is deleted]
  snapshot       : versions.json
  exported at    : 2026-09-22T06:15:00  (4 day(s) 16 hour(s) before this run)
  prune pattern  : %SYNC%
  older than     : 7 day(s)  (created before 2026-09-19 22:35:11)
  scope          : all versions
  candidate rule : name LIKE '%SYNC%' AND creation_time < DATEADD(day, -7, GETDATE())
  versions read  : 7
  undated        : 0 matching version(s) with no readable creation time, never pruned

  pass 1  -  2 leaf version(s)
      "DOMAIN\fieldcrew".SYNC_TABLET_07
      gisowner.SYNC_FIELD_0803
  pass 2  -  1 leaf version(s)
      gisowner.SYNC_FIELD_0802

  3 version(s) would be deleted across 2 pass(es), per the snapshot exported at 2026-09-22T06:15:00.
  A snapshot deletes nothing. To act on this plan, run against the live
  workspace, which re-reads the version table first.
```

That snapshot holds seven synthetic versions. `SYNC_FIELD_0801` is old and matches, but it
stays, because a `QA_REVIEW` version that does not match is still its child. `SYNC_TODAY` is
too young. The export time is printed in the header and again in the verdict, because a
snapshot can be stale. `--from-versions` refuses `--apply`: only a live run, which re-reads the
table, may delete.

The `undated` line counts the versions that match the pattern and the scope but have no
readable creation time. Such a version is never pruned. A live plan prints the same line. A
table whose creation times cannot be read plans `nothing to prune` exactly like a clean one,
so this line is the only thing that tells the two apart. The self-test plans a table whose two
`SYNC` versions carry the times `01/02/2025 10:00:00 AM` and `2025-01-02 03:04:05 -05:00`. It
asserts `0 version(s) would be deleted` next to `undated        : 2`.

| Flag | Default | Meaning |
|---|---|---|
| `--workspace PATH` | none | Admin `.sde` connection file. Required for a live run and for `--export-versions`. Also read from `$SDE_MAINTENANCE_WORKSPACE`; the flag wins. Refused with `--from-versions`. |
| `--prune-pattern TEXT` | `%SYNC%` | SQL `LIKE` pattern a version name must match. `%` and `_` wildcards, case-insensitive. A raw single quote is refused, not escaped. |
| `--prune-days N` | `7` | Only versions created more than N days ago. A negative value is refused, and so is a value that reaches back before the year 1. |
| `--only-versions A,B` | none | Restrict the run to these versions. Bare or owner-qualified names both work. An empty value (`--only-versions ""`) scopes the run to **nothing**; omit the flag for no scoping. Refused with `--export-versions`. |
| `--from-versions FILE` | none | Plan from a snapshot file instead of a live workspace. Read-only, no arcpy. |
| `--export-versions FILE` | none | Write the live version table to `FILE` as a snapshot. Needs arcpy and a workspace. Cannot be combined with `--from-versions`. |
| `--apply` | **off** | Actually delete, or with `--export-versions` actually write the file. Without it, nothing is deleted or written. |
| `--self-test` | off | Run the built-in assertions and exit. |

Exit codes:

| Code | Live plan and `--apply` | `--from-versions` and `--export-versions` |
|---|---|---|
| `0` | The run finished its work. | The plan finished, or the export ran. |
| `1` | The run left work, or could not run. See below. | The plan stopped at the pass limit. |
| `2` | A usage error that argparse reports. | The command could not run. See below. |

A live run exits `1` in each of these cases. Every refusal that 1.0.0 had keeps the `1` it had
there.

- a version refused to delete
- the run stopped at the pass limit
- re-reading the table after an `--apply` pass failed. The run prints `STOPPED`, lists the
  versions it had deleted, and tries nothing more.
- the report of an `--apply` run could not be written, for example to a full disk or a closed
  pipe. One line on stderr then gives the number of versions deleted.
- the command could not run: no arcpy, no workspace, a bad `--prune-pattern`, an out-of-range
  `--prune-days`, a workspace that does not exist, a version table with no `DEFAULT` row, or a
  database error that arcpy raises

A half-finished `--apply` is never reported as success.

The two snapshot modes are new in 1.1.0, and they exit `2` when they cannot run: an invalid
or unreadable snapshot, no arcpy, no workspace, an out-of-range `--prune-days`, a version table
with no `DEFAULT` row, an export path that holds something other than a snapshot, or an
export path that repeats the workspace string.

A plan exits `0` whether or not it lists versions to delete. So a scheduled `--from-versions`
check cannot read "stale versions found" from the exit code. It reads the verdict line,
`N version(s) would be deleted`, and the `undated` line, instead. The exit code tells a finished
plan (`0`) from a plan cut off at the pass limit (`1`) and from a broken input (`2`).

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
  "schema_version": 1,
  "exported_at": "2026-09-22T06:15:00",
  "versions": [
    {"name": "DEFAULT", "parent": null, "owner": "sde", "created": "2019-03-01T08:00:00"},
    {"name": "SYNC_A", "parent": "DEFAULT", "owner": "gisowner", "created": "2026-08-01T02:00:00.250000"}
  ]
}
```

| Field | Type | Rule |
|---|---|---|
| `format` | text | exactly `gdbprune-versions` |
| `schema_version` | integer | exactly `1`. `true` is refused, although Python treats it as 1. |
| `exported_at` | timestamp | when the export ran |
| `versions` | list | one object per row of `sde.SDE_versions`, every row, unfiltered. It must hold `DEFAULT`. |
| `versions[].name` | text | not empty, no surrounding space, every character printable |
| `versions[].parent` | text or null | the same rules as `name`; null for a version with no parent |
| `versions[].owner` | text | no surrounding space, every character printable. Empty text means no owner. |
| `versions[].created` | timestamp or null | null means unknown, and such a version is never pruned |

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
wrong type, `NaN`, a date that does not exist, a space in place of the `T`, a trailing newline,
non-ASCII digits, a creation time of `""`, `false` or `0`, an unprintable character, a version
listed twice with the same owner and name, a list with no `DEFAULT` version, and a file nested
thousands of levels deep are all refused. The error names the field, or the offending value where no field applies, and the run
exits `2`. A UTF-8 byte order mark is accepted. The self-test holds one assertion for each of
these.

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
snapshot, and it never writes through a symbolic link whose target is missing. If `FILE` is
the workspace string itself, the export refuses before it reads the table and exits `2`. That
stops a repeated connection string, which is not a file yet, from becoming a file named after
the password. If `FILE` exists and is not a snapshot, for example the `.sde` file spelled
another way, the export refuses and exits `2`.

## Configuration

Precedence, highest first:

1. the command-line flag
2. the environment variable `SDE_MAINTENANCE_WORKSPACE` (only `--workspace` reads one)
3. the `CONFIGURATION` block near the top of `gdbprune.py`

That block holds the settings that are deliberately not flags: `RESERVED_VERSIONS`
(names that are never candidates, `DEFAULT` out of the box), `VERSION_TABLE`,
`MAX_PASSES` (100), the two flag defaults, and the snapshot schema's field lists. The same
precedence is stated in the module docstring and enforced in `main()`. The environment
variable does not block `--from-versions`; only the `--workspace` flag does.

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
   The example file above, read on a Windows host at UTC-4, reported `4 day(s) 16 hour(s)`.
   Read in the same minute on an Ubuntu host that runs on UTC, it reported `4 day(s) 20 hour(s)`.
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
