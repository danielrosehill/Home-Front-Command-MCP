"""Refresh the bundled oref reference data (areas, categories, instructions) from www.oref.org.il.

Run from an Israeli IP:  uv run python scripts/refresh_data.py
Writes src/home_front_command_mcp/data/{areas,categories,instructions}.json. The guidelines snapshot
(data/guidelines/) is separate: it comes from danielrosehill/Pikud-Haoref-Guidelines-0326.
"""

import json
from datetime import date
from pathlib import Path

import httpx

from home_front_command_mcp.server import HEADERS

OUT = Path(__file__).resolve().parent.parent / "src" / "home_front_command_mcp" / "data"
BASE = "https://www.oref.org.il"


def get(path: str):
    r = httpx.get(BASE + path, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8-sig"))


def main() -> None:
    today = date.today().isoformat()
    en, he = get("/districts/districts_eng.json"), get("/districts/districts_heb.json")
    hk = {x["id"]: x for x in he}
    areas = [{"id": int(x["id"]), "en": x["label"], "he": x["label_he"], "district": x["areaname"],
              "district_he": hk.get(x["id"], {}).get("areaname"), "council": x["rashut"],
              "protection_seconds": x["migun_time"]} for x in sorted(en, key=lambda x: int(x["id"]))]
    (OUT / "areas.json").write_text(json.dumps(
        {"source": f"{BASE}/districts/districts_eng.json + districts_heb.json", "fetched": today, "areas": areas},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    cats = get("/alerts/alertCategories.json")
    (OUT / "categories.json").write_text(json.dumps(
        {"source": f"{BASE}/alerts/alertCategories.json", "fetched": today,
         "categories": [{k: c[k] for k in ("id", "category", "matrix_id", "eng", "heb")} for c in cats]},
        ensure_ascii=False, indent=0), encoding="utf-8")
    rc = get("/alerts/RemainderConfig_eng.json")
    (OUT / "instructions.json").write_text(json.dumps(
        {"source": f"{BASE}/alerts/RemainderConfig_eng.json", "fetched": today,
         "instructions": [{"cat": r["cat"], "title_he": r["titleHeb"].strip(), "title": r["title"].strip(),
                           "instructions": r["instructions"].strip(),
                           "url": (r.get("crisisManagementLink") or {}).get("url")} for r in rc]},
        ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"areas {len(areas)}, categories {len(cats)}, instructions {len(rc)}")


if __name__ == "__main__":
    main()
