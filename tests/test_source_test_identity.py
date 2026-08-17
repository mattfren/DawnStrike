from __future__ import annotations

import hashlib

from scripts.capture_source_test_identity import _git_blob_oid, capture_identity, verify_payload


def test_git_blob_identity_uses_repository_object_framing() -> None:
    content = b"test content\n"
    expected = hashlib.sha1(b"blob 13\0test content\n", usedforsecurity=False).hexdigest()
    assert _git_blob_oid(content, "sha1") == expected


def test_capture_self_validates_same_checkout_and_head_mapping() -> None:
    payload = capture_identity()
    valid, current = verify_payload(payload)

    assert valid is True
    assert current["file_count"] == len(current["files"])
    assert current["head_commit_oid"]
    assert current["head_tree_oid"]
    assert all(item["checkout_git_blob_oid"] for item in current["files"])
