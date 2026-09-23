#!/usr/bin/env python3
"""The smoke runners, portable. One script, six suites; the same launches,
waits and checks as the zsh runners beside it (which stay as the reference
on macOS), written so a Windows box can run them without zsh, mktemp, kill
or ln -s. Plain CPython, no third-party modules.

    python smokes/run_smokes.py smoke      [work_dir]
    python smokes/run_smokes.py reconnect  [work_dir]
    python smokes/run_smokes.py cache      [work_dir]
    python smokes/run_smokes.py mapping    [work_dir]
    python smokes/run_smokes.py bench      [agent|zmq] [work_dir]
    python smokes/run_smokes.py coverage   [work_dir]

Environment, as for the zsh runners: BLENDER overrides the binary (default
per OS: the 5.2 install), QCB_TRANSPORT=agent QCB_AGENT=spawn runs over the
agent, and the suite scripts' own switches (QCB_COV_ONLY, QCB_COV_UNDO,
QCB_SMOKE_KIOSK, ...) pass straight through.

Two things the zsh runners left to the operator are done here: the pyzmq
wheel matching Blender's Python is unpacked into <work>/pysite when the
transport is zmq, and on Windows a Blender is stopped with its whole process
tree (the agent it spawned included -- TerminateProcess alone would orphan
it, and an orphaned replica agent keeps UDP/4246).

Exit code 0 means pass, as before. verdict.json / latency.json / coverage.json
and the host.log / replica.log land in the work dir.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WIN = sys.platform == "win32"


# --- Blender ----------------------------------------------------------------

def blender_path() -> str:
    bl = os.environ.get("BLENDER", "")
    if bl:
        return bl
    if WIN:
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        cands = sorted(glob.glob(os.path.join(pf, "Blender Foundation", "Blender 5.*", "blender.exe")))
        return cands[-1] if cands else "blender.exe"
    if sys.platform == "darwin":
        return "/Applications/Blender.app/Contents/MacOS/Blender"
    return shutil.which("blender") or "blender"


def blender_python_tag(bl: str) -> str:
    """'cp313' for the Python inside this Blender, asked once, headless."""
    out = subprocess.run(
        [bl, "-b", "--factory-startup", "--python-expr",
         "import sys; print('PYTAG=cp%d%d' % sys.version_info[:2])"],
        capture_output=True, text=True, timeout=120,
    ).stdout
    for line in out.splitlines():
        if line.startswith("PYTAG="):
            return line[len("PYTAG="):].strip()
    raise SystemExit("could not learn Blender's Python version from: " + bl)


def prepare_pysite(work: str, bl: str) -> None:
    """The zmq transport needs pyzmq importable by Blender's Python; the
    agent transport is stdlib-only and needs nothing."""
    if os.environ.get("QCB_TRANSPORT", "") == "agent":
        return
    tag = blender_python_tag(bl)
    plat = "win_amd64" if WIN else ("macosx" if sys.platform == "darwin" else "linux")
    wheels = [w for w in glob.glob(os.path.join(REPO, "qcbridge", "wheels", "pyzmq-*.whl"))
              if f"-{tag}-" in os.path.basename(w) and plat in os.path.basename(w)]
    if not wheels:
        raise SystemExit(f"no pyzmq wheel for {tag}/{plat} in qcbridge/wheels; "
                         "set QCB_TRANSPORT=agent QCB_AGENT=spawn or add the wheel")
    pysite = os.path.join(work, "pysite")
    if not os.path.isdir(os.path.join(pysite, "zmq")):
        os.makedirs(pysite, exist_ok=True)
        with zipfile.ZipFile(wheels[0]) as z:
            z.extractall(pysite)
        print(f"pysite: {os.path.basename(wheels[0])}")


# --- processes --------------------------------------------------------------

def launch(bl: str, script: str, work: str, log: str, env_extra: dict | None = None,
           debug: bool = True) -> subprocess.Popen:
    env = dict(os.environ)
    if debug:
        env["QCB_DEBUG"] = "1"
    env.update(env_extra or {})
    args = [bl, "--factory-startup", "-noaudio", "--python", script, "--", work]
    out = open(log, "wb")
    return subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, env=env,
                            cwd=work)


def stop(procs, grace: float = 2.0) -> None:
    """kill; sleep 2; kill -9 -- with the process tree on Windows."""
    procs = [p for p in procs if p is not None and p.poll() is None]
    for p in procs:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True)
        else:
            p.terminate()
    t0 = time.time()
    while any(p.poll() is None for p in procs) and time.time() - t0 < grace:
        time.sleep(0.1)
    for p in procs:
        if p.poll() is None:
            p.kill()


def kill9(p: subprocess.Popen) -> None:
    """The reconnect suite's `kill -9`: no grace, no clean exit -- on Windows
    with the tree, so the replica's private agent goes too, as the tracker
    asks (`--exit-with-addon` never gets its chance either way)."""
    if WIN:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        p.kill()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def load_json(work: str, name: str) -> dict:
    try:
        with open(os.path.join(work, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def wait_for(work: str, cond, timeout: float) -> bool:
    """cond(h, r) over host.json / replica.json, polled every 0.5 s."""
    t0 = time.time()
    while True:
        h, r = load_json(work, "host.json"), load_json(work, "replica.json")
        try:
            if cond(h, r):
                return True
        except Exception:
            pass
        if time.time() - t0 > timeout:
            return False
        time.sleep(0.5)


def fresh_work(arg: str | None, prefix: str) -> str:
    work = arg or tempfile.mkdtemp(prefix=prefix)
    os.makedirs(work, exist_ok=True)
    for n in ("host.json", "replica.json", "verdict.json", "coverage.json",
              "host_lat.json", "replica_lat.json", "latency.json"):
        try:
            os.remove(os.path.join(work, n))
        except OSError:
            pass
    for d in ("bl_replica", "bl_host"):
        os.makedirs(os.path.join(work, d), exist_ok=True)
    print(f"work dir: {work}")
    return work


def link_dir(target: str, link: str) -> None:
    """The mapping suite's `ln -s`. A symlink needs a privilege on Windows
    that a normal session may not have; a directory junction does the same
    job for a path that stays on one volume and needs none."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if not WIN:
            raise
    import _winapi
    _winapi.CreateJunction(target, link)


def run_python(script: str, *args: str) -> int:
    return subprocess.run([sys.executable, script, *args]).returncode


# --- suites -----------------------------------------------------------------

def pair(bl: str, work: str, replica_py: str, host_py: str, replica_env=None, host_env=None):
    """Replica first, four seconds, then the host -- the rhythm every runner uses."""
    r = launch(bl, replica_py, work, os.path.join(work, "replica.log"), replica_env)
    time.sleep(4)
    h = launch(bl, host_py, work, os.path.join(work, "host.log"), host_env)
    return r, h


def suite_smoke(bl: str, work: str) -> int:
    r, h = pair(bl, work, os.path.join(HERE, "smoke_replica.py"), os.path.join(HERE, "smoke_host.py"))
    try:
        return run_python(os.path.join(HERE, "smoke_poller.py"), work)
    finally:
        stop([h, r])


def suite_coverage(bl: str, work: str) -> int:
    cov = os.path.join(HERE, "coverage")
    r, h = pair(bl, work, os.path.join(cov, "cov_replica.py"), os.path.join(cov, "cov_host.py"))
    try:
        return run_python(os.path.join(cov, "cov_poller.py"), work)
    finally:
        stop([h, r])


def suite_cache(bl: str, work: str) -> int:
    r, h = pair(bl, work, os.path.join(HERE, "smoke_cache_replica.py"),
                os.path.join(HERE, "smoke_cache_host.py"))
    try:
        t0 = time.time()
        hj = rj = {}
        while time.time() - t0 < 120:
            hj, rj = load_json(work, "host.json"), load_json(work, "replica.json")
            if hj.get("done") and rj.get("frame") == 20 and rj.get("cloth_baked") and "mean_z" in rj:
                break
            time.sleep(0.5)
        time.sleep(2)
        hj, rj = load_json(work, "host.json"), load_json(work, "replica.json")
        st = rj.get("stats", {})
        checks = {
            "externalized_before_bake": bool(hj.get("externalized_before_bake")),
            "host_baked_to_root": bool(hj.get("host_baked")) and hj.get("host_cache_files", 0) > 0,
            "replica_external_same_path": rj.get("cloth_external") is True
                and rj.get("cloth_filepath") == hj.get("host_filepath"),
            "replica_reads_bake_no_resync": (rj.get("cloth_baked") is True and rj.get("frame") == 20
                and abs((rj.get("mean_z") or 0) - (hj.get("host_mean_z_f20") or 1)) < 1e-3
                and st.get("bootstraps") == 1 and hj.get("sent_boot") == 1),
            "replica_clean": st.get("gaps", 1) == 0 and st.get("apply_errors", 1) == 0
                and st.get("frozen_caches", 1) == 0,
        }
        print(json.dumps({
            "host": {k: hj.get(k) for k in ("host_baked", "host_mean_z_f20", "host_cache_files",
                                            "cache_note", "externalized")},
            "replica": {k: rj.get(k) for k in ("frame", "cloth_baked", "cloth_external",
                                               "cloth_info", "mean_z")}}, indent=1))
        return report(work, checks)
    finally:
        stop([h, r])


def suite_mapping(bl: str, work: str) -> int:
    shutil.rmtree(os.path.join(work, "replicaside"), ignore_errors=True)
    os.makedirs(os.path.join(work, "hostside", "proj"), exist_ok=True)
    os.makedirs(os.path.join(work, "replicaside"), exist_ok=True)
    link_dir(os.path.join(work, "hostside", "proj"), os.path.join(work, "replicaside", "proj"))
    r, h = pair(bl, work, os.path.join(HERE, "smoke_mapping_replica.py"),
                os.path.join(HERE, "smoke_mapping_host.py"))
    try:
        t0 = time.time()
        hj = rj = {}
        while time.time() - t0 < 90:
            hj, rj = load_json(work, "host.json"), load_json(work, "replica.json")
            if hj.get("done") and len(rj.get("images", {})) == 3 and hj.get("peer_status", {}).get("bootstraps"):
                break
            time.sleep(0.5)
        time.sleep(2)
        hj, rj = load_json(work, "host.json"), load_json(work, "replica.json")
        rr = rj.get("replica_root", "")
        im = rj.get("images", {})
        st = rj.get("stats", {})
        checks = {
            "relative_image_mapped_to_replica_root":
                im.get("RelTex", {}).get("filepath", "").startswith(rr) and im.get("RelTex", {}).get("loads") is True,
            "absolute_image_loads_untranslated_same_os":
                im.get("AbsTex", {}).get("filepath", "").endswith("abs.png") and im.get("AbsTex", {}).get("loads") is True,
            "stray_image_left_alone":
                im.get("StrayTex", {}).get("filepath", "").endswith("stray.png")
                and not im.get("StrayTex", {}).get("filepath", "").startswith(rr),
            "unmapped_counted_and_on_host_panel":
                st.get("unmapped_paths", 0) >= 1 and hj.get("peer_status", {}).get("unmapped", 0) >= 1,
            "replica_clean": st.get("apply_errors", 1) == 0 and st.get("gaps", 1) == 0,
        }
        print(json.dumps({"images": im, "unmapped": st.get("unmapped_paths"),
                          "pong_unmapped": hj.get("peer_status", {}).get("unmapped")}, indent=1))
        return report(work, checks)
    finally:
        stop([h, r])


def suite_reconnect(bl: str, work: str) -> int:
    def start_replica(tag: str, extra: dict | None = None):
        env = {"QCB_SMOKE_TAG": tag}
        env.update(extra or {})
        return launch(bl, os.path.join(HERE, "smoke_reconnect_replica.py"), work,
                      os.path.join(work, f"replica-{tag}.log"), env)

    checks: dict[str, bool] = {}
    r1 = start_replica("a")
    time.sleep(4)
    h = launch(bl, os.path.join(HERE, "smoke_reconnect_host.py"), work, os.path.join(work, "host.log"))
    r2 = None
    try:
        checks["first_boot"] = wait_for(work, lambda hj, rj:
            rj.get("tag") == "a" and rj.get("stats", {}).get("bootstraps", 0) >= 1
            and rj.get("probe") is not None, 60)
        print("killing replica a")
        kill9(r1)
        time.sleep(3)
        try:
            os.remove(os.path.join(work, "replica.json"))
        except OSError:
            pass
        r2 = start_replica("b", {"QCB_TEST_DROP_FIRST_T1": "1", "QCB_SMOKE_LOCAL_EDIT": "1"})
        checks["rebootstrap_after_restart"] = wait_for(work, lambda hj, rj:
            rj.get("tag") == "b" and rj.get("stats", {}).get("bootstraps", 0) >= 1
            and rj.get("probe") is not None and hj.get("sent_boot", 0) >= 2, 60)
        checks["auto_resync_after_gap"] = wait_for(work, lambda hj, rj:
            hj.get("done") and rj.get("stats", {}).get("bootstraps", 0) >= 2
            and rj.get("probe") == [7.0, 3.0, 0.0], 60)
        checks["host_counted_auto_resync"] = wait_for(work, lambda hj, rj:
            hj.get("auto_resyncs", 0) >= 1, 5)
        checks["flag_cleared_gap_kept"] = wait_for(work, lambda hj, rj:
            not rj.get("stats", {}).get("want_resync") and rj.get("stats", {}).get("gaps", 0) >= 1, 10)
        checks["local_edit_reported_to_host"] = wait_for(work, lambda hj, rj:
            hj.get("peer_status", {}).get("local_edits", 0) >= 1
            and "Probe" in hj.get("peer_status", {}).get("last_local_edit", ""), 25)
    finally:
        stop([h, r2, r1])
    hj, rj = load_json(work, "host.json"), load_json(work, "replica.json")
    print("--- host notes:", hj.get("notes"))
    print("--- replica b:", rj.get("stats"), rj.get("probe"), rj.get("overlay"))
    return report(work, checks)


def suite_bench(bl: str, work: str, mode: str) -> int:
    print(f"mode: {mode}")
    env = {}
    if mode == "agent":
        env = {"QCB_TRANSPORT": "agent", "QCB_AGENT": "spawn"}
        os.environ.update(env)
    else:
        for k in ("QCB_TRANSPORT", "QCB_AGENT"):
            os.environ.pop(k, None)
    prepare_pysite(work, bl)
    r = launch(bl, os.path.join(HERE, "bench_latency_replica.py"), work,
               os.path.join(work, "replica.log"), env, debug=False)
    time.sleep(4)
    h = launch(bl, os.path.join(HERE, "bench_latency_host.py"), work,
               os.path.join(work, "host.log"), env, debug=False)
    try:
        t0 = time.time()
        while True:
            if load_json(work, "host_lat.json").get("done") is True:
                break
            if time.time() - t0 > 300:
                print("TIMEOUT")
                break
            if h.poll() is not None:
                print("host exited early")
                break
            time.sleep(1)
        time.sleep(4)  # let the last phase drain and the replica dump once more
        return run_python(os.path.join(HERE, "bench_latency_report.py"), work)
    finally:
        stop([h, r])


def report(work: str, checks: dict) -> int:
    for k, v in checks.items():
        print(f"{k}:{'pass' if v else 'FAIL'}")
    with open(os.path.join(work, "verdict.json"), "w", encoding="utf-8") as f:
        json.dump({"pass": all(checks.values()), "checks": checks}, f, indent=1)
    return 0 if all(checks.values()) else 1


# --- main -------------------------------------------------------------------

SUITES = ("smoke", "reconnect", "cache", "mapping", "bench", "coverage")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in SUITES:
        print(__doc__)
        return 2
    suite = argv[1]
    rest = argv[2:]
    mode = "agent"
    if suite == "bench" and rest and rest[0] in ("agent", "zmq"):
        mode = rest.pop(0)
    work = fresh_work(rest[0] if rest else None, f"qcb-{suite}-")
    bl = blender_path()
    if not os.path.isfile(bl) and shutil.which(bl) is None:
        print(f"Blender not found: {bl} (set BLENDER)")
        return 2
    print(f"blender: {bl}")
    if suite != "bench":
        prepare_pysite(work, bl)
    if suite == "smoke":
        return suite_smoke(bl, work)
    if suite == "reconnect":
        return suite_reconnect(bl, work)
    if suite == "cache":
        return suite_cache(bl, work)
    if suite == "mapping":
        return suite_mapping(bl, work)
    if suite == "bench":
        return suite_bench(bl, work, mode)
    if suite == "coverage":
        return suite_coverage(bl, work)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
