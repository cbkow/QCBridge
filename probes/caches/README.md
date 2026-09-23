# Cache probes

The measurements behind `CACHES.md`. Headless Blender 5.2, macOS; each probe
runs a "host" half and a "replica" half in **separate processes**, because a
same-process test cannot show what crosses a `libraries.write` or a full
copy.

They write only under the scratch directory you name. `S` is the point-cache
scratch, `P` its parent (the GN probes use `$S` too, pointed at a `gn/`
sibling — see each script's first lines).

```zsh
BL=/Applications/Blender.app/Contents/MacOS/Blender
export S=/tmp/qcb-probe/pc P=/tmp/qcb-probe; mkdir -p $S/host $S/replica $S/ext

# point caches: memory / disk-under-a-renamed-copy / external, then the flags
for m in host replica replica_boot replica_same; do $BL -b --factory-startup -noaudio --python probes/caches/pointcache_shapes.py -- $m; done
# ...and the positional verdicts (z at frame 12 after a direct jump)
for m in host replica replica_boot replica_same; do $BL -b --factory-startup -noaudio --python probes/caches/pointcache_positional.py -- $m; done

# geometry nodes: packed vs disk, what a partial write carries, z at frame 10
export S=/tmp/qcb-probe/gn; mkdir -p $S/host $S/replica
for m in host replica replica_boot; do $BL -b --factory-startup -noaudio --python probes/caches/gn_shapes.py -- $m; done
for m in host replica replica_boot; do $BL -b --factory-startup -noaudio --python probes/caches/gn_positional.py -- $m; done

# rescan triggers, converting a baked cache, GN bake detection, bake_directory on the replica
for m in rescan_alternatives convert gn_detect_and_dir gn_dir_on_replica; do $BL -b --factory-startup -noaudio --python probes/caches/rescan_convert_detect.py -- $m; done
# does scrubbing an unbaked zone fire depsgraph updates? does a bake name the object?
$BL -b --factory-startup -noaudio --python probes/caches/gn_depsgraph_signal.py
```

Each prints a `PROBE_JSON {...}` line. The discriminators are positional:
cloth lowest-vertex z at frame 12 (baked −0.5346, unbaked jump −0.6061, no
frames 0.0) and cube top z at frame 10 (baked 2.0, unbaked jump 1.2). Flags
(`is_baked`, `info`) are recorded but proven untrustworthy after a rename.
