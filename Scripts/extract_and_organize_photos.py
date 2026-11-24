import csv
from pathlib import Path
import re
import shutil
import geopandas as gpd
import pandas as pd
import pyogrio

# --- PATH CONFIGURATION ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GDB_PATH = PROJECT_ROOT / "Data" / "data.gdb"
PHOTOS_DIR = PROJECT_ROOT / "Photos"
OUTPUT_DIR = PROJECT_ROOT / "Outputs"
MANIFEST_CSV = OUTPUT_DIR / "organized_photos_manifest.csv"

# Set to True to group photos into Entry_0001/ subfolders, or False to put all photos directly in Photos/
GROUP_BY_SUBFOLDER = True

# If True, clears existing Entry_* subfolders (or existing photos) before writing the new export
CLEAN_BEFORE_OVERWRITE = True


def sanitize_filename(name: str) -> str:
  return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def find_layers(gdb_path: Path):
  if not gdb_path.exists() or not gdb_path.is_dir():
    raise FileNotFoundError(f"Geodatabase folder not found at: {gdb_path}")

  layers = [name for name, _ in pyogrio.list_layers(str(gdb_path))]
  attach_layers = [n for n in layers if n.endswith("__ATTACH")]
  main_layers = [
      n for n in layers if not n.endswith("__ATTACH") and not n.endswith("__REL")
  ]
  if not attach_layers or not main_layers:
    raise RuntimeError(
        f"Could not find main layer and __ATTACH table in {gdb_path}. Found:"
        f" {layers}"
    )
  return main_layers[0], attach_layers[0]


def get_col_ci(df: pd.DataFrame, *candidates: str) -> str | None:
  """Case-insensitive column lookup."""
  lower_map = {c.lower(): c for c in df.columns}
  for cand in candidates:
    if cand.lower() in lower_map:
      return lower_map[cand.lower()]
  return None


def is_missing(val) -> bool:
  return val is None or pd.isna(val) or str(val).strip() == ""


def prepare_output_targets(photos_dir: Path, manifest_csv: Path):
  """Detects existing output folders/files and clears stale items for clean overwrite."""
  photos_dir.mkdir(parents=True, exist_ok=True)
  manifest_csv.parent.mkdir(parents=True, exist_ok=True)

  existing_entry_dirs = [p for p in photos_dir.glob("Entry_*") if p.is_dir()]
  existing_flat_files = [
      p for p in photos_dir.iterdir() if p.is_file() and p.name != ".gitkeep"
  ]
  manifest_existed = manifest_csv.exists()

  if existing_entry_dirs or existing_flat_files or manifest_existed:
    print(
        "Existing output detected: "
        f"{len(existing_entry_dirs)} Entry_* folder(s), "
        f"{len(existing_flat_files)} root file(s), "
        f"manifest={'yes' if manifest_existed else 'no'}."
    )

  if CLEAN_BEFORE_OVERWRITE:
    for entry_dir in existing_entry_dirs:
      shutil.rmtree(entry_dir)
    if not GROUP_BY_SUBFOLDER:
      for flat_file in existing_flat_files:
        flat_file.unlink()
    if manifest_existed:
      manifest_csv.unlink()
    print("Cleared previous output folders/files for clean overwrite.")


def main():
  prepare_output_targets(PHOTOS_DIR, MANIFEST_CSV)

  print(f"Using geodatabase: '{GDB_PATH}'")
  main_layer_name, attach_layer_name = find_layers(GDB_PATH)
  print(f"Main layer: '{main_layer_name}'")
  print(f"Attachment table: '{attach_layer_name}'")

  gdf = gpd.read_file(str(GDB_PATH), layer=main_layer_name)
  attach_gdf = gpd.read_file(str(GDB_PATH), layer=attach_layer_name)

  oid_col = get_col_ci(gdf, "OBJECTID", "oid", "fid")
  gid_col = get_col_ci(gdf, "GlobalID", "GLOBALID")
  rel_oid_col = get_col_ci(attach_gdf, "REL_OBJECTID", "REL_OID")
  rel_gid_col = get_col_ci(attach_gdf, "REL_GLOBALID", "REL_GUID")
  att_id_col = get_col_ci(attach_gdf, "ATTACHMENTID", "ATT_ID")
  att_name_col = get_col_ci(attach_gdf, "ATT_NAME", "NAME", "FILENAME")
  data_col = get_col_ci(attach_gdf, "DATA", "ATT_DATA")

  if not data_col:
    raise RuntimeError(
        f"No binary attachment column found in {attach_layer_name}. Columns:"
        f" {list(attach_gdf.columns)}"
    )

  parent_by_oid = {}
  parent_by_gid = {}
  all_attr_columns = [c for c in gdf.columns if c != "geometry"]

  for idx, row in gdf.iterrows():
    raw_oid = row.get(oid_col) if oid_col else None
    entry_num = int(raw_oid) if not is_missing(raw_oid) else (idx + 1)
    raw_gid = row.get(gid_col) if gid_col else None
    norm_gid = "" if is_missing(raw_gid) else str(raw_gid).strip("{}").lower()

    geom = row.geometry
    lon = geom.x if (geom is not None and not geom.is_empty) else ""
    lat = geom.y if (geom is not None and not geom.is_empty) else ""

    record = {
        "entry_number": entry_num,
        "OBJECTID": "" if is_missing(raw_oid) else raw_oid,
        "GlobalID": "" if is_missing(raw_gid) else str(raw_gid),
        "lat": lat,
        "lon": lon,
    }
    for col in all_attr_columns:
      if col not in record:
        val = row.get(col)
        record[col] = "" if is_missing(val) else str(val)

    if not is_missing(raw_oid):
      parent_by_oid[int(raw_oid)] = record
    if norm_gid:
      parent_by_gid[norm_gid] = record

  photo_counter_per_entry = {}
  manifest_rows = []
  unmatched_count = 0

  for idx, att in attach_gdf.iterrows():
    blob = att.get(data_col)
    if blob is None or (isinstance(blob, float) and pd.isna(blob)):
      continue

    rel_oid_raw = att.get(rel_oid_col) if rel_oid_col else None
    rel_gid_raw = att.get(rel_gid_col) if rel_gid_col else None
    att_id = att.get(att_id_col) if att_id_col else (idx + 1)
    raw_att_name = (
        str(att.get(att_name_col))
        if (att_name_col and not is_missing(att.get(att_name_col)))
        else f"photo_{att_id}.jpg"
    )
    clean_att_name = sanitize_filename(Path(raw_att_name).name)

    rel_oid_int = int(rel_oid_raw) if not is_missing(rel_oid_raw) else None
    rel_gid_norm = (
        "" if is_missing(rel_gid_raw) else str(rel_gid_raw).strip("{}").lower()
    )

    parent = None
    if rel_oid_int is not None and rel_oid_int in parent_by_oid:
      parent = parent_by_oid[rel_oid_int]
    elif rel_gid_norm and rel_gid_norm in parent_by_gid:
      parent = parent_by_gid[rel_gid_norm]

    if parent:
      entry_num = parent["entry_number"]
    else:
      unmatched_count += 1
      entry_num = rel_oid_int if rel_oid_int is not None else 0

    photo_counter_per_entry[entry_num] = (
        photo_counter_per_entry.get(entry_num, 0) + 1
    )
    photo_seq = photo_counter_per_entry[entry_num]

    entry_tag = f"Entry_{entry_num:04d}"
    filename = f"{entry_tag}_Photo_{photo_seq:02d}_{clean_att_name}"

    if GROUP_BY_SUBFOLDER:
      target_dir = PHOTOS_DIR / entry_tag
      target_dir.mkdir(parents=True, exist_ok=True)
    else:
      target_dir = PHOTOS_DIR

    out_path = target_dir / filename
    with open(out_path, "wb") as f:
      f.write(bytes(blob))

    manifest_row = {
        "entry_number": entry_num,
        "entry_tag": entry_tag,
        "photo_seq": photo_seq,
        "attachment_id": att_id,
        "saved_relative_path": str(out_path.relative_to(PROJECT_ROOT)),
        "original_att_name": raw_att_name,
        "lat": parent["lat"] if parent else "",
        "lon": parent["lon"] if parent else "",
        "GlobalID": parent["GlobalID"] if parent else "",
    }
    if parent:
      for col in all_attr_columns:
        if col not in manifest_row:
          manifest_row[col] = parent[col]

    manifest_rows.append(manifest_row)

  manifest_rows.sort(key=lambda r: (r["entry_number"], r["photo_seq"]))

  if manifest_rows:
    fieldnames = list(manifest_rows[0].keys())
    with open(MANIFEST_CSV, "w", encoding="utf-8", newline="") as f:
      writer = csv.DictWriter(f, fieldnames=fieldnames)
      writer.writeheader()
      writer.writerows(manifest_rows)

  print(
      f"Done! Extracted {len(manifest_rows)} photos across"
      f" {len(photo_counter_per_entry)} entries into {PHOTOS_DIR}"
  )
  if unmatched_count:
    print(
        f"Warning: {unmatched_count} photos could not be matched to a parent"
        " survey row."
    )
  print(f"Full index saved to: {MANIFEST_CSV}")


if __name__ == "__main__":
  main()