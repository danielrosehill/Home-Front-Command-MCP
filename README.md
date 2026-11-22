# Home-Front-Command-MCP

An MCP server for Israel's Home Front Command (Pikud HaOref, פיקוד העורף): alerts active right now, alert
history, each area's time to reach shelter, and the official English civilian guidelines. Built for voice
assistants: every result has a `say` field, one calm plain-English sentence, and compact JSON behind it.

Unofficial. Not affiliated with the Home Front Command or the IDF. Not a replacement for the official app,
sirens or emergency services (100 police, 101 ambulance, 102 fire).

**Nearest shelter** is out of scope: use the public Miklat MCP at `https://mcp.jlmshelters.com/mcp`.

## Tools

All tools are read-only (`readOnlyHint: true`). `area` everywhere accepts Hebrew or English oref area names,
comma-separated: an exact area (`Jerusalem - West`, `ירושלים - מערב`), a split city (`Jerusalem` / `ירושלים`
= its 7 `Jerusalem - *` areas), a district (`Dan`, `Gaza Envelope`) or a substring. Omitted = `HFC_AREAS`;
`all` = the whole country.

### `active_alerts(area?)`

Alerts active now, from the live feed. Returns (illustrative active alert; built from the site's own alert shape):

```json
{"say": "Rocket alert now in Jerusalem: go to the protected space and stay ten minutes. You have a minute and a half to get there.",
 "active": true, "checked": "2026-11-22 23:08:31", "areas": "Jerusalem",
 "alerts": [{"id": "134087", "category": "missilealert", "threat": "rockets or missiles", "drill": false,
   "title": "Missile and rocket attack - Enter protected space",
   "instruction": "Enter a protected space within the time you have available to reach shelter.",
   "instruction_he": "היכנסו למרחב המוגן ושהו בו 10 דקות", "stay_minutes": 10,
   "guideline_url": "https://www.oref.org.il/eng/articles/security-threats/rockets-missles/1106/",
   "areas": ["Jerusalem - West"], "areas_he": ["ירושלים - מערב"], "protection_seconds": 90, "other_areas": 1}],
 "elsewhere": 0}
```

Idle: `{"say": "No active alerts for Jerusalem.", "active": false, ...}`; if other areas have alerts,
`elsewhere` counts them and the sentence says so. `threat` is plain English: rockets or missiles, hostile
aircraft or drone, terrorist infiltration, earthquake, tsunami, hazardous materials, radiological event,
non-conventional missile, early warning or instruction (`flash`), update (`update`, e.g. "The event has ended").
`stay_minutes` comes only from the minutes stated in the Hebrew `desc`; `protection_seconds` only from oref's
area list. Nothing is invented: when neither is present, the sentence leaves it out.

### `recent_alerts(area?, hours=24, limit=10, include_updates=false)`

History for the last 1-720 hours, newest first. Rows (one per area per alert) are grouped into events (one per
minute and type). Returns `say`, `total` and `counts` (per category, from the count endpoint, complete),
`events` (`time`, `threat`, `title`, first 10 `areas`, `area_count`), `source`, and `truncated: true` when the
event list hit the API's 200-row page (counts stay complete). Updates and early warnings are counted but only
listed as events with `include_updates=true`. Counts are per area: one salvo on 30 areas counts 30.

### `guidelines(topic, limit=2)`

Keyword search over a bundled English snapshot of oref.org.il/eng (22 pages, extracted 2026-03-20). Returns
`say` (the top answer's first sentences), `results` (`title`, `question`, `excerpt`, `source_url`),
`snapshot_date` and a `note` pointing to the live site. Covers: what to do in a siren, in a car, tunnel, bus,
train, outdoors; time to reach shelter; leaving the protected space; mamad / mamak / shelter; emergency kit;
drones; terrorist infiltration; hazardous materials; family plan; who to contact; receiving alerts. Not
covered by the snapshot: earthquake and tsunami behaviour.

### `protection_time(area?)`

Time available to reach a protected space for an area, from oref's official area list (bundled, fetched
2026-11-22: 1,492 areas; 899 have 90 s, 173 60 s, 142 45 s, 130 30 s, 144 15 s, 4 immediate). Also the way
to look up exact Hebrew/English area names. The current *defensive policy level* is not exposed: oref's
`restrictionsPolicy` endpoint returned placeholder text on 2026-11-22 (see Endpoints).

## Configuration

| Env | Default | |
| --- | --- | --- |
| `HFC_AREAS` | empty (whole country) | Default areas, comma-separated, Hebrew or English |
| `HFC_BASE_URL` | `https://www.oref.org.il` | Point at a relay if the host is outside Israel |
| `HFC_USER_AGENT` | a desktop Chrome UA | Must look like a browser (see below) |
| `HFC_TIMEOUT` | `10` | HTTP timeout, seconds |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Streamable HTTP, path `/mcp`, stateless, JSON responses |
| `MCP_TRANSPORT` | `streamable-http` | or `stdio` |

For Jerusalem set `HFC_AREAS=Jerusalem` (or the Hebrew `ירושלים`), which resolves to these 7 oref areas, all
90 s: `ירושלים - מרכז`, `ירושלים - צפון`, `ירושלים - דרום`, `ירושלים - מזרח`, `ירושלים - מערב`,
`ירושלים - אזור תעשייה עטרות`, `ירושלים - כפר עקב`. Narrow to one (`ירושלים - מערב`) for a single neighbourhood.
The surrounding Jerusalem district (Givat Ze'ev, Mevaseret Zion...) is not included unless named.

## Run

```bash
uv sync
uv run home-front-command-mcp                         # http://0.0.0.0:8000/mcp
MCP_TRANSPORT=stdio uv run home-front-command-mcp     # stdio
docker compose up -d                                  # builds the v1.0 tag, 127.0.0.1:8000/mcp
```

Claude Code: `claude mcp add hfc -e MCP_TRANSPORT=stdio -e HFC_AREAS=Jerusalem -- uvx --from git+https://github.com/danielrosehill/Home-Front-Command-MCP home-front-command-mcp`

The host needs an Israeli IP: oref.org.il is geofenced (CloudFront, PoP `TLV55`).

## Endpoints (verified 2026-11-22 from an Israeli residential IP)

There is no documented public API. Everything below was found by probing and by reading the site's Angular
bundle (`chunk-PKCZWSFH.js` holds the environment config, `chunk-6BXFF77I.js` the API route table).

**Headers.** oref's CloudFront WAF returns `403` (a 919-byte HTML "request could not be satisfied") to
non-browser User-Agents (`curl/*`, `python-httpx/*`, and even a bare `Mozilla/5.0`) on the API, districts and
config paths; a full desktop Chrome UA passes. The live `Alerts.json` was served even to a python UA.
`Referer: https://www.oref.org.il/` and `X-Requested-With: XMLHttpRequest` were not required on 2026-11-22
but are sent anyway (older clients needed them). The server sends all three.

| Purpose | URL | Notes |
| --- | --- | --- |
| Live alerts | `GET /warningMessages/alert/Alerts.json` | `/WarningMessages/alert/alerts.json` 302s here. Idle body is a UTF-8 BOM + CRLF (5 bytes, `EF BB BF 0D 0A`), so decode `utf-8-sig` and treat empty as none. Active: `{"id","cat","title","data":[Hebrew area names],"desc"}`; `cat` is the matrix id (1 missiles, 6 hostile aircraft, 10 flash/update, 3 earthquake, 13 infiltration, 101+ drills). Shape taken from the site's mock feed; **no live alert was observed** while building this. |
| History (primary) | `GET /api/v1/AlertHistory/{eng\|heb}?from=YYYY-MM-DDTHH:mm:ss&to=...&cities=A\|B&categoryIds=1\|2` | Times are Israel local (confirmed: a 02:56 alert matched a 02:50-02:58 window, not the UTC one). `cities` = area labels in the request language joined by `\|`, empty = all. Also `?period=0\|1\|2` (last day / week / month). Response `{"lastRetrievedAlarm", "alarmsGroupedByDate":[{"key":"13.11.2026","value":[{"data","date","time","dateTime","category","category_Desc","rid"}]}]}`, newest first, max 200 rows. The paging cursor (`lastCreatedOn`, `lastTitle`, `lastCityName`) returned overlapping rows when tried, so it is not used. A 365-day count worked (450,916 rows). |
| History counts | `GET /api/v1/AlertHistory/count/{lang}?from=..&to=..&cities=..` | `{"alarmsByCategory":[{"categoryId","categoryDesc","alarmsCount"}],"totalAlarms"}` |
| History (legacy, last day) | `GET /warningMessages/alert/History/AlertsHistory.json` | Hebrew rows `{alertDate,title,data,category}`; `\r\n` when empty. Used only as a fallback for `hours<=24`. |
| Areas + protection time | `GET /districts/districts_{eng,heb}.json` (also `/api/v1/AlertHistory/districts/{lang}`) | `label`, `label_he`, `areaname` (district), `rashut` (council), `migun_time` seconds. Bundled. |
| Categories | `GET /alerts/alertCategories.json` | 28 entries, `id` (history category) ↔ `matrix_id` (live `cat`) ↔ `eng`/`heb`. Bundled. |
| Instructions | `GET /alerts/RemainderConfig_{eng,heb}.json` | 46 entries: Hebrew title → English title, instructions, guideline link. Bundled; used to translate live titles. |
| Defensive policy | `GET /api/v1/restrictionsPolicy/eng` | Returned "Bla Bla Bla" placeholder guidelines; `restrictionsPolicy/data/{lang}/{city}` returned a "Site Maintenance" HTML page. Not used. |
| Dead | `alerts-history.oref.org.il/Shared/Ajax/GetAlarmsHistory.aspx` (DNS fails), `www.oref.org.il/Shared/Ajax/GetAlarmsHistory.aspx` (404) | The old history API. |

Refresh the bundled area/category/instruction data (from an Israeli IP): `uv run python scripts/refresh_data.py`.

## Verified vs assumed (2026-11-22)

- Verified live: idle live feed parses to "No active alerts"; history for 24 h and 7 days was empty (the most
  recent alert was 13 Nov 2026 02:56, Nachal Oz, rockets); 30 days returned 14 rows / 5 alerts across all areas
  and none in Jerusalem; a 2-day window in March 2026 returned a full 200-row page grouped into 37 events;
  `protection_time("Jerusalem")` = 90 s for all 7 areas; guidelines for "car during siren" and "how long to
  reach shelter" return the vehicle FAQ and the defense-time FAQ.
- Assumed: the active alert JSON shape and that live `data` names equal the `label_he` names in the districts
  file (both from the site's own code and long-standing community use, not from a live alert seen here);
  English translation of live titles relies on `RemainderConfig_eng.json` matching the Hebrew title.

## Data and attribution

- `src/home_front_command_mcp/data/guidelines/`: English guideline snapshot extracted 2026-03-20 from
  https://www.oref.org.il/eng, copied from [danielrosehill/Pikud-Haoref-Guidelines-0326](https://github.com/danielrosehill/Pikud-Haoref-Guidelines-0326)
  (as bundled in the `home-front-command-guidelines` skill of
  [Claude-Israel-Agent-Skills-Plugin](https://github.com/danielrosehill/Claude-Israel-Agent-Skills-Plugin)).
  Content © Home Front Command; included for reference, always check the live site.
- `areas.json`, `categories.json`, `instructions.json`: oref's public JSON files, fetched 2026-11-22.
- Feed syntax notes: [Israel-Red-Alert-Syntax-Notes](https://github.com/danielrosehill/Israel-Red-Alert-Syntax-Notes),
  [Israel-Home-Front-Ideas](https://github.com/danielrosehill/Israel-Home-Front-Ideas) (observed payload schema).

## License

MIT (code). Bundled oref content remains the Home Front Command's.
