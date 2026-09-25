"""Tests for scripts/safe_env_edit.py -- SYNTHETIC fixtures only.

No real secrets file is ever touched here. All values below are
fabricated sentinels used solely to prove the helper never leaks them.
"""

from __future__ import annotations

import os
import stat
import sys
import hashlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import safe_env_edit as see  # noqa: E402

SENTINEL = "SENTINEL_DO_NOT_LEAK_a1b2c3"


def _write_bytes(path, data: bytes):
    with open(path, "wb") as f:
        f.write(data)


def _read_bytes(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def assert_no_leak(*captured_texts: str):
    for text in captured_texts:
        assert SENTINEL not in text, "sentinel leaked into captured output"


# ---------------------------------------------------------------------------
# 1. Mixed line endings
# ---------------------------------------------------------------------------


def test_mixed_line_endings_preserved(tmp_path):
    p = tmp_path / "mixed.env"
    raw = (
        b"FOO=bar\n"
        b"API_TOKEN=" + SENTINEL.encode() + b"\r\n"
        b"BAZ=qux\n"
        b"FEATURE_FLAG=off\r\n"
        b"LAST=noeol"
    )
    _write_bytes(p, raw)

    status = see.set_key(str(p), "BAZ", "changed")
    assert status.present is True

    new_raw = _read_bytes(p)
    lines = see._split_lines_preserving_terminators(new_raw)
    # Untouched lines keep exact original bytes/terminators.
    assert lines[0] == b"FOO=bar\n"
    assert lines[1] == b"API_TOKEN=" + SENTINEL.encode() + b"\r\n"
    assert lines[2] == b"BAZ=changed\n"  # terminator preserved as \n (original)
    assert lines[3] == b"FEATURE_FLAG=off\r\n"
    assert lines[4] == b"LAST=noeol"  # no terminator, preserved
    # Note: the sentinel legitimately remains in the target file at rest
    # (that's the untouched secret line, not a leak). "No leak" is checked
    # against stdout/stderr/exceptions/return values below and elsewhere.
    assert status.key == "BAZ"


def test_check_key_on_mixed_line_endings_reports_metadata_only(tmp_path, capsys):
    p = tmp_path / "mixed2.env"
    raw = b"API_TOKEN=" + SENTINEL.encode() + b"\r\nFLAG=enabled\n"
    _write_bytes(p, raw)

    status = see.check_key(str(p), "API_TOKEN")
    d = status.to_public_dict()
    assert d["key"] == "API_TOKEN"
    assert d["present"] is True
    assert d["classification"] == "NON_BOOLEAN"
    assert d["occurrence_count"] == 1

    out = repr(d)
    assert_no_leak(out)


# ---------------------------------------------------------------------------
# 2. Duplicate keys
# ---------------------------------------------------------------------------


def test_duplicate_key_refused_on_set(tmp_path):
    p = tmp_path / "dup.env"
    raw = b"DB_PASSWORD=" + SENTINEL.encode() + b"\nDB_PASSWORD=other_val\n"
    _write_bytes(p, raw)
    before = _read_bytes(p)

    with pytest.raises(see.DuplicateKeyError) as exc_info:
        see.set_key(str(p), "DB_PASSWORD", "new")

    msg = str(exc_info.value)
    assert "DB_PASSWORD" in msg
    assert "2" in msg
    assert_no_leak(msg)

    after = _read_bytes(p)
    assert before == after  # untouched, no guessing


def test_duplicate_key_reported_explicitly_on_check(tmp_path):
    p = tmp_path / "dup2.env"
    raw = b"TOKEN=" + SENTINEL.encode() + b"\nTOKEN=" + SENTINEL.encode() + b"2\n"
    _write_bytes(p, raw)

    status = see.check_key(str(p), "TOKEN")
    assert status.occurrence_count == 2
    assert status.classification == see.Classification.AMBIGUOUS
    assert_no_leak(repr(status.to_public_dict()))


# ---------------------------------------------------------------------------
# 3. Concurrent edit
# ---------------------------------------------------------------------------


def test_concurrent_modification_detected_and_aborts(tmp_path):
    p = tmp_path / "conc.env"
    raw = b"SECRET=" + SENTINEL.encode() + b"\nCOUNT=1\n"
    _write_bytes(p, raw)

    original_hash = hashlib.sha256(raw).hexdigest()

    # Simulate a change underneath us between "read" and "write".
    _write_bytes(p, raw.replace(b"COUNT=1", b"COUNT=2"))

    with pytest.raises(see.ConcurrentModificationError) as exc_info:
        see.set_key(str(p), "COUNT", "99", expected_hash=original_hash)

    assert_no_leak(str(exc_info.value))

    after = _read_bytes(p)
    assert after == raw.replace(b"COUNT=1", b"COUNT=2")  # unchanged by our aborted write
    assert b"COUNT=99" not in after


# ---------------------------------------------------------------------------
# 4. Write failure
# ---------------------------------------------------------------------------


def test_write_failure_leaves_original_intact_and_no_secret_in_traceback(tmp_path):
    p = tmp_path / "writefail.env"
    raw = b"API_KEY=" + SENTINEL.encode() + b"\nFLAG=on\n"
    _write_bytes(p, raw)

    with pytest.raises(see.WriteFailedError) as exc_info:
        see.set_key(str(p), "FLAG", "off", _fail_before_write=True)

    msg = str(exc_info.value)
    assert "FLAG" in msg
    assert_no_leak(msg)

    after = _read_bytes(p)
    assert after == raw  # original untouched

    # No leftover temp files in the directory.
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".safe_env_edit_")]
    assert leftovers == []


def test_write_failure_via_injected_writer_no_leak(tmp_path):
    p = tmp_path / "writefail2.env"
    raw = b"TOKEN=" + SENTINEL.encode() + b"\nX=1\n"
    _write_bytes(p, raw)

    def bad_writer(tmp_path_, data):
        raise OSError(f"disk full while writing {len(data)} bytes")

    with pytest.raises(see.WriteFailedError) as exc_info:
        see.set_key(str(p), "X", "2", _writer=bad_writer)

    assert_no_leak(str(exc_info.value))
    assert _read_bytes(p) == raw


# ---------------------------------------------------------------------------
# 5. Permission preservation
# ---------------------------------------------------------------------------


def test_permissions_preserved_after_edit(tmp_path):
    p = tmp_path / "perms.env"
    raw = b"FLAG=off\nOTHER=" + SENTINEL.encode() + b"\n"
    _write_bytes(p, raw)

    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    before_mode = stat.S_IMODE(os.stat(p).st_mode)

    see.set_key(str(p), "FLAG", "on")

    after_mode = stat.S_IMODE(os.stat(p).st_mode)
    assert after_mode == before_mode


# ---------------------------------------------------------------------------
# BOM / UTF-8 case
# ---------------------------------------------------------------------------


def test_bom_preserved(tmp_path):
    p = tmp_path / "bom.env"
    raw = b"\xef\xbb\xbfFOO=bar\nSECRET=" + SENTINEL.encode() + b"\n"
    _write_bytes(p, raw)

    see.set_key(str(p), "FOO", "changed")

    after = _read_bytes(p)
    assert after.startswith(b"\xef\xbb\xbf")
    assert after == b"\xef\xbb\xbfFOO=changed\nSECRET=" + SENTINEL.encode() + b"\n"
    # Sentinel legitimately persists in the untouched secret line on disk;
    # that is not a leak. Leak checks target captured process output below.


# ---------------------------------------------------------------------------
# Trailing-newline-absent case
# ---------------------------------------------------------------------------


def test_no_trailing_newline_preserved(tmp_path):
    p = tmp_path / "noeol.env"
    raw = b"SECRET=" + SENTINEL.encode() + b"\nLAST_KEY=value_no_eol"
    _write_bytes(p, raw)

    see.set_key(str(p), "LAST_KEY", "new_value")

    after = _read_bytes(p)
    assert after == b"SECRET=" + SENTINEL.encode() + b"\nLAST_KEY=new_value"
    assert not after.endswith(b"\n")


# ---------------------------------------------------------------------------
# Key not found
# ---------------------------------------------------------------------------


def test_key_not_found_no_leak(tmp_path):
    p = tmp_path / "nf.env"
    raw = b"OTHER=" + SENTINEL.encode() + b"\n"
    _write_bytes(p, raw)

    with pytest.raises(see.KeyNotFoundError) as exc_info:
        see.set_key(str(p), "MISSING", "x")

    msg = str(exc_info.value)
    assert "MISSING" in msg
    assert_no_leak(msg)


# ---------------------------------------------------------------------------
# check-key never reveals value, across classifications
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", see.Classification.ENABLED),
        ("false", see.Classification.DISABLED),
        ("1", see.Classification.ENABLED),
        ("0", see.Classification.DISABLED),
        ("", see.Classification.EMPTY),
        (SENTINEL, see.Classification.NON_BOOLEAN),
    ],
)
def test_check_key_classification(tmp_path, value, expected):
    p = tmp_path / "cls.env"
    _write_bytes(p, f"KEY={value}\n".encode())

    status = see.check_key(str(p), "KEY")
    assert status.classification == expected
    assert_no_leak(repr(status.to_public_dict()))


# ---------------------------------------------------------------------------
# CLI-level no-leak smoke test (stdout capture)
# ---------------------------------------------------------------------------


def test_cli_check_key_stdout_no_leak(tmp_path, capsys):
    p = tmp_path / "cli.env"
    raw = b"CREDENTIAL=" + SENTINEL.encode() + b"\n"
    _write_bytes(p, raw)

    rc = see._main(["check-key", str(p), "CREDENTIAL"])
    assert rc == 0

    captured = capsys.readouterr()
    assert_no_leak(captured.out, captured.err)


def test_cli_set_key_stdout_no_leak(tmp_path, capsys):
    p = tmp_path / "cli2.env"
    raw = b"CREDENTIAL=" + SENTINEL.encode() + b"\nFLAG=off\n"
    _write_bytes(p, raw)

    rc = see._main(["set-key", str(p), "FLAG", "on"])
    assert rc == 0

    captured = capsys.readouterr()
    assert_no_leak(captured.out, captured.err)
    # Untouched secret line still present and unmodified on disk.
    after = _read_bytes(p)
    assert b"CREDENTIAL=" + SENTINEL.encode() in after
