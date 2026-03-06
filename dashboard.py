#!/usr/bin/env python3
"""
Camera Monitor v2 - Web Dashboard
Run: python dashboard.py
Opens at: http://localhost:8150
"""

import json
import base64
import sys
import os
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent
EVENTS_DIR = BASE / "events"
BASELINES_DIR = BASE / "baselines"
LOG_DIR = BASE / "logs"
DATA_DIR = BASE / "data"
PROFILES_FILE = BASE / "camera_profiles.json"
SESSION_FILE = DATA_DIR / "dashboard_session.json"

# Load .env file if present
_env_file = BASE / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

HA_URL = os.environ.get("HA_URL", "http://192.168.86.102:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

PORT = 8150
START_TIME = datetime.now()

DATA_DIR.mkdir(exist_ok=True)

# ── HA State Cache ──
_ha_states_cache = {"data": None, "time": 0}
_ha_cache_lock = threading.Lock()
HA_CACHE_TTL = 30  # seconds


def load_profiles():
    with open(PROFILES_FILE) as f:
        return json.load(f)


def load_session():
    if SESSION_FILE.exists():
        with open(SESSION_FILE) as f:
            return json.load(f)
    return {"last_visit": None}


def save_session(data):
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f)


def ha_get(path, timeout=10):
    try:
        req = urllib.request.Request(
            f"{HA_URL}{path}",
            headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        )
        return urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        return None


def get_ha_states():
    """Get all HA states with caching."""
    with _ha_cache_lock:
        now = time.time()
        if _ha_states_cache["data"] and (now - _ha_states_cache["time"]) < HA_CACHE_TTL:
            return _ha_states_cache["data"]
    raw = ha_get("/api/states")
    if not raw:
        return _ha_states_cache.get("data") or []
    states = json.loads(raw)
    with _ha_cache_lock:
        _ha_states_cache["data"] = states
        _ha_states_cache["time"] = time.time()
    return states


def get_camera_snapshot_b64(entity_id):
    try:
        data = ha_get(f"/api/camera_proxy/{entity_id}", timeout=10)
        if data:
            return base64.b64encode(data).decode("ascii")
    except Exception:
        pass
    return None


def get_all_camera_status():
    states = get_ha_states()
    if not states:
        return []
    profiles = load_profiles()
    cameras = []
    for eid, cfg in profiles.get("cameras", {}).items():
        ha_state = next((s for s in states if s["entity_id"] == eid), None)
        cameras.append({
            "entity_id": eid,
            "name": cfg["name"],
            "group": cfg["group"],
            "location": cfg["location"],
            "enabled": cfg["enabled"],
            "state": ha_state["state"] if ha_state else "unknown",
            "notes": cfg.get("notes", ""),
        })
    return cameras


def get_live_cameras_parallel():
    """Fetch all camera snapshots in parallel using ThreadPoolExecutor."""
    cameras = get_all_camera_status()
    online = [c for c in cameras if c["state"] == "recording"]
    offline = [c for c in cameras if c["state"] != "recording"]

    # Fetch snapshots in parallel
    results = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(get_camera_snapshot_b64, c["entity_id"]): c["entity_id"] for c in online}
        for future in as_completed(futures, timeout=15):
            eid = futures[future]
            try:
                results[eid] = future.result()
            except Exception:
                results[eid] = None

    out = []
    for cam in cameras:
        snap = results.get(cam["entity_id"]) if cam["state"] == "recording" else None
        out.append({**cam, "snapshot": snap})
    return out


def load_events(date_filter=None, camera_filter=None, search=None, limit=50, offset=0):
    events_log = LOG_DIR / "events.jsonl"
    if not events_log.exists():
        return [], 0
    events = []
    with open(events_log, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if date_filter:
        events = [e for e in events if e.get("start", "").startswith(date_filter)]
    if camera_filter and camera_filter != "all":
        events = [e for e in events if e.get("camera") == camera_filter or e.get("camera_name") == camera_filter]
    if search:
        q = search.lower()
        events = [e for e in events if q in json.dumps(e).lower()]

    total = len(events)
    events = list(reversed(events))
    events = events[offset:offset + limit]
    return events, total


def get_event_image_b64(event_id, frame_name=None):
    edir = EVENTS_DIR / event_id
    if not edir.exists():
        return None
    if frame_name:
        fpath = edir / frame_name
    else:
        jpgs = sorted([f for f in edir.glob("frame_*.jpg")])
        if not jpgs:
            jpgs = sorted(edir.glob("*.jpg"))
        if not jpgs:
            return None
        fpath = jpgs[0]
    if fpath.exists():
        return base64.b64encode(fpath.read_bytes()).decode("ascii")
    return None


def get_event_detail(event_id):
    edir = EVENTS_DIR / event_id
    meta_path = edir / "event.json"
    if not meta_path.exists():
        return None
    with open(meta_path) as f:
        meta = json.load(f)
    images = []
    for jpg in sorted(edir.glob("*.jpg")):
        images.append({"name": jpg.name, "size": jpg.stat().st_size})
    meta["images"] = images
    return meta


def get_monitor_status():
    """Check if monitor.py is running by looking at log freshness."""
    log_file = LOG_DIR / "monitor.log"
    if not log_file.exists():
        return {"running": False, "last_log": None}
    mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
    age = (datetime.now() - mtime).total_seconds()
    uptime = (datetime.now() - START_TIME).total_seconds()
    return {
        "running": age < 120,
        "last_log": mtime.isoformat(),
        "age_seconds": age,
        "dashboard_uptime": int(uptime),
    }


def get_calendar_data():
    events_log = LOG_DIR / "events.jsonl"
    if not events_log.exists():
        return {}
    counts = {}
    with open(events_log, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    e = json.loads(line)
                    day = e.get("start", "")[:10]
                    if day:
                        counts[day] = counts.get(day, 0) + 1
                except json.JSONDecodeError:
                    continue
    return counts


def get_tuning_stats():
    """Get self-tuning stats if available."""
    stats_file = DATA_DIR / "tuning_stats.json"
    if stats_file.exists():
        with open(stats_file) as f:
            return json.load(f)
    return None


# ══════════════════════════════════════════════════════
# HTTP SERVER
# ══════════════════════════════════════════════════════
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                self._html(DASHBOARD_HTML)

            elif path == "/api/status":
                cameras = get_all_camera_status()
                monitor = get_monitor_status()
                session = load_session()
                events, total = load_events(limit=10)
                new_count = 0
                lv = session.get("last_visit")
                if lv:
                    all_events, _ = load_events(limit=1000)
                    new_count = sum(1 for e in all_events if e.get("start", "") > lv)
                self._json({
                    "cameras": cameras,
                    "monitor": monitor,
                    "recent_events": events,
                    "total_events": total,
                    "new_since_last_visit": new_count,
                    "last_visit": lv,
                    "now": datetime.now().isoformat(),
                })

            elif path == "/api/cameras/live":
                self._json(get_live_cameras_parallel())

            elif path == "/api/camera/snapshot":
                eid = params.get("id", [None])[0]
                if eid:
                    b64 = get_camera_snapshot_b64(eid)
                    self._json({"entity_id": eid, "snapshot": b64})
                else:
                    self._json({"error": "missing id"}, 400)

            elif path == "/api/events":
                date = params.get("date", [None])[0]
                camera = params.get("camera", [None])[0]
                search = params.get("search", [None])[0]
                limit = int(params.get("limit", [50])[0])
                offset = int(params.get("offset", [0])[0])
                events, total = load_events(date, camera, search, limit, offset)
                for e in events:
                    eid = e.get("event_id")
                    if eid:
                        e["thumbnail"] = get_event_image_b64(eid)
                self._json({"events": events, "total": total})

            elif path == "/api/event":
                eid = params.get("id", [None])[0]
                if eid:
                    detail = get_event_detail(eid)
                    if detail:
                        for img in detail.get("images", []):
                            img["data"] = get_event_image_b64(eid, img["name"])
                        self._json(detail)
                    else:
                        self._json({"error": "not found"}, 404)
                else:
                    self._json({"error": "missing id"}, 400)

            elif path == "/api/calendar":
                self._json(get_calendar_data())

            elif path == "/api/session/mark-visited":
                session = load_session()
                session["last_visit"] = datetime.now().isoformat()
                save_session(session)
                self._json({"ok": True})

            elif path == "/api/stats":
                stats = get_tuning_stats()
                self._json(stats or {})

            elif path == "/api/monitor/log":
                lines = int(params.get("lines", [100])[0])
                log_file = LOG_DIR / "monitor.log"
                if log_file.exists():
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        all_lines = f.readlines()
                    self._json({"lines": all_lines[-lines:]})
                else:
                    self._json({"lines": []})

            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self._json({"error": str(e)}, 500)


# ══════════════════════════════════════════════════════
# DASHBOARD HTML (Single Page App)
# ══════════════════════════════════════════════════════
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera Monitor</title>
<style>
:root{--bg:#08080d;--surface:#0d1117;--surface2:#151520;--border:#1a1a2e;--green:#39ff14;--blue:#4a9eff;--orange:#ff9f43;--red:#ff4444;--yellow:#ffd700;--text:#ddd;--muted:#666;--radius:10px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);overflow-x:hidden}

/* Nav */
.nav{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;display:flex;align-items:center;height:56px;position:sticky;top:0;z-index:100}
.nav .logo{color:var(--green);font-weight:700;font-size:18px;margin-right:32px;cursor:pointer}
.nav .logo span{color:#fff;font-weight:300}
.nav-links{display:flex;gap:4px}
.nav-link{padding:8px 16px;border-radius:8px;color:var(--muted);font-size:13px;cursor:pointer;transition:all .2s;border:none;background:none;font-family:inherit}
.nav-link:hover,.nav-link.active{color:#fff;background:var(--surface2)}
.nav-link.active{color:var(--green)}
.nav-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.status-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
.status-dot.off{background:var(--red)}
.badge{background:var(--green);color:#000;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;cursor:pointer}
.hamburger{display:none;background:none;border:none;color:var(--text);font-size:22px;cursor:pointer;padding:4px 8px}

/* Layout */
.page{display:none;padding:24px;max-width:1400px;margin:0 auto}.page.active{display:block}
.grid{display:grid;gap:16px}
.grid-2{grid-template-columns:1fr 1fr}
.grid-3{grid-template-columns:1fr 1fr 1fr}

/* Cards */
.card{background:var(--surface);border-radius:var(--radius);border:1px solid var(--border);overflow:hidden}
.card-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-head h3{font-size:14px;color:#fff}
.card-body{padding:16px 18px}

/* Stat boxes */
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.stat-box{background:var(--surface);border-radius:var(--radius);padding:20px;text-align:center;border:1px solid var(--border)}
.stat-box .n{font-size:32px;font-weight:700}.stat-box .l{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-top:2px}

/* Camera grid */
.cam-card{position:relative;background:var(--surface);border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);transition:border-color .2s}
.cam-card:hover{border-color:var(--green)}
.cam-card img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;cursor:pointer;transition:opacity .3s}
.cam-card .cam-overlay{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,.85));padding:10px 12px}
.cam-card .cam-name{font-size:13px;font-weight:600;color:#fff}
.cam-card .cam-status{font-size:11px}
.cam-card .cam-group{position:absolute;top:8px;right:8px;padding:2px 8px;border-radius:8px;font-size:9px;font-weight:700;color:#000}
.cam-card .offline{width:100%;aspect-ratio:4/3;background:var(--surface2);display:flex;align-items:center;justify-content:center;color:#333;font-size:13px}
.cam-card .cam-refresh{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.6);border:none;color:#fff;padding:4px 8px;border-radius:6px;font-size:10px;cursor:pointer;opacity:0;transition:opacity .2s}
.cam-card:hover .cam-refresh{opacity:1}

/* Events */
.event-card{background:var(--surface);border-radius:var(--radius);margin-bottom:12px;overflow:hidden;border:1px solid var(--border);cursor:pointer;transition:border-color .2s}
.event-card:hover{border-color:var(--green)}
.event-card.new{border-left:3px solid var(--green)}
.event-row{display:flex;gap:16px;padding:14px 16px;align-items:center}
.event-thumb{width:120px;height:90px;object-fit:cover;border-radius:8px;flex-shrink:0;background:var(--surface2)}
.event-info{flex:1;min-width:0}
.event-info .title{font-size:14px;font-weight:600;color:#fff;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.event-info .meta{font-size:12px;color:var(--muted)}
.event-info .desc{font-size:12px;color:#999;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.event-type{padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;color:#000;margin-left:8px}

/* Detail modal */
.modal-bg{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.85);z-index:200;justify-content:center;align-items:flex-start;padding:40px;overflow-y:auto}
.modal-bg.open{display:flex}
.modal{background:var(--surface);border-radius:12px;width:100%;max-width:900px;border:1px solid var(--border)}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-head h2{font-size:18px;color:#fff}
.modal-close{background:none;border:none;color:var(--muted);font-size:24px;cursor:pointer}
.modal-body{padding:20px}
.modal-img{width:100%;border-radius:8px;margin-bottom:4px;cursor:pointer}
.modal-img.zoomed{position:fixed;top:0;left:0;width:100%;height:100%;object-fit:contain;z-index:300;background:#000;border-radius:0;margin:0}
.frame-nav{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.frame-btn{padding:4px 12px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:11px;cursor:pointer}
.frame-btn.active{border-color:var(--green);color:var(--green)}
.frame-btn.pre{opacity:0.5}

/* Calendar */
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.cal-head{text-align:center;font-size:11px;color:var(--muted);padding:8px 0}
.cal-day{text-align:center;padding:10px 4px;border-radius:8px;font-size:13px;cursor:pointer;transition:all .2s;position:relative}
.cal-day:hover{background:var(--surface2)}
.cal-day.today{color:var(--green);font-weight:700}
.cal-day.has-events::after{content:'';position:absolute;bottom:4px;left:50%;transform:translateX(-50%);width:6px;height:6px;border-radius:50%;background:var(--green)}
.cal-day.selected{background:var(--green);color:#000}
.cal-day.other-month{color:#333}
.cal-nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.cal-nav button{background:var(--surface2);border:1px solid var(--border);color:#fff;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-family:inherit}
.cal-nav .month-label{font-size:15px;font-weight:600}

/* Search */
.search-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.search-bar input,.search-bar select{background:var(--surface2);border:1px solid var(--border);color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;flex:1;font-family:inherit}
.search-bar select{flex:0;min-width:160px}
.search-bar button{background:var(--green);color:#000;border:none;padding:10px 20px;border-radius:8px;font-weight:700;cursor:pointer;font-size:13px;font-family:inherit}

/* Log viewer */
.log-viewer{background:#000;border-radius:8px;padding:12px;font-family:'Consolas','Courier New',monospace;font-size:11px;color:#0f0;max-height:600px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.6}
.log-viewer .warn{color:var(--orange)}.log-viewer .err{color:var(--red)}.log-viewer .info-highlight{color:var(--green)}

/* Toast */
.toast{position:fixed;bottom:24px;right:24px;background:var(--green);color:#000;padding:12px 20px;border-radius:8px;font-weight:600;font-size:13px;z-index:300;display:none;animation:slideIn .3s ease}
@keyframes slideIn{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}

/* Uptime bar */
.uptime-bar{background:var(--surface);border-top:1px solid var(--border);padding:6px 24px;font-size:11px;color:var(--muted);display:flex;gap:20px;position:fixed;bottom:0;left:0;right:0;z-index:50}

@media(max-width:900px){
    .grid-2,.grid-3{grid-template-columns:1fr}
    .stat-row{grid-template-columns:1fr 1fr}
    .nav-links{display:none}
    .hamburger{display:block}
    .nav-links.open{display:flex;flex-direction:column;position:absolute;top:56px;left:0;right:0;background:var(--surface);padding:8px;border-bottom:1px solid var(--border);z-index:101}
    .page{padding:16px;padding-bottom:60px}
}
</style>
</head><body>

<div class="nav">
    <div class="logo" onclick="showPage('dashboard')">CAM<span>MONITOR</span></div>
    <button class="hamburger" onclick="document.querySelector('.nav-links').classList.toggle('open')">&#9776;</button>
    <div class="nav-links">
        <button class="nav-link active" onclick="showPage('dashboard')">Dashboard</button>
        <button class="nav-link" onclick="showPage('cameras')">Cameras</button>
        <button class="nav-link" onclick="showPage('events')">Events</button>
        <button class="nav-link" onclick="showPage('calendar')">Calendar</button>
        <button class="nav-link" onclick="showPage('logs')">Logs</button>
    </div>
    <div class="nav-right">
        <span id="monitorStatus"><span class="status-dot off"></span> <span style="font-size:12px;color:var(--muted)">Checking...</span></span>
        <span id="newBadge" class="badge" style="display:none" onclick="showPage('events')">0 new</span>
        <span style="font-size:12px;color:var(--muted)" id="clock"></span>
    </div>
</div>

<!-- Dashboard Page -->
<div class="page active" id="page-dashboard">
    <div class="stat-row" id="statsRow"></div>
    <div class="grid grid-2">
        <div class="card">
            <div class="card-head"><h3>Recent Events</h3><span style="font-size:11px;color:var(--muted)" id="eventCount"></span></div>
            <div class="card-body" id="recentEvents"><div style="color:var(--muted);font-size:13px">Loading...</div></div>
        </div>
        <div class="card">
            <div class="card-head"><h3>Camera Status</h3></div>
            <div class="card-body" id="cameraStatus"><div style="color:var(--muted);font-size:13px">Loading...</div></div>
        </div>
    </div>
</div>

<!-- Cameras Page -->
<div class="page" id="page-cameras">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="font-size:20px;color:#fff">Live <span style="color:var(--green)">Cameras</span></h2>
        <div style="display:flex;gap:8px;align-items:center">
            <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:4px">
                <input type="checkbox" id="autoRefreshCams" checked> Auto-refresh
            </label>
            <button onclick="loadCameras()" style="background:var(--surface2);border:1px solid var(--border);color:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:12px;font-family:inherit">Refresh All</button>
        </div>
    </div>
    <div class="grid grid-3" id="cameraGrid"><div style="color:var(--muted)">Loading cameras...</div></div>
</div>

<!-- Events Page -->
<div class="page" id="page-events">
    <h2 style="font-size:20px;color:#fff;margin-bottom:16px">Event <span style="color:var(--green)">History</span></h2>
    <div class="search-bar">
        <input type="text" id="searchInput" placeholder="Search events..." onkeyup="if(event.key==='Enter')searchEvents()">
        <select id="cameraFilter"><option value="all">All Cameras</option></select>
        <input type="date" id="dateFilter" style="flex:0;min-width:160px">
        <button onclick="searchEvents()">Search</button>
    </div>
    <div id="eventsList"></div>
    <div id="eventsMore" style="text-align:center;padding:16px"></div>
</div>

<!-- Calendar Page -->
<div class="page" id="page-calendar">
    <h2 style="font-size:20px;color:#fff;margin-bottom:16px">Event <span style="color:var(--green)">Calendar</span></h2>
    <div class="grid grid-2">
        <div class="card">
            <div class="card-body">
                <div class="cal-nav">
                    <button onclick="calNav(-1)">&lt; Prev</button>
                    <span class="month-label" id="calMonthLabel"></span>
                    <button onclick="calNav(1)">Next &gt;</button>
                </div>
                <div class="cal-grid" id="calGrid"></div>
            </div>
        </div>
        <div class="card">
            <div class="card-head"><h3 id="calDayLabel">Select a day</h3></div>
            <div class="card-body" id="calDayEvents"><div style="color:var(--muted);font-size:13px">Click a day to see events</div></div>
        </div>
    </div>
</div>

<!-- Logs Page -->
<div class="page" id="page-logs">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="font-size:20px;color:#fff">Monitor <span style="color:var(--green)">Logs</span></h2>
        <div style="display:flex;gap:8px;align-items:center">
            <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:4px">
                <input type="checkbox" id="autoRefreshLogs" checked> Auto-refresh
            </label>
            <select id="logLines" onchange="loadLogs()" style="background:var(--surface2);border:1px solid var(--border);color:#fff;padding:6px;border-radius:6px;font-size:12px">
                <option value="50">50 lines</option>
                <option value="100" selected>100 lines</option>
                <option value="200">200 lines</option>
                <option value="500">500 lines</option>
            </select>
            <button onclick="loadLogs()" style="background:var(--surface2);border:1px solid var(--border);color:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:12px;font-family:inherit">Refresh</button>
        </div>
    </div>
    <div class="log-viewer" id="logViewer">Loading...</div>
</div>

<!-- Event Detail Modal -->
<div class="modal-bg" id="eventModal" onclick="if(event.target===this)closeModal()">
    <div class="modal">
        <div class="modal-head">
            <h2 id="modalTitle">Event Detail</h2>
            <button class="modal-close" onclick="closeModal()">&times;</button>
        </div>
        <div class="modal-body" id="modalBody"></div>
    </div>
</div>

<div class="toast" id="toast"></div>

<div class="uptime-bar">
    <span id="uptimeText">Dashboard starting...</span>
    <span id="haStatus">HA: checking...</span>
    <span style="margin-left:auto" id="lastRefresh">-</span>
</div>

<script>
const API = '';
let calYear, calMonth, calData = {};
let eventsOffset = 0;
let lastEventCount = 0;
let camAutoRefreshTimer = null;
let logAutoRefreshTimer = null;
const dashboardStartTime = Date.now();

// ── Navigation ──
function showPage(name) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-link').forEach(n => n.classList.remove('active'));
    document.getElementById('page-' + name).classList.add('active');
    const navBtn = document.querySelector(`[onclick="showPage('${name}')"]`);
    if (navBtn) navBtn.classList.add('active');
    // Close mobile menu
    document.querySelector('.nav-links').classList.remove('open');
    // Page-specific actions
    if (name === 'cameras') { loadCameras(); startCamAutoRefresh(); } else { stopCamAutoRefresh(); }
    if (name === 'events') { eventsOffset = 0; searchEvents(); }
    if (name === 'calendar') initCalendar();
    if (name === 'logs') { loadLogs(); startLogAutoRefresh(); } else { stopLogAutoRefresh(); }
}

function startCamAutoRefresh() {
    stopCamAutoRefresh();
    camAutoRefreshTimer = setInterval(() => {
        if (document.getElementById('autoRefreshCams').checked) loadCameras();
    }, 15000);
}
function stopCamAutoRefresh() { if (camAutoRefreshTimer) { clearInterval(camAutoRefreshTimer); camAutoRefreshTimer = null; } }
function startLogAutoRefresh() {
    stopLogAutoRefresh();
    logAutoRefreshTimer = setInterval(() => {
        if (document.getElementById('autoRefreshLogs').checked) loadLogs();
    }, 5000);
}
function stopLogAutoRefresh() { if (logAutoRefreshTimer) { clearInterval(logAutoRefreshTimer); logAutoRefreshTimer = null; } }

function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 4000);
}

// ── Dashboard ──
async function loadDashboard() {
    try {
        const res = await fetch(API + '/api/status');
        const data = await res.json();

        const online = data.cameras.filter(c => c.state === 'recording').length;
        const offline = data.cameras.filter(c => c.state !== 'recording').length;
        const isRunning = data.monitor.running;

        document.getElementById('monitorStatus').innerHTML = `
            <span class="status-dot ${isRunning ? 'on' : 'off'}"></span>
            <span style="font-size:12px;color:${isRunning ? 'var(--green)' : 'var(--red)'}">
                ${isRunning ? 'Running' : 'Stopped'}
            </span>`;

        if (data.new_since_last_visit > 0) {
            document.getElementById('newBadge').style.display = 'inline';
            document.getElementById('newBadge').textContent = data.new_since_last_visit + ' new';
        } else {
            document.getElementById('newBadge').style.display = 'none';
        }

        // Toast for new events
        if (lastEventCount > 0 && data.total_events > lastEventCount) {
            showToast(`New event detected! (${data.total_events - lastEventCount} new)`);
        }
        lastEventCount = data.total_events;

        document.getElementById('statsRow').innerHTML = `
            <div class="stat-box"><div class="n" style="color:var(--green)">${online}</div><div class="l">Online</div></div>
            <div class="stat-box"><div class="n" style="color:${offline ? 'var(--red)' : 'var(--muted)'}">${offline}</div><div class="l">Offline</div></div>
            <div class="stat-box"><div class="n">${data.total_events}</div><div class="l">Events</div></div>
            <div class="stat-box"><div class="n" style="color:var(--green)">${data.new_since_last_visit}</div><div class="l">New</div></div>`;

        document.getElementById('eventCount').textContent = data.total_events + ' total';

        // Recent events
        let evHtml = '';
        if (data.recent_events.length === 0) {
            evHtml = '<div style="color:var(--muted);font-size:13px;padding:12px 0">No events yet. Monitor is scanning - events appear when real activity is detected.</div>';
        }
        for (const ev of data.recent_events) {
            const dt = new Date(ev.start);
            const timeStr = dt.toLocaleString();
            const desc = (ev.descriptions || [])[0] || 'No description';
            const isNew = data.last_visit && ev.start > data.last_visit;
            const dur = (ev.duration_seconds||0);
            evHtml += `<div class="event-card ${isNew ? 'new' : ''}" onclick="openEvent('${ev.event_id}')">
                <div class="event-row">
                    <div class="event-info">
                        <div class="title">${escHtml(desc.substring(0, 100))}</div>
                        <div class="meta">${ev.camera_name} &bull; ${timeStr} &bull; ${dur.toFixed(0)}s &bull; ${ev.frame_count||0} frames</div>
                    </div>
                </div>
            </div>`;
        }
        document.getElementById('recentEvents').innerHTML = evHtml;

        // Camera status
        let camHtml = '';
        for (const cam of data.cameras) {
            const groupColor = cam.group === 'office' ? 'var(--blue)' : 'var(--orange)';
            camHtml += `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
                <span class="status-dot ${cam.state === 'recording' ? 'on' : 'off'}"></span>
                <div style="flex:1">
                    <div style="font-size:13px;font-weight:600">${cam.name}</div>
                    <div style="font-size:11px;color:var(--muted)">${cam.location}</div>
                </div>
                <span style="padding:2px 8px;border-radius:8px;font-size:9px;font-weight:700;background:${groupColor};color:#000">${cam.group.toUpperCase()}</span>
            </div>`;
        }
        document.getElementById('cameraStatus').innerHTML = camHtml;

        // Populate camera filter dropdown (once)
        const sel = document.getElementById('cameraFilter');
        if (sel.options.length <= 1) {
            for (const cam of data.cameras) {
                const opt = document.createElement('option');
                opt.value = cam.name;
                opt.textContent = cam.name;
                sel.appendChild(opt);
            }
        }

        // Update status bar
        document.getElementById('haStatus').textContent = `HA: ${online}/${online+offline} cams`;
        document.getElementById('lastRefresh').textContent = 'Updated ' + new Date().toLocaleTimeString();

    } catch (e) {
        console.error('Dashboard load failed:', e);
    }
}

// ── Cameras ──
async function loadCameras() {
    const grid = document.getElementById('cameraGrid');
    if (grid.children.length <= 1 && grid.children[0]?.tagName !== 'DIV') {
        // Don't show loading if we already have camera cards
    } else if (grid.querySelector('.cam-card') === null) {
        grid.innerHTML = '<div style="color:var(--muted)">Loading live feeds...</div>';
    }
    try {
        const res = await fetch(API + '/api/cameras/live');
        const cameras = await res.json();
        let html = '';
        for (const cam of cameras) {
            const groupColor = cam.group === 'office' ? 'var(--blue)' : 'var(--orange)';
            if (cam.snapshot) {
                html += `<div class="cam-card" data-eid="${cam.entity_id}">
                    <img src="data:image/jpeg;base64,${cam.snapshot}" onclick="this.classList.toggle('zoomed')" />
                    <button class="cam-refresh" onclick="event.stopPropagation();refreshCam('${cam.entity_id}',this)">Refresh</button>
                    <div class="cam-group" style="background:${groupColor}">${cam.group.toUpperCase()}</div>
                    <div class="cam-overlay">
                        <div class="cam-name">${cam.name}</div>
                        <div class="cam-status" style="color:var(--green)">Recording</div>
                    </div>
                </div>`;
            } else {
                html += `<div class="cam-card">
                    <div class="offline">${cam.state === 'recording' ? 'Loading...' : 'OFFLINE'}</div>
                    <div class="cam-group" style="background:${groupColor}">${cam.group.toUpperCase()}</div>
                    <div class="cam-overlay">
                        <div class="cam-name">${cam.name}</div>
                        <div class="cam-status" style="color:var(--red)">${cam.state}</div>
                    </div>
                </div>`;
            }
        }
        grid.innerHTML = html;
    } catch (e) {
        grid.innerHTML = '<div style="color:var(--red)">Failed to load cameras</div>';
    }
}

async function refreshCam(eid, btn) {
    btn.textContent = '...';
    try {
        const res = await fetch(API + '/api/camera/snapshot?id=' + eid);
        const data = await res.json();
        if (data.snapshot) {
            const card = document.querySelector(`[data-eid="${eid}"] img`);
            if (card) card.src = 'data:image/jpeg;base64,' + data.snapshot;
        }
    } catch(e) {}
    btn.textContent = 'Refresh';
}

// ── Events ──
async function searchEvents(append) {
    if (!append) eventsOffset = 0;
    const search = document.getElementById('searchInput').value;
    const camera = document.getElementById('cameraFilter').value;
    const date = document.getElementById('dateFilter').value;
    const params = new URLSearchParams({search, camera, date, limit: 20, offset: eventsOffset});
    try {
        const res = await fetch(API + '/api/events?' + params);
        const data = await res.json();
        let html = '';
        for (const ev of data.events) {
            const dt = new Date(ev.start);
            const desc = (ev.descriptions || [])[0] || 'No description';
            const groupColor = ev.group === 'office' ? 'var(--blue)' : 'var(--orange)';
            const thumbHtml = ev.thumbnail
                ? `<img class="event-thumb" src="data:image/jpeg;base64,${ev.thumbnail}" />`
                : '<div class="event-thumb" style="display:flex;align-items:center;justify-content:center;color:#333">No img</div>';
            html += `<div class="event-card" onclick="openEvent('${ev.event_id}')">
                <div class="event-row">
                    ${thumbHtml}
                    <div class="event-info">
                        <div class="title">${escHtml(desc.substring(0, 150))}<span class="event-type" style="background:${groupColor}">${(ev.group||'?').toUpperCase()}</span></div>
                        <div class="meta">${ev.camera_name} &bull; ${dt.toLocaleString()} &bull; ${(ev.duration_seconds||0).toFixed(0)}s &bull; ${ev.frame_count||0} frames</div>
                        <div class="desc">${escHtml((ev.descriptions || []).slice(1).join(' | ').substring(0, 200))}</div>
                    </div>
                </div>
            </div>`;
        }
        if (append) {
            document.getElementById('eventsList').innerHTML += html;
        } else {
            document.getElementById('eventsList').innerHTML = html || '<div style="color:var(--muted);padding:20px;text-align:center">No events found</div>';
        }
        eventsOffset += data.events.length;
        document.getElementById('eventsMore').innerHTML = eventsOffset < data.total
            ? `<button onclick="searchEvents(true)" style="background:var(--surface2);border:1px solid var(--border);color:#fff;padding:10px 24px;border-radius:8px;cursor:pointer;font-family:inherit">Load More (${data.total - eventsOffset} remaining)</button>`
            : `<span style="color:var(--muted);font-size:12px">${data.total} total events</span>`;
    } catch (e) {
        document.getElementById('eventsList').innerHTML = '<div style="color:var(--red)">Failed to load events</div>';
    }
}

// ── Event Detail ──
async function openEvent(eventId) {
    document.getElementById('eventModal').classList.add('open');
    document.getElementById('modalBody').innerHTML = '<div style="color:var(--muted)">Loading...</div>';
    try {
        const res = await fetch(API + '/api/event?id=' + eventId);
        const ev = await res.json();
        const dt = new Date(ev.start);
        document.getElementById('modalTitle').textContent = ev.camera_name + ' - ' + dt.toLocaleString();
        let html = `<div style="margin-bottom:16px;font-size:13px;color:var(--muted)">
            Duration: ${(ev.duration_seconds||0).toFixed(0)}s &bull; Frames: ${ev.frame_count} &bull; Trigger: ${ev.trigger || '?'}
        </div>`;
        // Descriptions
        for (const d of (ev.descriptions || [])) {
            html += `<div style="background:var(--surface2);border-radius:8px;padding:12px;margin-bottom:8px;font-size:13px;line-height:1.7;border-left:3px solid var(--green)">${escHtml(d)}</div>`;
        }
        // Frame navigation
        const frames = (ev.images || []);
        if (frames.length > 1) {
            html += '<div class="frame-nav">';
            frames.forEach((img, i) => {
                const isPre = img.name.startsWith('pre_');
                html += `<button class="frame-btn ${isPre?'pre':''} ${i===0?'active':''}" onclick="showFrame(this,${i})">${isPre?'Pre':'F'+i}</button>`;
            });
            html += '</div>';
        }
        // Images
        for (let i = 0; i < frames.length; i++) {
            const img = frames[i];
            if (img.data) {
                html += `<img class="modal-img" data-frame="${i}" style="${i>0?'display:none':''}" src="data:image/jpeg;base64,${img.data}" onclick="this.classList.toggle('zoomed')" />`;
            }
        }
        if (frames.length > 0) {
            html += `<div style="font-size:11px;color:var(--muted);margin-top:4px" id="frameLabel">${frames[0].name} (${(frames[0].size/1024).toFixed(1)} KB)</div>`;
        }
        document.getElementById('modalBody').innerHTML = html;
    } catch (e) {
        document.getElementById('modalBody').innerHTML = '<div style="color:var(--red)">Failed to load event</div>';
    }
}
function showFrame(btn, idx) {
    document.querySelectorAll('.frame-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.modal-img').forEach(img => img.style.display = 'none');
    const img = document.querySelector(`.modal-img[data-frame="${idx}"]`);
    if (img) img.style.display = 'block';
}
function closeModal() { document.getElementById('eventModal').classList.remove('open'); }

// ── Calendar ──
async function initCalendar() {
    const now = new Date();
    calYear = calYear || now.getFullYear();
    calMonth = calMonth !== undefined ? calMonth : now.getMonth();
    try {
        const res = await fetch(API + '/api/calendar');
        calData = await res.json();
    } catch (e) {}
    renderCalendar();
}
function calNav(dir) { calMonth += dir; if (calMonth > 11) { calMonth = 0; calYear++; } if (calMonth < 0) { calMonth = 11; calYear--; } renderCalendar(); }
function renderCalendar() {
    const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    document.getElementById('calMonthLabel').textContent = months[calMonth] + ' ' + calYear;
    const grid = document.getElementById('calGrid');
    const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    let html = days.map(d => `<div class="cal-head">${d}</div>`).join('');
    const first = new Date(calYear, calMonth, 1);
    const last = new Date(calYear, calMonth + 1, 0);
    const today = new Date();
    for (let i = 0; i < first.getDay(); i++) {
        const d = new Date(calYear, calMonth, -first.getDay() + i + 1);
        html += `<div class="cal-day other-month">${d.getDate()}</div>`;
    }
    for (let d = 1; d <= last.getDate(); d++) {
        const dateStr = `${calYear}-${String(calMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
        const isToday = today.getFullYear() === calYear && today.getMonth() === calMonth && today.getDate() === d;
        const count = calData[dateStr] || 0;
        const hasEvents = count > 0;
        html += `<div class="cal-day${isToday ? ' today' : ''}${hasEvents ? ' has-events' : ''}" onclick="selectCalDay('${dateStr}',this)">${d}${hasEvents ? '<br><span style="font-size:9px;color:var(--green)">' + count + '</span>' : ''}</div>`;
    }
    grid.innerHTML = html;
}
async function selectCalDay(dateStr, el) {
    document.querySelectorAll('.cal-day').forEach(d => d.classList.remove('selected'));
    if (el) el.classList.add('selected');
    document.getElementById('calDayLabel').textContent = new Date(dateStr + 'T12:00:00').toLocaleDateString('en-US', {weekday:'long',month:'long',day:'numeric',year:'numeric'});
    try {
        const res = await fetch(API + '/api/events?date=' + dateStr + '&limit=50');
        const data = await res.json();
        let html = '';
        if (data.events.length === 0) html = '<div style="color:var(--muted);font-size:13px">No events on this day</div>';
        for (const ev of data.events) {
            const dt = new Date(ev.start);
            const desc = (ev.descriptions || [])[0] || 'No description';
            html += `<div class="event-card" onclick="openEvent('${ev.event_id}')" style="margin-bottom:8px">
                <div style="padding:10px 14px">
                    <div style="font-size:13px;font-weight:600;color:#fff">${escHtml(desc.substring(0,120))}</div>
                    <div style="font-size:11px;color:var(--muted);margin-top:4px">${ev.camera_name} &bull; ${dt.toLocaleTimeString()}</div>
                </div>
            </div>`;
        }
        document.getElementById('calDayEvents').innerHTML = html;
    } catch (e) {}
}

// ── Logs ──
async function loadLogs() {
    const lines = document.getElementById('logLines').value;
    try {
        const res = await fetch(API + '/api/monitor/log?lines=' + lines);
        const data = await res.json();
        const viewer = document.getElementById('logViewer');
        // Colorize log lines
        const colored = data.lines.map(l => {
            if (l.includes('WARNING') || l.includes('WARN')) return `<span class="warn">${escHtml(l)}</span>`;
            if (l.includes('ERROR')) return `<span class="err">${escHtml(l)}</span>`;
            if (l.includes('REAL CHANGE') || l.includes('EVENT STARTED') || l.includes('BACK ONLINE')) return `<span class="info-highlight">${escHtml(l)}</span>`;
            return escHtml(l);
        }).join('');
        viewer.innerHTML = colored;
        viewer.scrollTop = viewer.scrollHeight;
    } catch (e) {
        document.getElementById('logViewer').textContent = 'Failed to load logs';
    }
}

// ── Utils ──
function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

function formatUptime(ms) {
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm ' + (s % 60) + 's';
}

function updateClock() {
    document.getElementById('clock').textContent = new Date().toLocaleTimeString();
    document.getElementById('uptimeText').textContent = 'Uptime: ' + formatUptime(Date.now() - dashboardStartTime);
}

// ── Keyboard ──
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        closeModal();
        document.querySelectorAll('.zoomed').forEach(i => i.classList.remove('zoomed'));
    }
});

// ── Init ──
loadDashboard();
updateClock();
setInterval(updateClock, 1000);
setInterval(loadDashboard, 30000);
</script>
</body></html>"""


def main():
    print(f"Camera Monitor Dashboard starting on http://localhost:{PORT}")
    print(f"Events dir: {EVENTS_DIR}")
    print(f"Logs dir: {LOG_DIR}")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)

    # Open browser
    import webbrowser
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    print(f"Dashboard running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
