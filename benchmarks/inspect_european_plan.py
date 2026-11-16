import json
import tempfile
from pathlib import Path

manifest_path = Path(tempfile.gettempdir()) / "valhalla_test_manifest.jsonl"
lines = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").strip().splitlines()]

print(f"Total Planned Tasks: {len(lines)}")
single_tasks = [t for t in lines if len(t["regions"]) == 1]
bin_packed_tasks = [t for t in lines if len(t["regions"]) > 1]
print(f"Single-region tasks: {len(single_tasks)}")
print(f"Bin-packed tasks: {len(bin_packed_tasks)}")

tile_counts = [t["estimated_tiles"] for t in lines]
print(f"Tile counts: min={min(tile_counts)}, max={max(tile_counts)}, avg={sum(tile_counts)/len(tile_counts):.1f}")

print("\n--- SAMPLE TASKS (First 20) ---")
for t in lines[:20]:
    reg_str = ", ".join(t["regions"])
    t_id = t["task_id"]
    t_est = t["estimated_tiles"]
    p_cnt = len(t["osm_urls"])
    print(f"Task {t_id:3d}: {reg_str[:45]:45} | Est. Tiles: {t_est:3d} | PBFs: {p_cnt}")

print("\n--- BIN-PACKED TASKS (Sample) ---")
for t in bin_packed_tasks[:8]:
    t_id = t["task_id"]
    t_est = t["estimated_tiles"]
    print(f"Task {t_id:3d} ({t_est} tiles): {t['regions']}")
