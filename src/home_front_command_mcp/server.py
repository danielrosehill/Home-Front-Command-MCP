"""Home Front Command MCP: Israel's Pikud HaOref (Home Front Command) alerts and guidelines for agents.

Built for voice assistants: every result carries a `say` field, one calm plain-English sentence.
Configuration (environment):
  HFC_AREAS       default areas, comma-separated, Hebrew or English names ("Jerusalem", "ירושלים - מערב");
                  empty = the whole country. A tool's `area` argument overrides it; area="all" = whole country.
  HFC_BASE_URL    oref base URL (default https://www.oref.org.il). The site is geofenced to Israeli IPs.
  HFC_USER_AGENT  browser User-Agent to send (oref's CloudFront WAF returns 403 to curl/python UAs)
  HFC_TIMEOUT     HTTP timeout in seconds (default 10)
  HOST, PORT      bind address for streamable HTTP (default 0.0.0.0:8000); path /mcp
  MCP_TRANSPORT   streamable-http (default) or stdio
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

TZ = ZoneInfo("Asia/Jerusalem")
BASE = os.environ.get("HFC_BASE_URL", "https://www.oref.org.il").rstrip("/")
UA = os.environ.get(
    "HFC_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.oref.org.il/", "X-Requested-With": "XMLHttpRequest",
           "Accept": "application/json, text/plain, */*"}
TIMEOUT = float(os.environ.get("HFC_TIMEOUT", "10"))
LIVE_PATH = "/warningMessages/alert/Alerts.json"
LEGACY_HISTORY_PATH = "/warningMessages/alert/History/AlertsHistory.json"
HISTORY_API = "/api/v1/AlertHistory"
PAGE_ROWS = 200  # the history API returns at most 200 rows per call (observed 2026-11-22)
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)  # answers from bundled data only


def _data(name: str):
    return json.loads(resources.files("home_front_command_mcp").joinpath("data", name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- reference data (bundled oref snapshots)

@dataclass(frozen=True)
class Area:
    id: int
    en: str
    he: str
    district: str
    district_he: str
    council: str | None
    protection_seconds: int


_AREAS_DOC = _data("areas.json")
AREAS = [Area(a["id"], a["en"], a["he"], a["district"] or "", a["district_he"] or "", a["council"],
              a["protection_seconds"]) for a in _AREAS_DOC["areas"]]
BY_HE = {a.he: a for a in AREAS}
BY_EN = {a.en.lower(): a for a in AREAS}
CATEGORIES = _data("categories.json")["categories"]
INSTRUCTIONS = _data("instructions.json")["instructions"]

# Plain-English threat names by oref category key (alertCategories.json `category`).
THREAT = {
    "missilealert": "rockets or missiles",
    "uav": "hostile aircraft or drone",
    "nonconventional": "non-conventional missile",
    "warning": "general alert",
    "earthquakealert1": "earthquake",
    "earthquakealert2": "earthquake",
    "cbrne": "radiological event",
    "terrorattack": "terrorist infiltration",
    "tsunami": "tsunami",
    "hazmat": "hazardous materials",
    "update": "update",
    "flash": "early warning or instruction",
    "memorialday1": "memorial day siren",
    "memorialday2": "memorial day siren",
}
SHORT = {  # for the spoken sentence: "<short> alert now in ..."
    "missilealert": "Rocket", "uav": "Hostile aircraft", "nonconventional": "Non-conventional missile",
    "terrorattack": "Terrorist infiltration", "earthquakealert1": "Earthquake", "earthquakealert2": "Earthquake",
    "tsunami": "Tsunami", "hazmat": "Hazardous materials", "cbrne": "Radiological", "warning": "General",
}
CAT_BY_ID = {c["id"]: c for c in CATEGORIES}


def _cat_by_matrix(matrix_id: int) -> dict | None:
    # Several categories share a matrix id (13 update / 14 flash both = 10); drills are 100+.
    return next((c for c in CATEGORIES if c["matrix_id"] == matrix_id), None)


def _threat_key(key: str) -> tuple[str, bool]:
    drill = key.endswith("drill")
    base = key[:-5] if drill else key
    if base.startswith("earthquakedrill"):
        base = "earthquakealert1"
    return base, drill


def _norm(s: str) -> str:
    s = s.strip().lower().replace("’", "'").replace("`", "'").replace("״", '"').replace("׳", "'")
    return re.sub(r"\s+", " ", s)


def resolve_areas(term: str) -> list[Area]:
    """Map a user/config area name (Hebrew or English) to oref alert areas.

    Order: exact area name; split-city prefix ("Jerusalem" -> "Jerusalem - West", ...); district name
    ("Dan", "Jerusalem" district is only used when no split-city match); substring (3+ chars)."""
    t = _norm(term)
    if not t:
        return []
    exact = [a for a in AREAS if _norm(a.en) == t or _norm(a.he) == t]
    if exact:
        return exact
    split = [a for a in AREAS if _norm(a.en).startswith(t + " - ") or _norm(a.he).startswith(t + " - ")]
    if split:
        return split
    district = [a for a in AREAS if _norm(a.district) == t or _norm(a.district_he) == t]
    if district:
        return district
    if len(t) >= 3:
        return [a for a in AREAS if t in _norm(a.en) or t in _norm(a.he)]
    return []


def wanted_areas(area: str | None) -> tuple[list[Area] | None, str, list[str]]:
    """(areas or None for whole country, spoken label, unknown terms)."""
    raw = area if area is not None else os.environ.get("HFC_AREAS", "")
    terms = [x.strip() for x in raw.split(",") if x.strip()]
    if not terms or any(_norm(x) in ("all", "israel", "everywhere", "כל הארץ") for x in terms):
        return None, "Israel", []
    found: dict[int, Area] = {}
    unknown = []
    for x in terms:
        hits = resolve_areas(x)
        if not hits:
            unknown.append(x)
        for a in hits:
            found[a.id] = a
    return list(found.values()), ", ".join(terms), unknown


# ---------------------------------------------------------------- speech helpers

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def words(n: int) -> str:
    if 0 <= n < 20:
        return _ONES[n]
    if 20 <= n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    return str(n)


def protection_phrase(seconds: int) -> str:
    if seconds <= 0:
        return "immediately"
    if seconds == 90:
        return "a minute and a half"
    if seconds == 60:
        return "one minute"
    if seconds % 60 == 0:
        return f"{words(seconds // 60)} minutes"
    return f"{words(seconds)} seconds"


def spoken_time(dt: datetime, now: datetime) -> str:
    hm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"today at {hm}"
    if dt.date() == now.date() - timedelta(days=1):
        return f"yesterday at {hm}"
    if (now.date() - dt.date()).days < 7:
        return f"on {dt.strftime('%A')} at {hm}"
    return f"on {dt.strftime('%A %-d %B')} at {hm}"


def area_list(names: list[str], n: int = 3) -> str:
    if len(names) <= n:
        return " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]
    return ", ".join(names[:n]) + f" and {len(names) - n} other areas"


def _json(obj: dict) -> str:
    # Compact: FastMCP would pretty-print a dict, more bytes per voice turn.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- HTTP

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    return _client


async def get_text(path: str, params: dict | None = None) -> str:
    r = await client().get(path, params=params)
    r.raise_for_status()
    if "html" in r.headers.get("content-type", ""):
        raise RuntimeError(f"HTTP {r.status_code} returned HTML (blocked or maintenance page)")
    # Empty feeds are a UTF-8 BOM plus CRLF; utf-8-sig drops the BOM.
    return r.content.decode("utf-8-sig").strip()


# ---------------------------------------------------------------- live alerts

def _match_instruction(title_he: str) -> dict | None:
    t = title_he.strip()
    if not t:
        return None
    for ins in INSTRUCTIONS:
        if ins["title_he"] == t:
            return ins
    for ins in INSTRUCTIONS:  # live titles are often the short form ("ירי רקטות וטילים")
        if ins["title_he"].startswith(t) or t.startswith(ins["title_he"]):
            return ins
    return None


_STAY = re.compile(r"(\d+)\s*דקות")


def parse_live(payload) -> list[dict]:
    items = payload if isinstance(payload, list) else [payload]
    out = []
    for a in items:
        if not isinstance(a, dict) or not a.get("data"):
            continue
        try:
            matrix = int(a.get("cat"))
        except (TypeError, ValueError):
            matrix = -1
        title_he = (a.get("title") or "").strip()
        desc_he = (a.get("desc") or "").strip()
        ins = _match_instruction(title_he)
        if ins is None and "בדקות הקרובות" in title_he:  # "alerts expected in your area in the next minutes"
            ins = _match_instruction("התקרבו למרחב מוגן")
        cat = _cat_by_matrix(matrix)
        key = cat["category"] if cat else "unknown"
        if matrix == 10:  # 10 carries both flashes (early warnings) and updates; the title tells which
            key = ins["cat"] if ins else ("update" if "הסתיים" in title_he else "flash")
        elif ins is None:  # short live title with no exact match: use the category's standard instruction
            ins = next((i for i in INSTRUCTIONS if i["cat"] == key), None)
        base, drill = _threat_key(key)
        m = _STAY.search(desc_he)
        names_he = a["data"] if isinstance(a["data"], list) else [a["data"]]
        areas = [BY_HE.get(n) for n in names_he]
        out.append({
            "id": a.get("id"),
            "category": base,
            "threat": THREAT.get(base, cat["eng"] if cat else "unknown"),
            "drill": drill,
            "title": ins["title"] if ins else (title_he if matrix == 10 or not cat else cat["eng"]),
            "title_he": title_he,
            "instruction": ins["instructions"] if ins else None,
            "instruction_he": desc_he,
            "stay_minutes": int(m.group(1)) if m else None,
            "guideline_url": ins["url"] if ins else None,
            "areas_he": names_he,
            "areas": [x.en if x else n for x, n in zip(areas, names_he)],
            "protection_seconds": min((x.protection_seconds for x in areas if x), default=None),
        })
    return out


def say_live(alert: dict, where: str) -> str:
    places = area_list(alert["areas"]) if where == "Israel" else where
    c = alert["category"]
    drill = " drill" if alert["drill"] else ""
    if c in SHORT:
        s = f"{SHORT[c]} alert{drill} now in {places}"
        if c in ("earthquakealert1", "earthquakealert2"):
            s += ": go out to an open area"
        elif c == "tsunami":
            s += ": move away from the beach"
        elif c == "hazmat":
            s += ": go indoors and close the windows"
        else:
            s += ": go to the protected space"
            if alert["stay_minutes"]:
                s += f" and stay {words(alert['stay_minutes'])} minutes"
        s += "."
        if alert["protection_seconds"] is not None and c in ("missilealert", "nonconventional"):
            s += f" You have {protection_phrase(alert['protection_seconds'])} to get there." \
                if alert["protection_seconds"] > 0 else " Go immediately."
        return s
    # flash / update: speak the official English title and the first sentence of its instruction
    kind = "update" if c == "update" else "notice"
    if alert["instruction"]:
        first = re.split(r"(?<=[.!])\s", alert["instruction"], maxsplit=1)[0].rstrip(".!")
        return f"Home Front Command {kind} for {places}: {alert['title'].rstrip('.!')}. {first}."
    return f"Home Front Command {kind} for {places}; the message is in Hebrew only: {alert['title_he']}."


# ---------------------------------------------------------------- history

def _ts(row: dict) -> datetime | None:
    try:
        return datetime.strptime(f"{row['date']} {row['time']}", "%d.%m.%Y %H:%M:%S").replace(tzinfo=TZ)
    except (KeyError, ValueError):
        return None


def _history_key(cat_id: int) -> str:
    c = CAT_BY_ID.get(cat_id)
    return c["category"] if c else "unknown"


ALERT_CATEGORY_IDS = "|".join(str(c["id"]) for c in CATEGORIES
                              if c["id"] < 15 and c["category"] not in ("update", "flash"))


def group_events(rows: list[dict]) -> list[dict]:
    """One row per area per alert -> one event per (minute, category, description)."""
    events: dict[tuple, dict] = {}
    for r in rows:
        dt = _ts(r)
        if dt is None:
            continue
        k = (dt.strftime("%Y-%m-%d %H:%M"), r.get("category"), r.get("category_Desc"))
        ev = events.get(k)
        if ev is None:
            base, drill = _threat_key(_history_key(r.get("category")))
            ev = events[k] = {"time": dt, "category": base, "threat": THREAT.get(base, base), "drill": drill,
                              "title": r.get("category_Desc"), "areas": []}
        if r.get("data") and r["data"] not in ev["areas"]:
            ev["areas"].append(r["data"])
    return sorted(events.values(), key=lambda e: e["time"], reverse=True)


# ---------------------------------------------------------------- guidelines (bundled snapshot)

_GDIR = resources.files("home_front_command_mcp").joinpath("data", "guidelines")
MANIFEST = json.loads(_GDIR.joinpath("manifest.json").read_text(encoding="utf-8"))
SNAPSHOT_DATE = MANIFEST.get("extraction_date")

SYNONYMS = {
    "car": ["vehicle", "driving"], "driving": ["vehicle"], "drive": ["vehicle", "driving"],
    "siren": ["alert", "rocket", "missile"], "sirens": ["alert", "rocket", "missile"], "alarm": ["alert"], "warning": ["alert", "preliminary"],
    "shelter": ["protected", "space", "shelter"], "mamad": ["protected", "mamad", "room"],
    "miklat": ["shelter", "public"], "safe": ["protected"], "room": ["protected", "space"],
    "long": ["time", "available", "know"], "time": ["available", "defense"], "reach": ["available", "time"],
    "seconds": ["time", "available"], "kit": ["equipment"], "supplies": ["equipment"], "bag": ["equipment"],
    "drone": ["aerial", "hostile"], "uav": ["aerial", "hostile"], "rocket": ["missile"], "rockets": ["missile"],
    "missiles": ["missile"], "terrorist": ["infiltration"], "terror": ["infiltration", "terrorist"],
    "chemical": ["hazardous", "materials"], "hazmat": ["hazardous", "materials"], "outside": ["open", "outdoors"],
    "outdoors": ["open"], "bus": ["public", "transportation"], "train": ["trains", "railway"],
    "kids": ["children"], "baby": ["babies", "infants", "children"], "pets": ["pet", "animals"],
    "leave": ["exit", "stay"], "stay": ["minutes", "remain"], "quake": ["earthquake"], "call": ["contact"], "phone": ["contact", "numbers"],
    "number": ["contact"], "numbers": ["contact"],
}
STOP = set("a an the to of in on at for and or is are be do does what how i we my our you your when if "
           "should can there it this that with during while get from about goes off go".split())


NEG = re.compile(r"\b(cannot|can't|can’t|unable|not|no|unknown|don’t|don't)\b")
SPECIFIC = set("tunnel bridge hazardous materials student students transportation school schools train trains "
               "railway beach sea preliminary pregnant disabilities elderly bus buses hospital work workplace "
               "kindergarten abroad tourists".split())
ACT = re.compile(r"\b(do|act|should|what to)\b")


def _tokens(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP]


@dataclass
class Chunk:
    title: str
    url: str
    question: str
    text: str


def _load_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for f in MANIFEST["files"]:
        rel = f["filename"].removeprefix("guidelines/")
        body = _GDIR.joinpath(*rel.split("/")).read_text(encoding="utf-8")
        body = body.split("\n---\n", 1)[-1]  # drop the source header block
        body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)  # links -> text
        q, buf = "", []

        def flush():
            text = " ".join(x.strip() for x in buf if x.strip())
            if text or q:
                chunks.append(Chunk(f["title"], f["source_url"], q, text))

        for line in body.splitlines():
            s = line.strip().strip("*").strip()
            if line.startswith("#") or (s.endswith("?") and len(s) < 220):
                flush()
                q, buf = s.lstrip("#").strip(), []
            else:
                buf.append(line.replace("**", ""))
        flush()
    return [c for c in chunks if len(c.text) > 40]


CHUNKS = _load_chunks()


def search_guidelines(topic: str, limit: int) -> list[tuple[float, Chunk]]:
    words_ = _tokens(topic)
    how_to = bool(ACT.search(topic.lower())) or not words_ or "siren" in topic.lower() or "alert" in topic.lower()
    expanded = set(words_)
    for w in words_:
        expanded.update(SYNONYMS.get(w, []))
        if w.endswith("s"):
            expanded.add(w[:-1])
        if w.endswith("ing") and len(w) > 5:
            expanded.update({w[:-3], w[:-3] + "e"})
    scored = []
    for c in CHUNKS:
        qt, tt, ht = set(_tokens(c.question)), _tokens(c.text), set(_tokens(c.title))
        tset = set(tt)
        score = 0.0
        for w in expanded:
            weight = 1.0 if w in words_ else 0.6
            if w in qt:
                score += 3 * weight
            if w in tset:
                score += 1 * weight
            if w in ht:
                score += 1.5 * weight
        if score:
            ql = c.question.lower()
            if NEG.search(ql) and not NEG.search(topic.lower()):
                score *= 0.7  # "what if you cannot reach..." is not the answer to "how long do I have"
            extra = (qt & SPECIFIC) - expanded
            if extra:
                score *= 0.6 ** len(extra)  # a tunnel/bus/hazmat-specific answer when none was asked
            if how_to and ACT.search(ql):
                score *= 1.3  # "what to do" prefers "how should I act / what should I do" questions
            scored.append((score / (1 + len(tt) / 400), c))
    scored.sort(key=lambda x: x[0], reverse=True)
    out, seen = [], set()
    for s, c in scored:
        k = (c.question, c.text[:80])
        if k in seen:
            continue
        seen.add(k)
        out.append((s, c))
        if len(out) >= limit:
            break
    return out


def first_sentences(s: str, n_chars: int) -> str:
    if len(s) <= n_chars:
        return s
    parts = re.split(r"(?<=[.!])\s+", s)
    out = ""
    for p in parts:
        if out and len(out) + len(p) > n_chars:
            break
        out = (out + " " + p).strip()
    return out if out else s[:n_chars].rsplit(" ", 1)[0] + "..."


# ---------------------------------------------------------------- MCP

mcp = FastMCP(
    "home-front-command",
    instructions="Israel Home Front Command (Pikud HaOref): active_alerts() for alerts right now, "
                 "recent_alerts() for history, guidelines(topic) for official what-to-do guidance, "
                 "protection_time(area) for an area's time to reach shelter. Speak the `say` field; "
                 "never invent protection times or instructions.",
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
    json_response=True,
)


@mcp.tool(annotations=READ_ONLY)
async def active_alerts(area: str | None = None) -> str:
    """Home Front Command alerts active right now (rockets/missiles, hostile aircraft, infiltration,
    earthquake, hazmat, early warnings, all-clear updates).

    area: Hebrew or English area name(s), comma-separated ("Jerusalem", "Tel Aviv - City Center",
    "ירושלים - מערב"); default = HFC_AREAS; "all" = whole country.
    Returns {say, active, alerts:[{threat, title, instruction, stay_minutes, protection_seconds, areas}],
    elsewhere} where `elsewhere` counts alerts active in other areas."""
    wanted, where, unknown = wanted_areas(area)
    now = datetime.now(TZ)
    try:
        text = await get_text(LIVE_PATH)
    except Exception as e:  # noqa: BLE001
        return _json({"say": "I couldn't reach the Home Front Command alert feed right now.",
                      "error": f"{type(e).__name__}: {e}", "checked": now.strftime("%H:%M:%S")})
    alerts = parse_live(json.loads(text)) if text else []
    mine, elsewhere = [], 0
    names = {a.he for a in wanted} if wanted is not None else None
    for al in alerts:
        if names is None:
            mine.append(al)
            continue
        hit = [i for i, n in enumerate(al["areas_he"]) if n in names]
        if hit:
            al = dict(al, areas_he=[al["areas_he"][i] for i in hit], areas=[al["areas"][i] for i in hit],
                      other_areas=len(al["areas_he"]) - len(hit))
            mine.append(al)
        else:
            elsewhere += 1
    if mine:
        say = " ".join(say_live(a, where) for a in mine[:2])
    else:
        say = f"No active alerts for {where}."
        if elsewhere:
            say += f" There {'is' if elsewhere == 1 else 'are'} {words(elsewhere)} active elsewhere in Israel."
    out = {"say": say, "active": bool(mine), "checked": now.strftime("%Y-%m-%d %H:%M:%S"),
           "areas": where, "alerts": mine}
    if elsewhere:
        out["elsewhere"] = elsewhere
    if unknown:
        out["unknown_areas"] = unknown
    return _json(out)


@mcp.tool(annotations=READ_ONLY)
async def recent_alerts(area: str | None = None, hours: int = 24, limit: int = 10,
                        include_updates: bool = False) -> str:
    """Home Front Command alert history for the last N hours (1-720), newest first, from the official
    alert-history API. Alerts are grouped into events (one per minute and type, with the areas hit).

    area: as in active_alerts (default HFC_AREAS; "all" = whole country).
    include_updates: also list "event ended"/early-warning updates as events (they are always counted).
    Returns {say, total, counts:{type: n}, events:[{time, threat, title, areas}], truncated}."""
    hours = max(1, min(int(hours), 720))
    limit = max(1, min(int(limit), 40))
    wanted, where, unknown = wanted_areas(area)
    if wanted is not None and not wanted:
        return _json({"say": f"I don't know the area {where}.", "unknown_areas": unknown})
    now = datetime.now(TZ)
    start = now - timedelta(hours=hours)
    cities = "|".join(a.en for a in wanted) if wanted is not None else ""
    base = {"from": start.strftime("%Y-%m-%dT%H:%M:%S"), "to": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "cities": cities}
    list_params = dict(base) if include_updates else dict(base, categoryIds=ALERT_CATEGORY_IDS)
    try:
        counts_txt, list_txt = await asyncio.gather(get_text(f"{HISTORY_API}/count/eng", base),
                                                    get_text(f"{HISTORY_API}/eng", list_params))
        counts_doc, list_doc = json.loads(counts_txt), json.loads(list_txt)
        rows = [r for g in list_doc.get("alarmsGroupedByDate", []) for r in g.get("value", [])]
        counts = {c["categoryDesc"]: c["alarmsCount"] for c in counts_doc.get("alarmsByCategory", [])}
        total = counts_doc.get("totalAlarms", sum(counts.values()))
        source = f"{BASE}{HISTORY_API}"
    except Exception as e:  # noqa: BLE001
        if hours > 24:
            return _json({"say": "I couldn't reach the Home Front Command alert history right now.",
                          "error": f"{type(e).__name__}: {e}"})
        try:  # fallback: the site's rolling last-day file (Hebrew, one row per area)
            text = await get_text(LEGACY_HISTORY_PATH)
        except Exception as e2:  # noqa: BLE001
            return _json({"say": "I couldn't reach the Home Front Command alert history right now.",
                          "error": f"{type(e).__name__}: {e}; fallback {type(e2).__name__}: {e2}"})
        legacy = json.loads(text) if text else []
        names = {a.he for a in wanted} if wanted is not None else None
        rows = []
        for r in legacy:
            dt = datetime.strptime(r["alertDate"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            if dt < start or (names is not None and r.get("data") not in names):
                continue
            a = BY_HE.get(r.get("data"))
            rows.append({"date": dt.strftime("%d.%m.%Y"), "time": dt.strftime("%H:%M:%S"),
                         "data": a.en if a else r.get("data"), "category": r.get("category"),
                         "category_Desc": r.get("title")})
        if not include_updates:
            rows = [r for r in rows if r.get("category") not in (13, 14)]
        counts, total = {}, len(rows)
        source = f"{BASE}{LEGACY_HISTORY_PATH}"
    events = group_events(rows)
    alert_total = sum(n for k, n in counts.items() if k not in ("Update", "flash")) if counts else len(rows)
    span = f"the last {hours} hours" if hours < 48 else f"the last {words(round(hours / 24))} days"
    real = [e for e in events if e["category"] not in ("update", "flash")]
    if not real and not alert_total:
        say = f"No alerts in {where} in {span}."
    else:
        ev = real[0] if real else events[0]
        kind = SHORT.get(ev["category"], ev["title"] or ev["threat"]).lower()
        latest = f"{kind} alert {spoken_time(ev['time'], now)}"
        if where == "Israel":
            latest += f" in {area_list(ev['areas'], 2)}"
        n = alert_total
        say = (f"In {span} there {'was' if n == 1 else 'were'} {words(n) if n < 100 else n} "
               f"alert{'s' if n != 1 else ''} in {where}; the latest was a {latest}.")
    out = {
        "say": say, "areas": where, "hours": hours, "total": total, "counts": counts,
        "events": [dict(e, time=e["time"].strftime("%a %d %b %H:%M"), areas=e["areas"][:10],
                        area_count=len(e["areas"])) for e in events[:limit]],
        "source": source,
    }
    if len(rows) >= PAGE_ROWS:
        out["truncated"] = True  # first page only (200 rows); counts are complete
    if unknown:
        out["unknown_areas"] = unknown
    return _json(out)


@mcp.tool(annotations=LOCAL)
def guidelines(topic: str, limit: int = 2) -> str:
    """Official Home Front Command guidance on a topic, from a bundled English snapshot of oref.org.il/eng
    (what to do in a siren, in a car, outdoors, on a bus; time to reach shelter; mamad / protected space;
    emergency kit; drones; infiltration; hazmat; earthquake; family plan; who to call).

    Returns {say, results:[{title, question, excerpt, source_url}], snapshot_date, note}. Quote the excerpt;
    don't add numbers it doesn't contain."""
    limit = max(1, min(int(limit), 5))
    hits = search_guidelines(topic, limit)
    note = (f"From a snapshot of oref.org.il/eng taken {SNAPSHOT_DATE}; for current guidance see "
            "https://www.oref.org.il/eng. In an emergency call 100 police, 101 ambulance, 102 fire.")
    if not hits:
        return _json({"say": "I don't have Home Front Command guidance on that; check oref.org.il.",
                      "results": [], "snapshot_date": SNAPSHOT_DATE, "note": note})
    results = [{"title": c.title, "question": c.question, "excerpt": first_sentences(c.text, 700),
                "source_url": c.url} for _, c in hits]
    return _json({"say": first_sentences(hits[0][1].text, 260), "results": results,
                  "snapshot_date": SNAPSHOT_DATE, "note": note})


@mcp.tool(annotations=LOCAL)
def protection_time(area: str | None = None) -> str:
    """Time available to reach a protected space (defense time) for an alert area, from oref's official
    area list (bundled snapshot). Also resolves area names: use it to find the exact Hebrew/English names.

    area: Hebrew or English name (default HFC_AREAS). Returns {say, areas:[{en, he, district,
    protection_seconds}]}. The current defensive policy level is not included: oref's policy endpoint
    returned placeholder data when checked (2026-11-22)."""
    wanted, where, unknown = wanted_areas(area)
    if wanted is None:
        return _json({"say": "Name an area and I'll give its time to reach shelter. Most of Israel has a "
                             "minute and a half.", "areas": []})
    if not wanted:
        return _json({"say": f"I don't know the area {where}.", "unknown_areas": unknown, "areas": []})
    times = sorted({a.protection_seconds for a in wanted})
    if len(times) == 1:
        say = f"In {where} you have {protection_phrase(times[0])} to reach a protected space."
        if times[0] == 0:
            say = f"In {where} you must enter a protected space immediately."
    else:
        say = (f"In {where} the time to reach a protected space ranges from {protection_phrase(times[0])} "
               f"to {protection_phrase(times[-1])}, depending on the exact area.")
    out = {"say": say, "areas": [{"en": a.en, "he": a.he, "district": a.district, "council": a.council,
                                  "protection_seconds": a.protection_seconds} for a in wanted[:30]],
           "matched": len(wanted), "data_date": _AREAS_DOC.get("fetched"), "source": _AREAS_DOC.get("source")}
    if unknown:
        out["unknown_areas"] = unknown
    return _json(out)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
