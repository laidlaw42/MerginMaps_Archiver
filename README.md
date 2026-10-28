# Mergin Maps archiver

`mergin_archive.py` pulls everything from a Mergin Maps server (app.merginmaps.com or self-hosted)
into a tidy, self-describing folder so the service can be decommissioned.

For every project you can access it saves:

- **Version history** (`versions.csv`): Version, Created, Author, Files added / edited / removed
  (counts and file names), project size, and the client used.
- **File change log** (`file_changes.csv`): one row per file per version.
- **Conflicts** (`conflicts.csv` + `conflicts/vN/`): every conflict file ever uploaded, the file it
  relates to, who caused it and when, plus a copy of the file.
- **Latest data** (`files/`), checksum-verified against the server (`files_manifest.csv`).
- **Collaborators, workspace members and usage**, plus the raw API JSON.
- Optionally **every historical revision** of every file (`--full-history`).

Archive-wide `projects.csv`, `all_versions.csv`, `all_file_changes.csv` and `all_conflicts.csv`
plus a generated `README.md` sit at the top of the archive.

## Setup (once)

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Run

Use an account that is an owner/admin of every workspace, so all projects are visible.
Credentials can be typed when prompted, or set as `MERGIN_URL`, `MERGIN_USERNAME`, `MERGIN_PASSWORD`.

See what will be archived:

```bash
.venv\Scripts\python mergin_archive.py --list
```

Archive everything (history, conflicts, latest data):

```bash
.venv\Scripts\python mergin_archive.py --out C:\mergin_archive
```

Also keep every past revision of every file (recommended for a decommission, if disk allows):

```bash
.venv\Scripts\python mergin_archive.py --out C:\mergin_archive --full-history
```

Other options: `--workspace NAME`, `--project workspace/name` (both repeatable),
`--metadata-only`, `--force`, `--url` for a self-hosted server.

## Notes

- **Re-runnable:** finished projects are skipped, so after a failure or interruption run the same
  command again. Projects that changed on the server since are re-archived.
- Keep `--out` short (e.g. `C:\mergin_archive`) to stay under Windows' 260-character path limit.
- Mergin Maps has no conflict log of its own; conflicts are found from the conflict files the
  apps upload (`... (conflicted copy, user vN).gpkg`, `... (edit conflict, user vN).json`,
  `..._conflict_copy`, `..._rebase_conflicts`).
- Check `projects.csv` (Status and "Latest data verified" columns) and `archive.log` before
  deleting anything on the server.
