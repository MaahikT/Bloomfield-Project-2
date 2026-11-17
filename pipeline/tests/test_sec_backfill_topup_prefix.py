"""Browser upload widgets prepend a numeric counter to file names
("65-sec-edgar-doc-2026-00016.xml"). TOPUP_NAME must normalize that to the
canonical filing id so the existing raw_files identity + sha256 checks top up
the known filing instead of dropping into the new-filing path.
"""
import pytest

from collectors.common import config, files
from dashboard import sec_backfill

XML = b'<?xml version="1.0"?>\n<assetData><assets/></assetData>\n'
OTHER = b'<?xml version="1.0"?>\n<assetData><assets/><other/></assetData>\n'
FILE_ID = "sec-edgar-doc-2026-00016"
CANON = FILE_ID + ".xml"
PREFIXED = "65-" + CANON


def _seed(con):
    """Record the canonical filing exactly as a previous backfill left it."""
    res = files.store_file(con, XML, source="sec_edgar", doc_category="other",
                           collector_name="edgar.py:console-backfill", ext="xml",
                           file_id=FILE_ID)
    assert res.status == "new"
    return res


def _count(con):
    return con.execute("SELECT count(*) FROM raw_files").fetchone()[0]


def test_pattern_normalizes_exactly_one_numeric_prefix():
    assert sec_backfill.TOPUP_NAME.match(CANON).group(1) == FILE_ID
    assert sec_backfill.TOPUP_NAME.match(PREFIXED).group(1) == FILE_ID
    assert sec_backfill.TOPUP_NAME.match("7-" + CANON).group(1) == FILE_ID
    # anything but a single "<digits>-" prefix must NOT match
    assert sec_backfill.TOPUP_NAME.match("x-" + CANON) is None
    assert sec_backfill.TOPUP_NAME.match("65-66-" + CANON) is None
    assert sec_backfill.TOPUP_NAME.match("65" + CANON) is None
    assert sec_backfill.TOPUP_NAME.match("65-" + CANON.upper()) is None
    assert sec_backfill.TOPUP_NAME.match(" 65-" + CANON) is None


def test_unprefixed_name_still_skips_identical_bytes(con):
    _seed(con)
    report = sec_backfill.stage_files(con, [(CANON, XML)], deal="", provenance={})
    assert report == {"written": [], "skipped": [FILE_ID]}
    assert _count(con) == 1


def test_prefixed_name_skips_identical_bytes(con):
    _seed(con)
    report = sec_backfill.stage_files(con, [(PREFIXED, XML)], deal="", provenance={})
    assert report == {"written": [], "skipped": [FILE_ID]}
    assert _count(con) == 1


def test_prefixed_name_tops_up_missing_file(con):
    res = _seed(con)
    dest = config.from_stored_path(res.file_path)
    dest.unlink()
    report = sec_backfill.stage_files(con, [(PREFIXED, XML)], deal="", provenance={})
    assert report == {"written": [FILE_ID], "skipped": []}
    assert dest.read_bytes() == XML
    assert _count(con) == 1


def test_prefixed_name_hash_mismatch_rejected_no_write(con):
    res = _seed(con)
    dest = config.from_stored_path(res.file_path)
    dest.unlink()
    with pytest.raises(ValueError, match="sha256 does not match"):
        sec_backfill.stage_files(con, [(PREFIXED, OTHER)], deal="", provenance={})
    assert not dest.exists()           # nothing written
    assert _count(con) == 1


def test_non_numeric_prefix_stays_on_new_filing_path(con):
    _seed(con)
    with pytest.raises(ValueError, match="new filings need the deal/trust name"):
        sec_backfill.stage_files(con, [("x-" + CANON, XML)], deal="", provenance={})
    assert _count(con) == 1


def test_unknown_canonical_id_prefixed_still_needs_deal(con):
    with pytest.raises(ValueError, match="new filings need the deal/trust name"):
        sec_backfill.stage_files(con, [("65-sec-edgar-doc-2026-00999.xml", XML)],
                                 deal="", provenance={})
    assert _count(con) == 0


def test_prefixed_unknown_id_same_bytes_is_duplicate_not_new_identity(con):
    """Same bytes under a different canonical id must dedup to the existing
    filing by hash - the prefixed filename never creates a new identity."""
    _seed(con)
    report = sec_backfill.stage_files(con, [("65-sec-edgar-doc-2026-00999.xml", XML)],
                                      deal="BMO 2024-5C4 Mortgage Trust",
                                      provenance={})
    assert report == {"written": [], "skipped": [FILE_ID]}
    assert _count(con) == 1
