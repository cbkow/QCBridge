import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "qcbridge"))
from ring1.visview import VisSig, decode, encode, plan  # noqa: E402


def test_round_trip_and_normalisation():
    sig = VisSig.make(["b", "a", "a"], True, ["c2", "c1"])
    assert sig.local == ("a", "b") and sig.hidden_lc == ("c1", "c2")
    assert decode(encode(sig)) == sig
    assert decode(b"") is None and decode(b"garbage") is None and decode(None) is None
    assert decode(encode(VisSig())) == VisSig()


def test_plan_enter_exit_add_remove():
    off = VisSig()
    on_ab = VisSig.make(["a", "b"])
    p = plan(off, on_ab)
    assert p.enter_with == ("a", "b") and not p.exit and p.empty is False
    p = plan(on_ab, off)
    assert p.exit and not p.enter_with
    p = plan(on_ab, VisSig.make(["b", "c"]))
    assert p.add == ("c",) and p.remove == ("a",) and not p.exit and not p.enter_with
    assert plan(on_ab, on_ab).empty


def test_plan_collection_isolation():
    have = VisSig.make((), False, ())
    want = VisSig.make((), True, ["c1"])
    p = plan(have, want)
    assert p.set_local_colls is True and p.lc_hide == ("c1",) and p.lc_show == ()
    # turning the per-viewport mode off shows everything it had hidden
    p = plan(want, have)
    assert p.set_local_colls is False and p.lc_show == ("c1",) and p.lc_hide == ()
    # a change of the hidden set while on
    p = plan(want, VisSig.make((), True, ["c2"]))
    assert p.set_local_colls is None and p.lc_hide == ("c2",) and p.lc_show == ("c1",)
