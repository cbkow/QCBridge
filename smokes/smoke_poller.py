"""Poll host.json/replica.json, record milestone snapshots, emit verdict."""
import json
import os
import sys
import time

OUT = sys.argv[1]
TIMEOUT = 300.0
TOL = 1e-3

milestones: dict = {}


def read(name):
    try:
        with open(os.path.join(OUT, name)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def close(a, b, tol=TOL):
    return a is not None and b is not None and len(a) == len(b) and all(
        abs(x - y) <= tol for x, y in zip(a, b)
    )


def note(key, snap):
    if key not in milestones:
        milestones[key] = snap
        print(f"[{time.strftime('%H:%M:%S')}] milestone: {key}", flush=True)


start = time.monotonic()
host = replica = None
while time.monotonic() - start < TIMEOUT:
    host, replica = read("host.json"), read("replica.json")
    if replica:
        s = replica.get("stats", {})
        if s.get("bootstraps", 0) >= 1:
            note("boot", s["bootstraps"])
        if replica.get("cam_parent") == "RigNull":
            note("cam_parented", True)
        for ctype, target, infl in replica.get("cam_constraints", []):
            if ctype == "TRACK_TO" and target == "TargetCube":
                if abs(infl - 0.75) < 1e-4:
                    note("trackto_75", infl)
                if abs(infl - 0.3) < 1e-4:
                    note("trackto_30", infl)
        if "cam_parented" in milestones and replica.get("box_parent") is None:
            note("box_unparented", True)
        if replica.get("cloth_baked") and replica.get("frame") == 20:
            note("baked_at_f20", {"mean_z": replica.get("cloth_mean_z"),
                                  "bootstraps": s.get("bootstraps")})
        if "baked_at_f20" in milestones and replica.get("cloth_baked") is False:
            note("unbaked_after", True)
        cz = replica.get("cube_mean_z")
        if host and cz is not None:
            z1, z2 = host.get("host_gn_z1"), host.get("host_gn_z2")
            if z1 is not None and abs(cz - z1) <= TOL and replica.get("cube_has_gn"):
                note("gn_added", cz)
            if z2 is not None and abs(cz - z2) <= TOL:
                note("gn_tweaked", cz)
        rm = replica.get("railcam_matrix")
        if host and rm is not None:
            if close(rm, host.get("rail_m1")):
                note("rail_added", True)
            if close(rm, host.get("rail_m2")):
                note("rail_retimed", True)
        if host:
            cv, cm = replica.get("ctrl_val"), replica.get("cam_mix")
            if host.get("ctrl_v1") and cv is not None and cm is not None:
                if abs(cm - 0.5) < 1e-4 and abs(cv - 0.25) < 1e-4:
                    note("ctrl_set", [cv, cm])
                if abs(cv - 0.85) < 1e-4 and abs(cm - 0.5) < 1e-4:
                    note("ctrl_tweak", cv)
            sv = replica.get("smile_value")
            if sv is not None:
                if abs(sv - 0.6) < 1e-4:
                    note("smile_06", sv)
                if abs(sv - 0.9) < 1e-4 and replica.get("sk_count") == 1:
                    note("smile_09_no_leak", replica.get("sk_count"))
            if close(replica.get("lat_pt"), host.get("lat_pt"), 1e-3):
                note("lattice_pt", replica.get("lat_pt"))
            if close(replica.get("arm_matrix"), host.get("arm_matrix")):
                note("arm_pose", True)
    if host and host.get("done") and len(milestones) >= 17:
        time.sleep(2)  # let the last state settle, take a final snapshot
        replica = read("replica.json") or replica
        break
    time.sleep(0.5)

checks = {}
if not host or not replica:
    checks["dumps_present"] = False
else:
    s = replica.get("stats", {})
    checks["host_done"] = bool(host.get("done"))
    checks["boot"] = "boot" in milestones
    checks["cam_parented_mid_session"] = "cam_parented" in milestones
    checks["cam_matrix_matches"] = close(replica.get("cam_matrix"),
                                         host.get("cam_matrix"))
    checks["trackto_add_0.75"] = "trackto_75" in milestones
    checks["trackto_tweak_0.30"] = ("trackto_30" in milestones
                                    and host.get("cam_influence") == 0.3)
    checks["box_unparented_keep_transform"] = (
        "box_unparented" in milestones
        and close(replica.get("box_matrix"), host.get("box_matrix")))
    baked = milestones.get("baked_at_f20") or {}
    checks["bake_crossed_via_resync"] = (
        baked.get("bootstraps", 0) >= 2
        and baked.get("mean_z") is not None
        and abs(baked["mean_z"] - host.get("host_mean_z_f20", 9e9)) <= TOL)
    checks["bake_note_lifecycle"] = (
        host.get("bake_note_seen") and host.get("bake_note_cleared")
        and host.get("bake_note_seen_again"))
    checks["free_bake_crossed"] = ("unbaked_after" in milestones
                                   and host.get("host_unbaked"))
    checks["gn_add_mid_session"] = "gn_added" in milestones
    checks["gn_input_tweak_synced"] = "gn_tweaked" in milestones
    checks["rail_rig_built_mid_session"] = "rail_added" in milestones
    checks["rail_keyframe_retime_synced"] = "rail_retimed" in milestones
    checks["custom_props_via_sweep"] = "ctrl_set" in milestones
    checks["custom_prop_t1_tweak"] = "ctrl_tweak" in milestones
    checks["shape_key_value_synced"] = "smile_06" in milestones
    checks["shape_key_pairing_no_leak"] = (
        "smile_09_no_leak" in milestones and host.get("host_sk_count") == 1)
    checks["lattice_synced"] = "lattice_pt" in milestones
    checks["armature_pose_synced"] = "arm_pose" in milestones
    checks["replica_clean"] = (s.get("gaps") == 0 and s.get("apply_errors") == 0
                               and s.get("unknown_uuid") == 0)

verdict = {"pass": bool(checks) and all(checks.values()),
           "checks": checks, "milestones": milestones,
           "final_replica": replica, "final_host": host}
with open(os.path.join(OUT, "verdict.json"), "w") as f:
    json.dump(verdict, f, indent=1)
print(json.dumps({"pass": verdict["pass"], "checks": checks}, indent=1))
sys.exit(0 if verdict["pass"] else 1)
