"""Probe: which record fields exist across the library, to find where edits live."""

import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from icloud_spike import connect  # noqa: E402
from pyicloud.services.photos_cloudkit.service import record_field_value  # noqa: E402


def fields(rec):
    if hasattr(rec, "fields"):
        f = rec.fields
        return list(f.keys()) if hasattr(f, "keys") else list(vars(f).keys())
    return list(rec.get("fields", {}).keys())


api = connect()
master_fields, asset_fields = Counter(), Counter()
adjusted = []
for i, a in enumerate(api.photos.all):
    master_fields.update(fields(a.master_record))
    af = fields(a.asset_record)
    asset_fields.update(af)
    if any("Full" in k or "adjust" in k.lower() for k in af):
        if len(adjusted) < 3:
            adjusted.append((a.filename, [k for k in af if "Full" in k or "adjust" in k.lower()]))
print("items:", i + 1)
print("master fields:", dict(master_fields))
print("asset fields with Full/adjust:", {k: v for k, v in asset_fields.items() if "Full" in k or "adjust" in k.lower()})
print("examples:", adjusted)
