# gdbprune

Delete stale leaf versions from a traditionally-versioned Enterprise geodatabase, printing a plan first.

**Who this is for.** A paid ArcGIS Enterprise install, a **traditionally-versioned**
geodatabase, and an admin `.sde` connection file. Branch-versioned shops get nothing
from this tool: branch versions live in a different system table and are removed
through a different API. If your data is branch-versioned, stop here.

The obvious one-pass delete fails, because the database refuses to drop a version that
is still the parent of another version. gdbprune deletes only the versions that are
leaves right now, re-reads the version table, and repeats. Without `--apply` it prints
the plan and deletes nothing.

```
$ python gdbprune.py --self-test
gdbprune 1.0.0 self-test (no arcpy, no network, no credentials)
------------------------------------------------------------------
PASS  chain pass 1 selects exactly the leaf SYNC_C
PASS  chain pass 1 does NOT select SYNC_B (has live child)
PASS  chain pass 2 deleted SYNC_B after re-query
PASS  naive one-pass selector DOES select the pinned parent SYNC_B
PASS  PINNED DEFECT: old+matching version with a live child is never selected
PASS  a child that does not match the pattern still pins its parent
PASS  sde.DEFAULT is never a candidate even as an ancient matching leaf
PASS  a two-node cycle terminates without deleting anything
PASS  hitting max_passes is reported as a NON-converged run
[81 further PASS lines omitted]
------------------------------------------------------------------
OK: 90/90 assertions passed
```

Real output, one elision marked. 0.09s on plain CPython 3.13 with no arcpy installed,
exit code 0. No install, no config file, no credentials, no database.

## Install

Clone it. One file, standard library only, nothing to install.

```
git clone https://github.com/uhsear/gdbprune.git
```

- **Real runs** need arcpy, so use ArcGIS Pro's Python:
  `C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe`
- **`--self-test`** needs nothing but Python 3.8 or newer. arcpy is imported inside the
  two functions that touch a geodatabase, never at module scope.

## Quick start

```
python gdbprune.py --self-test
```

90 assertions, no arcpy, no network, no credentials, no database. Exits non-zero if any
assertion fails.

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

| Flag | Default | Meaning |
|---|---|---|
| `--workspace PATH` | none | Admin `.sde` connection file. Required for a real run. Also read from `$SDE_MAINTENANCE_WORKSPACE`; the flag wins. |
| `--prune-pattern TEXT` | `%SYNC%` | SQL `LIKE` pattern a version name must match. `%` and `_` wildcards, case-insensitive. A raw single quote is refused, not escaped. |
| `--prune-days N` | `7` | Only versions created more than N days ago. A negative value is refused. |
| `--only-versions A,B` | none | Restrict the run to these versions. Bare or owner-qualified names both work. An empty value (`--only-versions ""`) scopes the run to **nothing**; omit the flag for no scoping. |
| `--apply` | **off** | Actually delete. Without it, gdbprune prints the plan and exits 0 having deleted nothing. |
| `--self-test` | off | Run the built-in assertions and exit. |

Exit codes: `0` clean, `1` if any version refused to delete **or** the run stopped at
the pass limit with work outstanding. A half-finished `--apply` is never reported as
success.

## Configuration

Precedence, highest first:

1. the command-line flag
2. the environment variable `SDE_MAINTENANCE_WORKSPACE` (only `--workspace` reads one)
3. the `CONFIGURATION` block near the top of `gdbprune.py`

That block holds the settings that are deliberately not flags: `RESERVED_VERSIONS`
(names that are never candidates, `DEFAULT` out of the box), `VERSION_TABLE`,
`MAX_PASSES` (100), and the two flag defaults. The same precedence is stated in the
module docstring and enforced in `main()`.

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
prints `INCOMPLETE` and exits 1.

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
5. **Leafness is keyed on the unqualified version name.** Two owners holding versions
   with the identical name collapse into one node, which can make a genuine leaf look
   pinned. The error direction is a skipped candidate, never a wrong delete.
6. **The printed candidate rule uses SQL Server date syntax** (`DATEADD`/`GETDATE`). It
   is informational only, since the real comparison happens in Python against the client
   clock, but it will read wrong to an Oracle or PostgreSQL DBA.
7. **No pre-flight lock check and no undo.** A version locked by another session is
   attempted, its database error is recorded, its siblings still go, and the run exits 1.
   Output is stdout only, so redirect it if a scheduler runs this.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [gdbxray](https://github.com/uhsear/gdbxray) - what the geodatabase holds before you prune it
