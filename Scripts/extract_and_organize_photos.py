import csv
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pyogrio


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GDB = PROJECT_ROOT / "Data" / "data.gdb"
PHOTOS_DIR = PROJECT_ROOT / "Photos"
OUTPUTS_DIR = PROJECT_ROOT / "Outputs"
MANIFEST_CSV = OUTPUTS_DIR / "organized_photos_manifest.csv"
STATE_JSON = OUTPUTS_DIR / ".extraction_state.json"

MANIFEST_COLUMNS = [
    "entry_id",
    "global_id",
    "object_id",
    "folder_name",
    "photo_index",
    "photo_filename",
    "source_att_name",
    "attachment_id",
    "relative_path",
    "sha256",
    "size_bytes",
]


def normalize_guid(value: Any) -> str:
    """Normalize GlobalID/REL_GLOBALID strings without braces for stable keys."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.strip("{}").strip().upper()
    return text


def sanitize_filename(name: str, fallback: str = "photo.jpg") -> str:
    """Sanitize attachment filenames while preserving extension if present."""
    clean = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())
    clean = re.sub(r"\s+", "_", clean).strip("._")
    if not clean:
        return fallback
    root, ext = os.path.splitext(clean)
    if not ext:
        return f"{root}.jpg"
    return f"{root}{ext.lower()}"


def find_col_ci(columns: list[str], candidates: list[str]) -> str | None:
    """Find the first matching column name case-insensitively."""
    lower_map = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def extract_blob_bytes(raw_blob: Any) -> bytes | None:
    """Convert geodatabase attachment BLOB/bytes/memoryview/hex into bytes."""
    if raw_blob is None:
        return None
    if isinstance(raw_blob, bytes):
        return raw_blob if len(raw_blob) > 0 else None
    if isinstance(raw_blob, bytearray):
        return bytes(raw_blob) if len(raw_blob) > 0 else None
    if isinstance(raw_blob, memoryview):
        data = raw_blob.tobytes()
        return data if len(data) > 0 else None
    if isinstance(raw_blob, str):
        stripped = raw_blob.strip()
        if stripped.startswith("\\x") or stripped.startswith("0x"):
            hex_str = stripped[2:]
            try:
                data = bytes.fromhex(hex_str)
                return data if len(data) > 0 else None
            except ValueError:
                return None
    return None


def serialize_value(val: Any) -> Any:
    """Make attribute or geometry values JSON/hash serializable."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (bytes, bytearray, memoryview)):
        b = bytes(val)
        return {"__bytes_len__": len(b), "__bytes_sha256__": hashlib.sha256(b).hexdigest()}
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if hasattr(val, "__geo_interface__"):
        return val.__geo_interface__
    return str(val)


def discover_layers(gdb_path: Path) -> tuple[str, str | None]:
    """Identify the main survey feature layer and optional attachment table in data.gdb."""
    layers_df = pyogrio.list_layers(gdb_path)
    layer_names = [row[0] for row in layers_df]
    if not layer_names:
        raise RuntimeError(f"No layers found inside {gdb_path}")

    att_layer = None
    feature_layer = None
    for name in layer_names:
        lower = name.lower()
        if ("attach" in lower or "__attach" in lower) and att_layer is None:
            att_layer = name
        elif feature_layer is None:
            feature_layer = name

    if feature_layer is None:
        feature_layer = layer_names[0]

    return feature_layer, att_layer


def load_attachments_by_parent(gdb_path: Path, att_layer: str | None) -> dict[str, list[dict[str, Any]]]:
    """Load attachments grouped by normalized parent GlobalID."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not att_layer:
        return grouped

    gdf_att = pyogrio.read_dataframe(gdb_path, layer=att_layer)
    cols = list(gdf_att.columns)

    rel_guid_col = find_col_ci(cols, ["REL_GLOBALID", "RELGLOBALID", "PARENTGLOBALID", "GLOBALID"])
    att_id_col = find_col_ci(cols, ["ATTACHMENTID", "OBJECTID", "ID"])
    att_name_col = find_col_ci(cols, ["ATT_NAME", "FILENAME", "NAME"])
    data_col = find_col_ci(cols, ["DATA", "ATTACHMENT", "BLOB"])

    if not rel_guid_col or not data_col:
        return grouped

    for idx, row in gdf_att.iterrows():
        parent_guid = normalize_guid(row.get(rel_guid_col))
        if not parent_guid:
            continue

        blob = extract_blob_bytes(row.get(data_col))
        if not blob:
            continue

        att_id = row.get(att_id_col) if att_id_col else idx
        att_name = str(row.get(att_name_col) or f"attachment_{att_id}.jpg")
        blob_hash = hashlib.sha256(blob).hexdigest()

        grouped.setdefault(parent_guid, []).append(
            {
                "attachment_id": serialize_value(att_id),
                "att_name": att_name,
                "size_bytes": len(blob),
                "sha256": blob_hash,
                "bytes": blob,
            }
        )

    for parent_guid, items in grouped.items():
        items.sort(key=lambda x: (str(x["attachment_id"]), x["att_name"], x["sha256"]))

    return grouped


def build_entry_snapshot(
    gid: str,
    oid: Any,
    attr_dict: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Compute a deterministic entry signature and metadata record."""
    folder_name = f"Entry_{gid}"
    att_meta = [
        {
            "attachment_id": a["attachment_id"],
            "att_name": a["att_name"],
            "size_bytes": a["size_bytes"],
            "sha256": a["sha256"],
        }
        for a in attachments
    ]

    payload = {
        "global_id": gid,
        "object_id": serialize_value(oid),
        "attributes": attr_dict,
        "attachments": att_meta,
    }
    canonical_json = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    signature = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    state_entry = {
        "signature": signature,
        "folder_name": folder_name,
        "object_id": serialize_value(oid),
        "photo_count": len(attachments),
        "attachment_hashes": [a["sha256"] for a in attachments],
    }
    return signature, state_entry


def write_entry_photos(gid: str, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild Photos/Entry_<GlobalID> cleanly and return manifest rows for this entry."""
    folder_name = f"Entry_{gid}"
    entry_dir = PHOTOS_DIR / folder_name

    if entry_dir.exists():
        shutil.rmtree(entry_dir)
    entry_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for idx, att in enumerate(attachments, start=1):
        safe_name = sanitize_filename(att["att_name"], fallback=f"photo_{idx:02d}.jpg")
        out_name = f"{gid}_{idx:02d}_{safe_name}"
        out_path = entry_dir / out_name
        out_path.write_bytes(att["bytes"])

        rel_path = out_path.relative_to(PROJECT_ROOT).as_posix()
        rows.append(
            {
                "entry_id": gid,
                "global_id": gid,
                "object_id": "",
                "folder_name": folder_name,
                "photo_index": idx,
                "photo_filename": out_name,
                "source_att_name": att["att_name"],
                "attachment_id": att["attachment_id"],
                "relative_path": rel_path,
                "sha256": att["sha256"],
                "size_bytes": att["size_bytes"],
            }
        )

    return rows


def load_previous_state() -> dict[str, dict[str, Any]]:
    """Load previous entry state map from Outputs/.extraction_state.json."""
    if not STATE_JSON.exists():
        return {}
    try:
        data = json.loads(STATE_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data["entries"]
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def load_existing_manifest() -> list[dict[str, str]]:
    """Load existing rows from Outputs/organized_photos_manifest.csv if present."""
    if not MANIFEST_CSV.exists():
        return []
    with MANIFEST_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def write_manifest(rows: list[dict[str, Any]]) -> None:
    """Write normalized manifest rows to Outputs/organized_photos_manifest.csv."""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({col: r.get(col, "") for col in MANIFEST_COLUMNS})


def entry_folder_intact(gid: str, expected_hashes: list[str]) -> bool:
    """Verify that Entry_<GlobalID> and its files exist and match expected SHA-256 hashes."""
    entry_dir = PHOTOS_DIR / f"Entry_{gid}"
    if not entry_dir.is_dir():
        return False
    existing_files = [p for p in entry_dir.iterdir() if p.is_file()]
    if len(existing_files) != len(expected_hashes):
        return False
    actual_hashes = sorted(
        hashlib.sha256(p.read_bytes()).hexdigest() for p in existing_files
    )
    return actual_hashes == sorted(expected_hashes)


def run_incremental_extraction(gdb_path: Path = DEFAULT_GDB) -> None:
    if not gdb_path.exists():
        raise FileNotFoundError(f"Geodatabase not found at: {gdb_path}")

    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    feature_layer, att_layer = discover_layers(gdb_path)
    gdf = pyogrio.read_dataframe(gdb_path, layer=feature_layer)
    cols = list(gdf.columns)

    gid_col = find_col_ci(cols, ["GLOBALID", "GLOBAL_ID", "GUID"])
    if not gid_col:
        raise KeyError(f"Could not find a GlobalID column in layer '{feature_layer}'. Columns: {cols}")

    oid_col = find_col_ci(cols, ["OBJECTID", "OID", "FID"])
    attachments_by_gid = load_attachments_by_parent(gdb_path, att_layer)

    current_entries: dict[str, dict[str, Any]] = {}
    current_signatures: dict[str, str] = {}
    current_attachments: dict[str, list[dict[str, Any]]] = {}
    current_oids: dict[str, Any] = {}

    for _, row in gdf.iterrows():
        gid = normalize_guid(row.get(gid_col))
        if not gid:
            continue

        oid = row.get(oid_col) if oid_col else ""
        attr_dict: dict[str, Any] = {}
        for c in cols:
            if c == "geometry":
                attr_dict["geometry"] = serialize_value(row.get(c))
            else:
                attr_dict[c] = serialize_value(row.get(c))

        atts = attachments_by_gid.get(gid, [])
        sig, state_meta = build_entry_snapshot(gid, oid, attr_dict, atts)

        current_entries[gid] = state_meta
        current_signatures[gid] = sig
        current_attachments[gid] = atts
        current_oids[gid] = serialize_value(oid)

    prev_entries = load_previous_state()
    existing_manifest = load_existing_manifest()

    prev_gids = set(prev_entries.keys())
    curr_gids = set(current_entries.keys())

    deleted_gids = sorted(prev_gids - curr_gids)
    added_gids = sorted(curr_gids - prev_gids)
    common_gids = sorted(curr_gids & prev_gids)

    modified_gids: list[str] = []
    unchanged_gids: list[str] = []

    for gid in common_gids:
        prev_sig = prev_entries[gid].get("signature")
        expected_hashes = prev_entries[gid].get("attachment_hashes", [])
        if prev_sig != current_signatures[gid] or not entry_folder_intact(gid, expected_hashes):
            modified_gids.append(gid)
        else:
            unchanged_gids.append(gid)

    # 1. Remove deleted entry folders
    for gid in deleted_gids:
        folder = PHOTOS_DIR / f"Entry_{gid}"
        if folder.exists():
            shutil.rmtree(folder)

    # 2. Keep manifest rows for unchanged entries only
    changed_or_removed_gids = set(deleted_gids) | set(added_gids) | set(modified_gids)
    kept_rows: list[dict[str, Any]] = []
    for row in existing_manifest:
        row_gid = normalize_guid(row.get("global_id") or row.get("entry_id"))
        if row_gid and row_gid not in changed_or_removed_gids:
            kept_rows.append(row)

    # 3. Extract photos and build manifest rows for added and modified entries
    new_or_updated_rows: list[dict[str, Any]] = []
    for gid in sorted(set(added_gids) | set(modified_gids)):
        entry_rows = write_entry_photos(gid, current_attachments[gid])
        for r in entry_rows:
            r["object_id"] = current_oids[gid]
        new_or_updated_rows.extend(entry_rows)

    all_rows = kept_rows + new_or_updated_rows
    all_rows.sort(
        key=lambda r: (
            str(r.get("global_id", "")),
            int(r.get("photo_index") or 0),
        )
    )
    write_manifest(all_rows)

    # 4. Save updated state after files and CSV are written
    state_payload = {
        "source_gdb": str(gdb_path),
        "feature_layer": feature_layer,
        "attachment_layer": att_layer,
        "entries": current_entries,
    }
    STATE_JSON.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    print(
        f"Incremental extraction complete | "
        f"Added: {len(added_gids)}, Modified: {len(modified_gids)}, "
        f"Deleted: {len(deleted_gids)}, Unchanged (skipped): {len(unchanged_gids)}"
    )


if __name__ == "__main__":
    run_incremental_extraction()