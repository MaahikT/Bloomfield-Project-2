"""
dashboard/sec_backfill.py - SEC EX-102 manual backfill behind the ingest-key console.

Called ONLY from maybe_ingest_console (dashboard/app.py), after the key gate. Two explicit steps:
  stage_files(...)  - validate every uploaded Exhibit-102 XML, then store bytes (no parsing)
  run_backfill(...) - back up the database file, then let the edgar collector's
                      load_unparsed_manual_files re-parse SEC raw files that predate the
                      current parser bookmarks (schedule_al_v3 / operating_metrics bridge).

Safety rules (PR review checklist):
  - DTD/ENTITY declarations are rejected outright; defusedxml is used when installed.
  - Upload names never become paths: new filings get server-generated ids via
    files.store_file; top-ups write only to the path already recorded in raw_files,
    after a realpath containment check, and never overwrite different bytes.
  - Size/count caps are enforced before any write; sha256 must match raw_files records.
  - No fetching anywhere: provenance URLs are validated strings, never requested.
  - The whole batch is validated before the first write; a failed validation writes nothing.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from collectors import edgar
from collectors.common import config, files
from collectors.common.log import RunTracker

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024
MAX_FILES = 40
# Upload widgets may prepend a numeric counter to the uploaded file name
# ("65-sec-edgar-doc-2026-00016.xml"). Tolerate exactly one "<digits>-" prefix;
# identity still comes only from the canonical id matching raw_files plus the
# sha256 checks below, so a prefixed name can only ever top up that same filing.
TOPUP_NAME = re.compile(r"^(?:\d+-)?(sec-edgar-doc-\d{4}-\d{5})\.xml$")
SEC_URL = re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/\S+\.xml$")
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")


def _raw_root() -> Path:
    return (config.data_dir() / "raw").resolve()


def _validate_xml(name: str, data: bytes) -> str | None:
    """None when acceptable, else the rejection reason (generic, no filesystem detail)."""
    if not data:
        return f"{name}: empty file"
    if len(data) > MAX_FILE_BYTES:
        return f"{name}: over the {MAX_FILE_BYTES // (1024 * 1024)} MB per-file limit"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"<"):
        return f"{name}: not XML"
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        return f"{name}: DTD/entity declarations are not accepted"
    try:
        try:
            from defusedxml import ElementTree as SafeET
            SafeET.fromstring(data)
        except ImportError:
            import xml.etree.ElementTree as ET
            ET.fromstring(data)
    except Exception as e:
        return f"{name}: XML does not parse ({type(e).__name__})"
    return None


def stage_files(con, uploads: list[tuple[str, bytes]], *, deal: str, provenance: dict) -> dict:
    """Validate the whole batch, then store. Raises ValueError before writing anything
    if any file fails validation. provenance maps upload name -> (accession, source url)."""
    if not uploads:
        raise ValueError("no files uploaded")
    if len(uploads) > MAX_FILES:
        raise ValueError(f"too many files (max {MAX_FILES})")
    if sum(len(d) for _, d in uploads) > MAX_TOTAL_BYTES:
        raise ValueError("upload batch too large")
    plan = []
    for name, data in uploads:
        err = _validate_xml(name, data)
        if err:
            raise ValueError(err)
        digest = hashlib.sha256(data).hexdigest()
        m = TOPUP_NAME.match(name)
        row = None
        if m:
            row = con.execute(
                "SELECT file_id, file_path, file_hash FROM raw_files "
                "WHERE file_id = ? AND source = 'sec_edgar'", [m.group(1)]).fetchone()
        if m and row:
            file_id, stored_path, recorded_hash = row
            if recorded_hash and recorded_hash != digest:
                raise ValueError(f"{name}: sha256 does not match the recorded raw_files hash")
            dest = config.from_stored_path(stored_path).resolve()
            if not dest.is_relative_to(_raw_root()) or dest.is_symlink():
                raise ValueError(f"{name}: recorded path fails the containment check - refusing")
            if dest.exists():
                if hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
                    plan.append({"kind": "skip", "file_id": file_id})
                    continue
                raise ValueError(f"{name}: different bytes already sit at the recorded path - refusing to overwrite")
            plan.append({"kind": "topup", "file_id": file_id, "dest": dest, "data": data})
        else:
            acc, url = provenance.get(name, (None, None))
            if acc is not None and not ACCESSION.match(acc):
                raise ValueError(f"{name}: bad accession format")
            if url is not None and not SEC_URL.match(url):
                raise ValueError(f"{name}: source URL must be an https://www.sec.gov/Archives/edgar/ xml link")
            if not deal.strip():
                raise ValueError(f"{name}: new filings need the deal/trust name")
            plan.append({"kind": "register", "data": data, "acc": acc, "url": url})
    written, skipped = [], []
    for item in plan:
        if item["kind"] == "skip":
            skipped.append(item["file_id"])
        elif item["kind"] == "topup":
            dest = item["dest"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp-upload")   # fixed suffix, no user input
            tmp.write_bytes(item["data"])
            os.replace(tmp, dest)                             # atomic on the same filesystem
            written.append(item["file_id"])
        else:
            res = files.store_file(
                con, item["data"], source="sec_edgar", doc_category="other",
                collector_name="edgar.py:console-backfill", url=item["url"],
                source_record_id=item["acc"], ext="xml",
                notes=f"deal={deal.strip()}; filed=unknown (console backfill)")
            (written if res.status == "new" else skipped).append(res.file_id)
    return {"written": written, "skipped": skipped}


def run_backfill(con, db_file: Path) -> dict:
    """Back up the database file, then re-parse SEC raw files missing current-parser output.
    On a catastrophic failure the database file is restored from the backup; the caller
    drops its connection so the next request reopens the restored file."""
    con.execute("CHECKPOINT")
    backup_dir = config.data_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{db_file.stem}-{stamp}.duckdb"
    shutil.copy2(db_file, backup)
    cfg = edgar.load_yaml(edgar.CONFIG_PATH)
    try:
        # suppress_exceptions=False: a catastrophic failure propagates to the restore path;
        # per-file parse problems are already contained inside load_unparsed_manual_files.
        with RunTracker(con, "edgar.py:console-backfill", suppress_exceptions=False) as run:
            parsed = edgar.load_unparsed_manual_files(con, run, cfg)
    except Exception:
        shutil.copy2(backup, db_file)
        raise
    return {"parsed": parsed, "status": run.status, "errors": list(run.errors),
            "backup": backup.name}
