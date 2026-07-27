"""IdentityRegistry: uuid bookkeeping + the duplicate-stamp collision case."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.registry import IdentityRegistry  # noqa: E402


def test_first_contact_and_reseen():
    r = IdentityRegistry()
    assert r.claim("aaa", 1) == "ok"
    assert r.claim("aaa", 1) == "ok"
    assert r.uuid_for(1) == "aaa"
    assert r.session_uid_for("aaa") == 1
    assert len(r) == 1


def test_duplicate_stamp_collides():
    # Duplicating a datablock copies the custom property: same uuid,
    # different session_uid → the newcomer must be restamped.
    r = IdentityRegistry()
    assert r.claim("aaa", 1) == "ok"
    assert r.claim("aaa", 2) == "collision"
    # Caller restamps the newcomer and claims the fresh uuid.
    assert r.claim("bbb", 2) == "ok"
    assert r.uuid_for(1) == "aaa"
    assert r.uuid_for(2) == "bbb"


def test_restamp_drops_stale_reverse_entry():
    r = IdentityRegistry()
    assert r.claim("aaa", 1) == "ok"
    assert r.claim("ccc", 1) == "ok"  # same datablock, fresh uuid
    assert r.uuid_for(1) == "ccc"
    assert r.session_uid_for("aaa") is None


def test_forget_uuid():
    r = IdentityRegistry()
    r.claim("aaa", 1)
    r.forget_uuid("aaa")
    assert r.session_uid_for("aaa") is None
    assert r.uuid_for(1) is None
    assert len(r) == 0
