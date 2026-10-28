#!/usr/bin/env python3
"""
Archive everything stored on a Mergin Maps server before decommissioning it.

For every project you can access it saves:
  * the project metadata, collaborators and full version history (JSON + CSV)
  * a per-version table: Version, Created, Author, Files added/edited/removed
  * a per-file change log (one row per file touched in each version)
  * every conflict file ever uploaded, with a CSV describing each one
  * the latest project data (checksum-verified against the server)
  * optionally, every historical revision of every file (--full-history)

Run `python mergin_archive.py --help` for options. See README.md for details.
"""

import argparse
import csv
import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from mergin import ClientError, LoginError, MerginClient

DEFAULT_URL = "https://app.merginmaps.com"
COMPLETE_MARKER = ".archive_complete.json"

log = logging.getLogger("mergin_archive")

VERSION_HEADERS = [
    "Workspace", "Project", "Version", "Created (UTC)", "Author",
    "Files added", "Files edited", "Files removed", "Files renamed",
    "Added file names", "Edited file names", "Removed file names", "Renamed file names",
    "Conflict files uploaded", "Project size (bytes)", "Client used",
]
CHANGE_HEADERS = [
    "Workspace", "Project", "Version", "Created (UTC)", "Author",
    "Change", "File", "Size (bytes)", "Checksum (sha1)", "Geodiff edit", "Is conflict file",
]
CONFLICT_HEADERS = [
    "Workspace", "Project", "Version", "Created (UTC)", "Uploaded by",
    "Conflict type", "Conflict file", "Original file", "Conflicting user", "Based on version",
    "Still present in latest version", "Archived copy",
]
PROJECT_HEADERS = [
    "Workspace", "Project", "Project ID", "Created (UTC)", "Last updated (UTC)", "Created by",
    "Latest version", "Version count", "File count (latest)", "Size (bytes)",
    "Contributors", "Conflict files", "Latest data verified", "Archive folder", "Status",
]

# File naming conventions Mergin Maps clients use when a sync conflict happens.
# Newer clients: "data (conflicted copy, martin v5).gpkg" / "data (edit conflict, martin v5).json"
# Older clients: "data.gpkg_conflict_copy" / "data.gpkg_rebase_conflicts"
CONFLICT_PATTERNS = [
    (re.compile(r"^(?P<stem>.+?) \(conflicted copy, (?P<user>.+) v(?P<ver>\d+)\)(?P<ext>\.[^./]*)?$"),
     "Conflicted copy"),
    (re.compile(r"^(?P<stem>.+?) \(edit conflict, (?P<user>.+) v(?P<ver>\d+)\)\.json$"), "Edit conflict"),
    (re.compile(r"^(?P<stem>.+?)_conflict_copy\d*$"), "Conflict copy (legacy)"),
    (re.compile(r"^(?P<stem>.+?)_rebase_conflicts\d*$"), "Rebase conflicts (legacy)"),
]


# --------------------------------------------------------------------------- helpers


def safe_name(name):
    """Make a string safe to use as a Windows/Linux folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().rstrip(".")
    return cleaned or "_"


def fmt_time(value):
    """'2024-03-01T10:22:33.123Z' -> '2024-03-01 10:22:33' (UTC). Leaves unparseable values alone."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_csv(path, headers, rows):
    """Write CSV with a BOM so Excel opens accented/unicode text correctly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def retry(fn, *args, attempts=3, **kwargs):
    """Retry transient server/network failures; re-raise 4xx errors immediately."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except ClientError as e:
            http = getattr(e, "http_error", None)
            if (http and 400 <= http < 500 and http != 429) or attempt == attempts:
                raise
            wait = 5 * attempt
            log.warning(f"  request failed ({e.detail}); retrying in {wait}s")
            time.sleep(wait)
        except OSError as e:
            if attempt == attempts:
                raise
            wait = 5 * attempt
            log.warning(f"  network error ({e}); retrying in {wait}s")
            time.sleep(wait)


def classify_conflict(path):
    """Return conflict details if `path` looks like a Mergin Maps conflict file, else None."""
    folder, _, base = path.replace("\\", "/").rpartition("/")
    prefix = f"{folder}/" if folder else ""
    for pattern, kind in CONFLICT_PATTERNS:
        m = pattern.match(base)
        if m:
            groups = m.groupdict()
            original = groups["stem"] + (groups.get("ext") or "")
            return {
                "Conflict type": kind,
                "Original file": prefix + original,
                "Conflicting user": groups.get("user") or "",
                "Based on version": f"v{groups['ver']}" if groups.get("ver") else "",
            }
    if "conflict" in base.lower():
        return {"Conflict type": "Other (name contains 'conflict')", "Original file": "",
                "Conflicting user": "", "Based on version": ""}
    return None


def version_number(name):
    try:
        return int(str(name).lstrip("v"))
    except ValueError:
        return 0


# --------------------------------------------------------------------------- server access


def connect(args):
    url = args.url or os.environ.get("MERGIN_URL") or DEFAULT_URL
    username = args.username or os.environ.get("MERGIN_USERNAME") or input("Mergin Maps username or email: ")
    password = os.environ.get("MERGIN_PASSWORD") or getpass.getpass(f"Password for {username}: ")
    try:
        mc = MerginClient(url, login=username, password=password, plugin_version="mergin-archive/1.0")
    except LoginError as e:
        sys.exit(f"Login failed: {e}")
    log.info(f"Logged in to {url} as {mc.username()}")
    return mc, url


def paged_projects(mc, label, **filters):
    """Page through the project list, logging progress so long listings don't look stuck."""
    projects, page = [], 1
    while True:
        resp = retry(mc.paginated_projects_list, page=page, per_page=100, **filters)
        projects += resp["projects"]
        log.info(f"  {label}: {len(projects)}/{resp['count']} projects listed")
        if not resp["projects"] or len(projects) >= resp["count"]:
            return projects
        page += 1


def list_projects(mc, only_workspaces=None):
    """All projects in every workspace the user belongs to.

    The unfiltered project list is avoided: on app.merginmaps.com it also contains every public
    project of every other user, which is huge and irrelevant here.
    """
    workspaces = []
    try:
        workspaces = retry(mc.workspaces_list)
        log.info(f"Workspaces: {', '.join(ws['name'] for ws in workspaces) or '(none)'}")
    except ClientError as e:
        log.info(f"Workspaces not available on this server ({e.detail})")

    if only_workspaces:
        wanted = {w.lower() for w in only_workspaces}
        workspaces = [ws for ws in workspaces if ws["name"].lower() in wanted]

    projects = {}
    for ws in workspaces:
        try:
            for p in paged_projects(mc, ws["name"], only_namespace=ws["name"]):
                projects[p["id"]] = p
        except ClientError as e:
            log.warning(f"Could not list projects of workspace {ws['name']}: {e.detail}")

    if not workspaces and not only_workspaces:
        # Old self-hosted servers without workspaces: only these filters keep the list to the user's own projects
        for flag in ("created", "shared"):
            try:
                for p in paged_projects(mc, f"{flag} projects", flag=flag):
                    projects[p["id"]] = p
            except ClientError as e:
                log.warning(f"Could not list {flag} projects: {e.detail}")

    result = sorted(projects.values(), key=lambda p: (p["namespace"].lower(), p["name"].lower()))
    return result, workspaces


def fetch_versions(mc, full_name, project_id):
    """Full version history, with per-file change lists for every version."""
    try:
        versions = retry(mc.project_versions, full_name)
    except IndexError:  # client raises this for projects with no versions at all
        return []

    # Newer servers only return change *counts* in the history listing; fetch the full record instead.
    def needs_detail(v):
        return not all(isinstance(items, list) for items in (v.get("changes") or {}).values())

    def detail(v):
        try:
            return retry(mc.project_version_info, project_id, v["name"])
        except ClientError as e:
            log.warning(f"  could not fetch details of {v['name']}: {e.detail}")
            return v

    todo = [i for i, v in enumerate(versions) if needs_detail(v)]
    if todo:
        log.info(f"  fetching file lists for {len(todo)} versions")
        with ThreadPoolExecutor(max_workers=8) as pool:
            for n, (i, v) in enumerate(zip(todo, pool.map(detail, [versions[i] for i in todo])), 1):
                versions[i] = v
                if n % 50 == 0 or n == len(todo):
                    log.info(f"    {n}/{len(todo)} versions")
    return versions


# --------------------------------------------------------------------------- archiving


def summarise_versions(workspace, project, versions):
    """Build the version table, the per-file change log and the conflict list."""
    version_rows, change_rows, conflicts = [], [], []
    for v in versions:
        changes = v.get("changes") or {}
        created = fmt_time(v.get("created"))
        author = v.get("author") or ""
        files = {kind: [] for kind in ("added", "edited", "removed", "renamed")}
        conflict_count = 0

        for kind, items in changes.items():
            if not isinstance(items, list):
                continue
            label = {"added": "added", "updated": "edited", "updated_diff": "edited",
                     "removed": "removed", "renamed": "renamed"}.get(kind, kind)
            for item in items:
                path = item.get("path") or item.get("new_path") or ""
                if kind == "renamed" and item.get("new_path"):
                    path = f"{item.get('path')} -> {item['new_path']}"
                files.setdefault(label, []).append(path)
                conflict = classify_conflict(path) if label in ("added", "edited") else None
                change_rows.append({
                    "Workspace": workspace, "Project": project, "Version": v.get("name"),
                    "Created (UTC)": created, "Author": author, "Change": label, "File": path,
                    "Size (bytes)": item.get("size", ""), "Checksum (sha1)": item.get("checksum", ""),
                    "Geodiff edit": "yes" if item.get("diff") else "",
                    "Is conflict file": "yes" if conflict else "",
                })
                if conflict:
                    conflict_count += 1
                    conflicts.append({
                        "Workspace": workspace, "Project": project, "Version": v.get("name"),
                        "Created (UTC)": created, "Uploaded by": author, "Conflict file": path,
                        **conflict,
                    })

        version_rows.append({
            "Workspace": workspace, "Project": project, "Version": v.get("name"),
            "Created (UTC)": created, "Author": author,
            "Files added": len(files["added"]), "Files edited": len(files["edited"]),
            "Files removed": len(files["removed"]), "Files renamed": len(files["renamed"]),
            "Added file names": "; ".join(files["added"]),
            "Edited file names": "; ".join(files["edited"]),
            "Removed file names": "; ".join(files["removed"]),
            "Renamed file names": "; ".join(files["renamed"]),
            "Conflict files uploaded": conflict_count,
            "Project size (bytes)": v.get("project_size", ""),
            "Client used": v.get("user_agent", ""),
        })
    return version_rows, change_rows, conflicts


def release_client_logs(folder):
    """The Mergin client leaves <folder>/.mergin/client-log.txt open after a download; close it
    so Windows lets us rename/delete the folder."""
    folder = os.path.abspath(folder)
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in list(getattr(logger, "handlers", [])):
            if isinstance(handler, logging.FileHandler) and os.path.abspath(handler.baseFilename).startswith(folder):
                logger.removeHandler(handler)
                handler.close()


def move_folder(src, dst, attempts=5):
    """Rename src to dst, waiting out brief locks from antivirus/indexers; copy as a last resort."""
    for attempt in range(1, attempts + 1):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == attempts:
                break
            time.sleep(2 * attempt)
    log.warning(f"  could not rename {src.name}, copying instead")
    shutil.copytree(src, dst)
    shutil.rmtree(src, ignore_errors=True)


def download_latest(mc, full_name, info, project_dir):
    """Download the latest data into project_dir/files and verify it against the server checksums."""
    target = project_dir / "files"
    tmp = project_dir / "files.partial"
    def attempt():
        # the client refuses to download into an existing folder, so clear any partial download first
        if tmp.exists():
            release_client_logs(tmp)
            shutil.rmtree(tmp)
        try:
            mc.download_project(full_name, str(tmp))
        finally:
            release_client_logs(tmp)

    retry(attempt)
    if target.exists():
        release_client_logs(target)
        shutil.rmtree(target)
    move_folder(tmp, target)

    rows, all_ok = [], True
    for f in info.get("files", []):
        local = target / f["path"]
        actual = sha1_of(local) if local.exists() else ""
        ok = actual == f.get("checksum")
        all_ok &= ok
        rows.append({"File": f["path"], "Size (bytes)": f.get("size", ""), "Modified (UTC)": fmt_time(f.get("mtime")),
                     "Server checksum (sha1)": f.get("checksum", ""), "Local checksum (sha1)": actual,
                     "Verified": "yes" if ok else "NO"})
    write_csv(project_dir / "files_manifest.csv",
              ["File", "Size (bytes)", "Modified (UTC)", "Server checksum (sha1)", "Local checksum (sha1)", "Verified"],
              rows)
    if not all_ok:
        log.warning(f"  checksum mismatch for some files of {full_name} - see files_manifest.csv")
    return all_ok


def download_revision(mc, full_name, path, version, dest):
    if dest.exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        retry(mc.download_project_file, full_name, path, str(dest), version=version)
        return True
    except (ClientError, OSError) as e:
        log.warning(f"  could not download {path} @ {version}: {getattr(e, 'detail', e)}")
        return False


def write_project_readme(project_dir, project, info, version_rows, conflicts, collaborators, verified, args):
    authors = Counter(r["Author"] for r in version_rows)
    lines = [
        f"# {project['namespace']} / {project['name']}",
        "",
        f"- **Project ID:** {project['id']}",
        f"- **Created:** {fmt_time(info.get('created') or project.get('created'))} UTC",
        f"- **Last updated:** {fmt_time(info.get('updated') or project.get('updated'))} UTC",
        f"- **Latest version:** {info.get('version') or project.get('version')} ({len(version_rows)} versions archived)",
        f"- **Files in latest version:** {len(info.get('files', []))}",
        f"- **Size:** {info.get('disk_usage', project.get('disk_usage', ''))} bytes",
        f"- **Conflict files found in history:** {len(conflicts)}",
        f"- **Latest data checksum-verified:** {verified}",
        f"- **Archived:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        "",
        "## Contributors (versions uploaded)",
        "",
    ]
    lines += [f"- {a or '(unknown)'}: {n}" for a, n in authors.most_common()] or ["- none"]
    if collaborators:
        lines += ["", "## Collaborators at time of archiving", ""]
        for c in collaborators:
            lines.append(f"- {c.get('username', '')} {c.get('email', '')} - {c.get('project_role') or c.get('role', '')}")
    lines += [
        "",
        "## What is in this folder",
        "",
        "| Item | Contents |",
        "|---|---|",
        "| `versions.csv` | One row per version: when, who, files added / edited / removed |",
        "| `file_changes.csv` | One row per file changed in each version |",
        "| `conflicts.csv` | Every conflict file uploaded, which file it relates to and who caused it |",
        "| `conflicts/vN/` | Copy of each conflict file as uploaded in version N |",
        "| `files/` | Project data at the latest version (open the `.qgs`/`.qgz` in QGIS) |",
        "| `files_manifest.csv` | Latest files with server checksums and verification result |",
    ]
    if args.full_history:
        lines.append("| `history/vN/` | Every file as it was added or edited in version N |")
    lines += [
        "| `raw/` | Unmodified JSON responses from the Mergin Maps API |",
        "",
    ]
    (project_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def archive_project(mc, project, out_dir, args):
    full_name = f"{project['namespace']}/{project['name']}"
    project_dir = out_dir / "projects" / safe_name(project["namespace"]) / safe_name(project["name"])
    raw = project_dir / "raw"

    marker = read_json(project_dir / COMPLETE_MARKER)
    already_done = (
        marker
        and marker.get("version") == project.get("version")
        and (args.metadata_only or not marker.get("metadata_only"))
        and (marker.get("full_history") or not args.full_history)
    )
    if already_done and not args.force:
        log.info(f"Skipping {full_name} (already archived at {marker['version']})")
        return project_dir

    log.info(f"Archiving {full_name} ({project.get('version')})")
    project_dir.mkdir(parents=True, exist_ok=True)

    log.info("  reading project info and version history")
    info = retry(mc.project_info, full_name)
    write_json(raw / "project_info.json", info)
    write_json(raw / "project_list_entry.json", project)

    collaborators = []
    try:
        collaborators = retry(mc.list_project_collaborators, project["id"])
    except (ClientError, AttributeError) as e:
        log.info(f"  collaborators not available: {getattr(e, 'detail', e)}")
    write_json(raw / "collaborators.json", collaborators)

    versions = fetch_versions(mc, full_name, project["id"])
    write_json(raw / "versions.json", versions)
    log.info(f"  {len(versions)} versions")

    version_rows, change_rows, conflicts = summarise_versions(project["namespace"], project["name"], versions)
    latest_paths = {f["path"] for f in info.get("files", [])}
    for c in conflicts:
        c["Still present in latest version"] = "yes" if c["Conflict file"] in latest_paths else "no"
        if not args.metadata_only:
            dest = project_dir / "conflicts" / c["Version"] / c["Conflict file"]
            if download_revision(mc, full_name, c["Conflict file"], c["Version"], dest):
                c["Archived copy"] = dest.relative_to(project_dir).as_posix()

    write_csv(project_dir / "versions.csv", VERSION_HEADERS, version_rows)
    write_csv(project_dir / "file_changes.csv", CHANGE_HEADERS, change_rows)
    write_csv(project_dir / "conflicts.csv", CONFLICT_HEADERS, conflicts)
    log.info(f"  {len(change_rows)} file changes, {len(conflicts)} conflict files")

    verified = "skipped (--metadata-only)"
    if not args.metadata_only:
        if info.get("files"):
            verified = "yes" if download_latest(mc, full_name, info, project_dir) else "NO - see files_manifest.csv"
        else:
            verified = "n/a (project is empty)"

        if args.full_history:
            todo = [r for r in change_rows if r["Change"] in ("added", "edited") and " -> " not in r["File"]]
            log.info(f"  downloading {len(todo)} historical file revisions")
            for i, r in enumerate(todo, 1):
                download_revision(mc, full_name, r["File"], r["Version"],
                                  project_dir / "history" / r["Version"] / r["File"])
                if i % 50 == 0:
                    log.info(f"    {i}/{len(todo)}")

    write_project_readme(project_dir, project, info, version_rows, conflicts, collaborators, verified, args)
    write_json(project_dir / COMPLETE_MARKER, {
        "version": project.get("version"),
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "verified": verified,
        "metadata_only": args.metadata_only,
        "full_history": args.full_history,
    })
    return project_dir


def archive_workspaces(mc, workspaces, out_dir):
    for ws in workspaces:
        ws_dir = out_dir / "workspaces" / safe_name(ws["name"])
        write_json(ws_dir / "workspace.json", ws)
        for label, fn in (("members", getattr(mc, "list_workspace_members", None)),
                          ("usage", mc.workspace_usage)):
            if fn is None:
                continue
            try:
                write_json(ws_dir / f"{label}.json", retry(fn, ws["id"]))
            except ClientError as e:
                log.info(f"  workspace {ws['name']} {label} not available: {e.detail}")


def build_indexes(out_dir, projects, statuses, url, username, args):
    """Combine every project's CSVs into archive-wide tables and write the top-level README."""
    all_versions, all_changes, all_conflicts, project_rows = [], [], [], []
    for p in projects:
        project_dir = out_dir / "projects" / safe_name(p["namespace"]) / safe_name(p["name"])
        versions = read_csv(project_dir / "versions.csv")
        conflicts = read_csv(project_dir / "conflicts.csv")
        all_versions += versions
        all_changes += read_csv(project_dir / "file_changes.csv")
        all_conflicts += conflicts
        info = read_json(project_dir / "raw" / "project_info.json", {})
        marker = read_json(project_dir / COMPLETE_MARKER, {})
        project_rows.append({
            "Workspace": p["namespace"], "Project": p["name"], "Project ID": p["id"],
            "Created (UTC)": fmt_time(p.get("created")), "Last updated (UTC)": fmt_time(p.get("updated")),
            "Created by": p.get("creator") or info.get("creator", ""),
            "Latest version": p.get("version", ""), "Version count": len(versions),
            "File count (latest)": len(info.get("files", [])), "Size (bytes)": p.get("disk_usage", ""),
            "Contributors": "; ".join(sorted({v["Author"] for v in versions if v["Author"]})),
            "Conflict files": len(conflicts), "Latest data verified": marker.get("verified", ""),
            "Archive folder": project_dir.relative_to(out_dir).as_posix(),
            "Status": statuses.get(p["id"], "archived" if marker else "NOT archived"),
        })

    write_csv(out_dir / "projects.csv", PROJECT_HEADERS, project_rows)
    write_csv(out_dir / "all_versions.csv", VERSION_HEADERS, all_versions)
    write_csv(out_dir / "all_file_changes.csv", CHANGE_HEADERS, all_changes)
    write_csv(out_dir / "all_conflicts.csv", CONFLICT_HEADERS, all_conflicts)

    failed = [r for r in project_rows if r["Status"] != "archived"]
    readme = f"""# Mergin Maps archive

Archived from **{url}** by **{username}** on {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC
using `mergin_archive.py`.

| | |
|---|---|
| Workspaces | {len({p['namespace'] for p in projects})} |
| Projects | {len(projects)} ({len(projects) - len(failed)} archived, {len(failed)} failed) |
| Versions | {len(all_versions)} |
| File changes | {len(all_changes)} |
| Conflict files | {len(all_conflicts)} |
| Mode | {"metadata only" if args.metadata_only else "full history (every file revision)" if args.full_history else "latest data + conflict files + full metadata"} |

## Where to start

- **`projects.csv`** - one row per project: size, versions, contributors, conflicts, and its folder.
- **`all_versions.csv`** - every upload across every project: Version, Created, Author,
  Files added / edited / removed (counts and file names). Filter by Workspace/Project.
- **`all_file_changes.csv`** - one row per file per version, to trace the history of a single file.
- **`all_conflicts.csv`** - every sync conflict ever uploaded.
- **`projects/<workspace>/<project>/`** - everything for one project; each has its own `README.md`.
- **`workspaces/<workspace>/`** - workspace details, members and storage usage (where the server allows).
- **`archive.log`** - log of the archiving run(s).

All CSVs are UTF-8 and open directly in Excel. All times are UTC.

## Folder layout

```
projects.csv / all_versions.csv / all_file_changes.csv / all_conflicts.csv
workspaces/<workspace>/        workspace.json, members.json, usage.json
projects/<workspace>/<project>/
    README.md                  summary of the project
    versions.csv               history of uploads (one row per version)
    file_changes.csv           history of files (one row per file per version)
    conflicts.csv              conflict files, original file, who and when
    conflicts/vN/...           the conflict files themselves, as uploaded in version N
    files/                     project data at the latest version (open the .qgz/.qgs in QGIS)
    files_manifest.csv         latest files with sha1 checksums and verification result
    history/vN/...             (only with --full-history) every file as added/edited in version N
    raw/                       untouched JSON from the Mergin Maps API
```

## About conflicts

Mergin Maps does not keep a separate conflict log. When two people edit the same data, the
client that syncs second uploads an extra file next to the original, named e.g.
`survey (conflicted copy, alice v12).gpkg` or `survey (edit conflict, alice v12).json`
(older clients: `survey.gpkg_conflict_copy`, `survey.gpkg_rebase_conflicts`). This archive
finds those files in the version history, records who/when/which file, and keeps a copy of each.

## Files changed vs "edited"

"Edited" = the file was replaced or, for GeoPackages, changed via a geodiff (marked
`Geodiff edit = yes` in the file change log). Mergin Maps records renames as remove + add.
"""
    if failed:
        readme += "\n## Projects not archived\n\n" + "\n".join(
            f"- {r['Workspace']}/{r['Project']}: {r['Status']}" for r in failed) + "\n"
    (out_dir / "README.md").write_text(readme, encoding="utf-8")
    return failed


# --------------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(description="Archive all data and history from a Mergin Maps server.")
    parser.add_argument("--out", default="mergin_archive", help="output folder (default: ./mergin_archive)")
    parser.add_argument("--url", help=f"server URL (default: $MERGIN_URL or {DEFAULT_URL})")
    parser.add_argument("--username", help="username or email (default: $MERGIN_USERNAME or prompt)")
    parser.add_argument("--workspace", action="append", help="only archive this workspace (repeatable)")
    parser.add_argument("--project", action="append", help="only archive this project, as workspace/name (repeatable)")
    parser.add_argument("--metadata-only", action="store_true", help="history and metadata only, no data files")
    parser.add_argument("--full-history", action="store_true",
                        help="also download every historical revision of every file (can be very large)")
    parser.add_argument("--force", action="store_true", help="re-archive projects that are already complete")
    parser.add_argument("--list", action="store_true", help="just list the projects that would be archived")
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(out_dir / "archive.log", encoding="utf-8")])

    socket.setdefaulttimeout(300)  # a stalled request fails (and is retried) instead of hanging forever
    mc, url = connect(args)
    projects, workspaces = list_projects(mc, args.workspace)
    if args.project:
        wanted = {p.lower() for p in args.project}
        projects = [p for p in projects if f"{p['namespace']}/{p['name']}".lower() in wanted]
    log.info(f"Found {len(projects)} projects in {len({p['namespace'] for p in projects})} workspaces")

    if args.list:
        for p in projects:
            print(f"{p['namespace']}/{p['name']}\t{p.get('version')}\t{p.get('disk_usage', '')} bytes")
        return

    write_json(out_dir / "raw" / "server_config.json", retry(mc.server_config))
    write_json(out_dir / "raw" / "projects_list.json", projects)
    write_json(out_dir / "raw" / "workspaces_list.json", workspaces)
    archive_workspaces(mc, workspaces, out_dir)

    statuses = {}
    for i, p in enumerate(projects, 1):
        log.info(f"[{i}/{len(projects)}]")
        try:
            archive_project(mc, p, out_dir, args)
            statuses[p["id"]] = "archived"
        except Exception as e:  # keep going - one broken project must not stop the archive
            detail = getattr(e, "detail", None) or repr(e)
            log.exception(f"FAILED {p['namespace']}/{p['name']}: {detail}")
            statuses[p["id"]] = f"FAILED: {detail}"

    failed = build_indexes(out_dir, projects, statuses, url, mc.username(), args)
    log.info(f"Done. Archive written to {out_dir}")
    if failed:
        log.warning(f"{len(failed)} project(s) failed - re-run the same command to retry them only")
        sys.exit(1)


if __name__ == "__main__":
    main()
