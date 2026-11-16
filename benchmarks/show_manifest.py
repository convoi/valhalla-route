import json
import os
from pathlib import Path

manifest_file = Path(os.environ["TEMP"]) / "valhallaroute_europe_manifest.jsonl"
tasks = [json.loads(line) for line in manifest_file.read_text(encoding="utf-8").strip().splitlines()]

print(f"=== MANIFEST OVERVIEW: {manifest_file} ===")
print(f"Total tasks: {len(tasks)}")
print(f"File size: {manifest_file.stat().st_size / 1024:.1f} KB")

print("\n--- EXACT JSON RECORD (Task 0: Single Region) ---")
print(json.dumps(tasks[0], indent=2))

bin_packed = [t for t in tasks if len(t["regions"]) > 1]
print(f"\n--- EXACT JSON RECORD (Task {bin_packed[0]['task_id']}: Bin-Packed Multi-Region) ---")
print(json.dumps(bin_packed[0], indent=2))

print(f"\n--- EXACT JSON RECORD (Task {bin_packed[-1]['task_id']}: Bin-Packed Micro-Regions) ---")
print(json.dumps(bin_packed[-1], indent=2))

print("\n--- ALL BIN-PACKED TASKS SUMMARY ---")
for t in bin_packed:
    t_id = t["task_id"]
    t_est = t["estimated_tiles"]
    r_list = t["regions"]
    print(f"Task {t_id:3d} | {t_est:3d} tiles | {len(r_list):2d} regions: {r_list}")
