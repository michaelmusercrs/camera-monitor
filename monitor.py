#!/usr/bin/env python3
"""
Camera Monitor v2 - Smart security camera monitoring with HA integration.

Architecture:
  1. HA motion events trigger snapshot capture (no blind polling)
  2. Rolling 15-second pre-event buffer per camera
  3. OpenCV SSIM pre-filter eliminates false positives before AI
  4. IR mode switch detection (file size jump) suppresses day/night transitions
  5. Ollama LLaVA only sees frames with real visual changes
  6. Event-based tracking: groups frames into arrival/departure/activity events
  7. Self-improving: adjusts thresholds based on observed patterns

Usage:
  python monitor.py                    # Normal operation
  python monitor.py --preview          # Preview mode: replay old data through new filters
  python monitor.py --test-cameras     # Test camera connectivity
"""

import os
import sys
import json
import time
import base64
import hashlib
import logging
import argparse
import threading
import urllib.request
import urllib.error
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path
from collections import deque, defaultdict
from dataclasses import dataclass, field

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).parent
PROFILES_FILE = BASE_DIR / "camera_profiles.json"
EVENTS_DIR = BASE_DIR / "events"
BASELINES_DIR = BASE_DIR / "baselines"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"

# Load .env file if present
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

HA_URL = os.environ.get("HA_URL", "http://192.168.86.102:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = "llava:7b"

# Timing
BUFFER_SECONDS = 15          # Pre-event rolling buffer
BUFFER_INTERVAL = 3          # Seconds between buffer captures
SCAN_INTERVAL = 20           # Normal scan interval per camera (seconds)
MOTION_BOOST_INTERVAL = 5    # Faster scan when HA says motion detected (seconds)
MOTION_BOOST_DURATION = 60   # How long to stay in boosted mode after motion event
MOTION_POLL_INTERVAL = 2     # How often to check HA for motion events
STABILIZE_SECONDS = 30       # Wait this long with no changes before closing an event
EVENT_CAPTURE_INTERVAL = 5   # Seconds between captures during an active event
BASELINE_INTERVAL = 3600     # Update baseline hourly

# Filtering
SIZE_JUMP_PCT = 25           # % change in file size = IR mode switch
IR_SETTLE_FRAMES = 3         # Skip this many frames after IR switch
MIN_CHANGE_AREA_PCT = 2.0    # Minimum % of pixels that must differ

for d in [EVENTS_DIR, BASELINES_DIR, LOG_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("camv2")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(ch)
fh = RotatingFileHandler(LOG_DIR / "monitor.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s"))
logger.addHandler(fh)


# ══════════════════════════════════════════════════════════════
# CAMERA PROFILE
# ══════════════════════════════════════════════════════════════
@dataclass
class CameraProfile:
    entity_id: str
    name: str
    group: str
    location: str
    enabled: bool
    ir_night_size_range: list
    daylight_size_range: list
    ir_to_day_hour: int
    day_to_ir_hour: int
    ssim_threshold: float
    night_ssim_threshold: float
    motion_entity: str = None
    notes: str = ""


def load_profiles():
    """Load camera profiles from JSON."""
    with open(PROFILES_FILE, "r") as f:
        data = json.load(f)

    profiles = {}
    motion_map = data.get("ha_motion_entities", {})
    for eid, cfg in data["cameras"].items():
        profiles[eid] = CameraProfile(
            entity_id=eid,
            name=cfg["name"],
            group=cfg["group"],
            location=cfg["location"],
            enabled=cfg["enabled"],
            ir_night_size_range=cfg["ir_night_size_range"],
            daylight_size_range=cfg["daylight_size_range"],
            ir_to_day_hour=cfg["ir_to_day_hour"],
            day_to_ir_hour=cfg["day_to_ir_hour"],
            ssim_threshold=cfg["ssim_threshold"],
            night_ssim_threshold=cfg["night_ssim_threshold"],
            motion_entity=motion_map.get(eid),
            notes=cfg.get("notes", ""),
        )
    return profiles, data.get("ha_motion_sensors", {})


# ══════════════════════════════════════════════════════════════
# HOME ASSISTANT API
# ══════════════════════════════════════════════════════════════
def ha_get(path, timeout=15):
    """GET from Home Assistant API."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout).read()


def ha_get_json(path, timeout=15):
    return json.loads(ha_get(path, timeout))


def grab_snapshot(entity_id, timeout=15):
    """Get camera snapshot as raw JPEG bytes."""
    try:
        return ha_get(f"/api/camera_proxy/{entity_id}", timeout=timeout)
    except Exception as e:
        logger.warning(f"Snapshot failed for {entity_id}: {e}")
        return None


def get_motion_event_time(entity_id):
    """Get the last motion event timestamp for a camera's doorbell_message entity."""
    try:
        state = ha_get_json(f"/api/states/{entity_id}", timeout=10)
        ts = state.get("state", "")
        if ts and ts not in ("unavailable", "unknown"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def get_binary_sensor_state(entity_id):
    """Check if a binary motion sensor is on."""
    try:
        state = ha_get_json(f"/api/states/{entity_id}", timeout=10)
        return state.get("state") == "on"
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# IMAGE ANALYSIS
# ══════════════════════════════════════════════════════════════
def bytes_to_cv2(img_bytes):
    """Convert JPEG bytes to OpenCV image."""
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)


ANALYSIS_WIDTH = 320  # Downscale to this width for SSIM/diff (much faster)

def _downscale(img, width=ANALYSIS_WIDTH):
    """Downscale image for faster analysis. Returns downscaled image."""
    h, w = img.shape[:2]
    if w <= width:
        return img
    scale = width / w
    return cv2.resize(img, (width, int(h * scale)), interpolation=cv2.INTER_AREA)


def compute_ssim(img1_bytes, img2_bytes):
    """Compute structural similarity between two images. Returns 0.0-1.0."""
    g1 = bytes_to_cv2(img1_bytes)
    g2 = bytes_to_cv2(img2_bytes)
    if g1 is None or g2 is None:
        return 0.0
    # Downscale for speed (6x faster on typical camera frames)
    g1 = _downscale(g1)
    g2 = _downscale(g2)
    if g1.shape != g2.shape:
        g2 = cv2.resize(g2, (g1.shape[1], g1.shape[0]))
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2
    g1 = g1.astype(np.float64)
    g2 = g2.astype(np.float64)
    mu1 = cv2.GaussianBlur(g1, (11, 11), 1.5)
    mu2 = cv2.GaussianBlur(g2, (11, 11), 1.5)
    mu1_sq = mu1 ** 2
    mu2_sq = mu2 ** 2
    mu1_mu2 = mu1 * mu2
    sigma1_sq = cv2.GaussianBlur(g1 ** 2, (11, 11), 1.5) - mu1_sq
    sigma2_sq = cv2.GaussianBlur(g2 ** 2, (11, 11), 1.5) - mu2_sq
    sigma12 = cv2.GaussianBlur(g1 * g2, (11, 11), 1.5) - mu1_mu2
    ssim_map = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))
    return float(ssim_map.mean())


def compute_diff_pct(img1_bytes, img2_bytes, threshold=30):
    """Compute percentage of pixels that changed significantly."""
    g1 = bytes_to_cv2(img1_bytes)
    g2 = bytes_to_cv2(img2_bytes)
    if g1 is None or g2 is None:
        return 100.0
    g1 = _downscale(g1)
    g2 = _downscale(g2)
    if g1.shape != g2.shape:
        g2 = cv2.resize(g2, (g1.shape[1], g1.shape[0]))
    diff = cv2.absdiff(g1, g2)
    changed = np.sum(diff > threshold)
    total = diff.size
    return (changed / total) * 100.0


def detect_contour_regions(img1_bytes, img2_bytes, min_area=200):
    """Find bounding boxes of changed regions between two frames."""
    g1 = bytes_to_cv2(img1_bytes)
    g2 = bytes_to_cv2(img2_bytes)
    if g1 is None or g2 is None:
        return []
    g1 = _downscale(g1)
    g2 = _downscale(g2)
    if g1.shape != g2.shape:
        g2 = cv2.resize(g2, (g1.shape[1], g1.shape[0]))
    diff = cv2.absdiff(g1, g2)
    _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
    thresh = cv2.dilate(thresh, None, iterations=3)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions = []
    for c in contours:
        area = cv2.contourArea(c)
        if area >= min_area:
            x, y, w, h = cv2.boundingRect(c)
            regions.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h), "area": int(area)})
    return sorted(regions, key=lambda r: r["area"], reverse=True)[:10]  # Cap at 10 regions


# ══════════════════════════════════════════════════════════════
# OLLAMA AI
# ══════════════════════════════════════════════════════════════
def ollama_analyze(img_bytes, prompt, timeout=120):
    """Send image to Ollama for analysis. Returns text response."""
    b64 = base64.b64encode(img_bytes).decode("ascii")
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 400},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    result = json.loads(resp.read())
    return result.get("response", "").strip()


DESCRIBE_PROMPT = """Security camera snapshot. List ONLY what you see. Be factual, no guessing.
- People: count, position, activity (or "none")
- Vehicles: type, color, location (or "none")
- Animals: type (or "none")
- Doors/gates: open/closed
- Weather (outdoor only): conditions
- Lighting: daylight / artificial / IR night

Short bullet list only. Do NOT describe image quality or camera angle. If you cannot clearly identify something, do not mention it."""

COMPARE_PROMPT = """Previous security camera scene: {prev_scene}

Look at the CURRENT image. Report ONLY real physical changes.
IGNORE: shadows moving, lighting shifts, IR camera noise, compression artifacts, timestamp changes.

If nothing meaningful changed, respond EXACTLY with: NONE

If something changed, respond with ONE line like:
"Person arrived - man in dark jacket at desk"
"Vehicle departed - white SUV no longer in lot"
"Animal - cat near doorway"
"Door opened - front gate now open"

Then 1-2 bullet details max. Do NOT list things that stayed the same."""


# ══════════════════════════════════════════════════════════════
# CAMERA STATE TRACKER
# ══════════════════════════════════════════════════════════════
@dataclass
class CameraState:
    profile: CameraProfile
    buffer: deque = field(default_factory=lambda: deque(maxlen=6))  # ~15s at 3s intervals
    baseline_bytes: bytes = None
    baseline_time: datetime = None
    prev_bytes: bytes = None
    prev_hash: str = None
    prev_description: str = None
    prev_size: int = 0
    ir_settle_counter: int = 0      # Frames to skip after IR switch
    active_event: dict = None       # Current event being tracked
    last_motion_time: datetime = None
    last_analysis_time: datetime = None
    last_scan_time: datetime = None
    motion_boost_until: datetime = None  # Scan faster until this time
    consecutive_none: int = 0       # Consecutive "NONE" results
    consecutive_failures: int = 0   # Consecutive snapshot failures
    offline_until: datetime = None  # Skip camera until this time after repeated failures
    total_captured: int = 0
    total_sent_to_ai: int = 0
    total_real_events: int = 0
    total_filtered: int = 0
    # Self-tuning
    false_positive_streak: int = 0
    ssim_adjustment: float = 0.0    # Added to threshold if too many FPs


class EventTracker:
    """Tracks an ongoing event (e.g., vehicle in lot, person in office)."""

    def __init__(self, camera_id, camera_name, trigger, event_dir):
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.trigger = trigger
        self.event_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{camera_id.split('.')[-1]}"
        self.event_dir = event_dir / self.event_id
        self.event_dir.mkdir(parents=True, exist_ok=True)
        self.start_time = datetime.now()
        self.last_change_time = datetime.now()
        self.frames = []
        self.descriptions = []
        self.ai_summary = None
        self.closed = False

    def add_frame(self, img_bytes, description=None, is_pre_buffer=False):
        ts = datetime.now()
        label = "pre" if is_pre_buffer else f"frame_{len(self.frames):03d}"
        filename = f"{label}_{ts.strftime('%H%M%S')}.jpg"
        filepath = self.event_dir / filename
        filepath.write_bytes(img_bytes)
        self.frames.append({
            "file": filename,
            "time": ts.isoformat(),
            "size": len(img_bytes),
            "description": description,
            "is_pre_buffer": is_pre_buffer,
        })
        if description and not is_pre_buffer:
            self.descriptions.append(description)
            self.last_change_time = ts

    def save_pre_buffer(self, buffer_frames):
        """Save the rolling buffer as pre-event frames with original timestamps."""
        for i, (ts, img_bytes) in enumerate(buffer_frames):
            filename = f"pre_{ts.strftime('%H%M%S')}_{i:02d}.jpg"
            filepath = self.event_dir / filename
            filepath.write_bytes(img_bytes)
            self.frames.append({
                "file": filename,
                "time": ts.isoformat(),
                "size": len(img_bytes),
                "description": None,
                "is_pre_buffer": True,
            })

    def duration_seconds(self):
        return (datetime.now() - self.start_time).total_seconds()

    def seconds_since_last_change(self):
        return (datetime.now() - self.last_change_time).total_seconds()

    def close(self, final_description=None):
        self.closed = True
        end_time = datetime.now()
        summary = {
            "event_id": self.event_id,
            "camera": self.camera_id,
            "camera_name": self.camera_name,
            "group": None,
            "trigger": self.trigger,
            "start": self.start_time.isoformat(),
            "end": end_time.isoformat(),
            "duration_seconds": (end_time - self.start_time).total_seconds(),
            "frame_count": len(self.frames),
            "descriptions": self.descriptions,
            "final_description": final_description,
            "ai_summary": self.ai_summary,
        }
        with open(self.event_dir / "event.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        return summary


# ══════════════════════════════════════════════════════════════
# PRE-FILTER PIPELINE
# ══════════════════════════════════════════════════════════════
class PreFilter:
    """Decides whether a frame should be sent to AI or discarded."""

    @staticmethod
    def run(cam_state: CameraState, img_bytes: bytes) -> tuple:
        """
        Returns (should_analyze: bool, reason: str, metadata: dict)
        """
        profile = cam_state.profile
        img_size = len(img_bytes)
        now = datetime.now()
        hour = now.hour
        meta = {"size": img_size, "ssim": None, "diff_pct": None, "regions": None}

        # 1. Camera disabled
        if not profile.enabled:
            return False, "camera_disabled", meta

        # 2. Identical frame (hash match)
        img_hash = hashlib.md5(img_bytes).hexdigest()
        if cam_state.prev_hash == img_hash:
            return False, "identical_frame", meta

        # 3. IR mode switch detection
        if cam_state.prev_size > 0:
            size_change_pct = abs(img_size - cam_state.prev_size) / cam_state.prev_size * 100
            if size_change_pct > SIZE_JUMP_PCT:
                cam_state.ir_settle_counter = IR_SETTLE_FRAMES
                return False, f"ir_switch_{size_change_pct:.0f}pct", meta

        # 4. IR settling period
        if cam_state.ir_settle_counter > 0:
            cam_state.ir_settle_counter -= 1
            return False, f"ir_settling_{cam_state.ir_settle_counter}_remaining", meta

        # 5. SSIM comparison against previous frame
        if cam_state.prev_bytes is not None:
            ssim = compute_ssim(cam_state.prev_bytes, img_bytes)
            meta["ssim"] = round(ssim, 4)

            # Use night threshold during dark hours
            is_night = hour < profile.ir_to_day_hour or hour >= profile.day_to_ir_hour
            threshold = profile.night_ssim_threshold if is_night else profile.ssim_threshold
            # Apply self-tuning adjustment
            threshold = min(0.99, threshold + cam_state.ssim_adjustment)

            if ssim > threshold:
                return False, f"ssim_too_high_{ssim:.3f}>{threshold:.3f}", meta

            # 6. Compute diff percentage and regions
            diff_pct = compute_diff_pct(cam_state.prev_bytes, img_bytes)
            meta["diff_pct"] = round(diff_pct, 2)

            if diff_pct < MIN_CHANGE_AREA_PCT:
                return False, f"diff_too_small_{diff_pct:.1f}pct", meta

            # 7. Find changed regions
            regions = detect_contour_regions(cam_state.prev_bytes, img_bytes)
            meta["regions"] = regions

        return True, "passed_all_filters", meta


# ══════════════════════════════════════════════════════════════
# SELF-IMPROVEMENT ENGINE
# ══════════════════════════════════════════════════════════════
class SelfTuner:
    """Adjusts thresholds based on observed patterns."""

    STATS_FILE = DATA_DIR / "tuning_stats.json"

    def __init__(self):
        self.stats = self._load()

    def _load(self):
        if self.STATS_FILE.exists():
            with open(self.STATS_FILE) as f:
                return json.load(f)
        return {"cameras": {}, "global": {"total_analyzed": 0, "total_none": 0, "total_real": 0}}

    def save(self):
        with open(self.STATS_FILE, "w") as f:
            json.dump(self.stats, f, indent=2)

    def record_result(self, camera_id, was_real, ssim_value=None, hour=None):
        """Record whether an AI analysis found something real or not."""
        self.stats["global"]["total_analyzed"] += 1
        if was_real:
            self.stats["global"]["total_real"] += 1
        else:
            self.stats["global"]["total_none"] += 1

        cam = self.stats["cameras"].setdefault(camera_id, {
            "analyzed": 0, "real": 0, "none": 0, "hourly_none": {}
        })
        cam["analyzed"] += 1
        if was_real:
            cam["real"] += 1
        else:
            cam["none"] += 1
            # Track which hours produce the most false positives
            h = str(hour) if hour is not None else "?"
            cam["hourly_none"][h] = cam["hourly_none"].get(h, 0) + 1

    def get_adjustment(self, cam_state: CameraState) -> float:
        """Returns SSIM threshold adjustment. Positive = stricter (more filtering)."""
        cam_stats = self.stats["cameras"].get(cam_state.profile.entity_id)
        if not cam_stats or cam_stats["analyzed"] < 10:
            return 0.0

        # If >70% of AI calls return NONE, tighten the threshold
        none_rate = cam_stats["none"] / cam_stats["analyzed"]
        if none_rate > 0.7:
            adjustment = min(0.05, (none_rate - 0.5) * 0.1)
            return adjustment

        # If <30% return NONE, loosen slightly (we might be missing things)
        if none_rate < 0.3 and cam_state.ssim_adjustment > 0:
            return max(-0.02, cam_state.ssim_adjustment - 0.01)

        return cam_state.ssim_adjustment


# ══════════════════════════════════════════════════════════════
# MAIN MONITOR
# ══════════════════════════════════════════════════════════════
class CameraMonitor:
    def __init__(self):
        self.profiles, self.motion_sensors = load_profiles()
        self.cam_states = {}
        self.tuner = SelfTuner()
        self.running = False

        for eid, profile in self.profiles.items():
            self.cam_states[eid] = CameraState(profile=profile)

    def update_baselines(self):
        """Capture fresh baseline for each camera."""
        logger.info("Updating baselines...")
        for eid, state in self.cam_states.items():
            if not state.profile.enabled:
                continue
            # Skip cameras in backoff
            if state.offline_until and datetime.now() < state.offline_until:
                continue
            img = grab_snapshot(eid)
            if img:
                state.baseline_bytes = img
                state.baseline_time = datetime.now()
                if state.consecutive_failures >= 3:
                    logger.info(f"  {state.profile.name}: BACK ONLINE")
                state.consecutive_failures = 0
                state.offline_until = None
                # Save to disk (keep only latest per camera)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe = eid.replace("camera.", "")
                path = BASELINES_DIR / f"{safe}_{ts}.jpg"
                path.write_bytes(img)
                # Clean old baselines for this camera (keep last 2)
                old = sorted(BASELINES_DIR.glob(f"{safe}_*.jpg"))
                for old_f in old[:-2]:
                    old_f.unlink(missing_ok=True)
                logger.info(f"  Baseline: {state.profile.name} ({len(img):,} bytes)")
            else:
                state.consecutive_failures += 1
                if state.consecutive_failures <= 2:
                    logger.warning(f"  Baseline FAILED: {state.profile.name}")
                elif state.consecutive_failures == 3:
                    state.offline_until = datetime.now() + timedelta(minutes=5)
                    logger.warning(f"  {state.profile.name}: offline, backing off 5min")

    def fill_buffers(self):
        """Capture one frame per camera into rolling buffer."""
        for eid, state in self.cam_states.items():
            if not state.profile.enabled:
                continue
            if state.offline_until and datetime.now() < state.offline_until:
                continue
            img = grab_snapshot(eid)
            if img:
                state.buffer.append((datetime.now(), img))

    def check_motion_triggers(self):
        """Check HA for motion events. Returns list of camera IDs with new motion."""
        triggered = []

        # Check doorbell_message events
        for eid, state in self.cam_states.items():
            if not state.profile.enabled or not state.profile.motion_entity:
                continue
            # Skip offline cameras
            if state.offline_until and datetime.now() < state.offline_until:
                continue
            try:
                event_time = get_motion_event_time(state.profile.motion_entity)
            except Exception:
                continue
            if event_time and event_time != state.last_motion_time:
                if event_time.tzinfo:
                    event_time_naive = event_time.replace(tzinfo=None)
                else:
                    event_time_naive = event_time
                age = (datetime.now() - event_time_naive).total_seconds()
                if 0 < age < 60:
                    triggered.append(eid)
                    logger.info(f"  MOTION: {state.profile.name} ({age:.0f}s ago)")
                state.last_motion_time = event_time

        # Check binary motion sensors
        for sensor_id, camera_ids in self.motion_sensors.items():
            try:
                if get_binary_sensor_state(sensor_id):
                    for cid in camera_ids:
                        if cid in self.cam_states and self.cam_states[cid].profile.enabled:
                            if cid not in triggered:
                                triggered.append(cid)
                                logger.info(f"  MOTION SENSOR: {sensor_id} -> {self.cam_states[cid].profile.name}")
            except Exception:
                continue

        return triggered

    def process_camera(self, eid, trigger="motion"):
        """Process a single camera: capture, filter, analyze if needed."""
        state = self.cam_states[eid]
        profile = state.profile

        # Skip cameras in backoff period
        if state.offline_until and datetime.now() < state.offline_until:
            return None

        img = grab_snapshot(eid)
        if not img:
            state.consecutive_failures += 1
            if state.consecutive_failures <= 2:
                logger.warning(f"  {profile.name}: snapshot failed (attempt {state.consecutive_failures})")
            elif state.consecutive_failures == 3:
                # Backoff: skip this camera for 5 minutes
                state.offline_until = datetime.now() + timedelta(minutes=5)
                logger.warning(f"  {profile.name}: offline - backing off for 5min")
            elif state.consecutive_failures % 60 == 0:
                # Periodic check-in (every ~5min * 60 = 5h)
                state.offline_until = datetime.now() + timedelta(minutes=5)
                logger.info(f"  {profile.name}: still offline ({state.consecutive_failures} failures)")
            else:
                state.offline_until = datetime.now() + timedelta(minutes=5)
            return None

        # Camera recovered from failure
        if state.consecutive_failures >= 3:
            logger.info(f"  {profile.name}: BACK ONLINE after {state.consecutive_failures} failures")
        state.consecutive_failures = 0
        state.offline_until = None

        state.total_captured += 1
        img_size = len(img)

        # Run pre-filter pipeline
        should_analyze, reason, meta = PreFilter.run(state, img)

        # Update state regardless
        state.prev_hash = hashlib.md5(img).hexdigest()
        state.prev_size = img_size
        state.buffer.append((datetime.now(), img))

        if not should_analyze:
            state.total_filtered += 1
            if state.total_filtered % 10 == 1:
                logger.info(f"  SCAN {profile.name}: filtered ({reason}) [captured={state.total_captured} filtered={state.total_filtered}]")
            else:
                logger.debug(f"  FILTERED {profile.name}: {reason}")

            # If we have an active event and scene stabilized, close it
            if state.active_event and state.active_event.seconds_since_last_change() > STABILIZE_SECONDS:
                self._close_event(state)

            state.prev_bytes = img
            return {"action": "filtered", "reason": reason}

        # ── PASSED FILTERS - Send to AI ──
        state.total_sent_to_ai += 1
        logger.info(f"  ANALYZING {profile.name}: ssim={meta.get('ssim','?')} diff={meta.get('diff_pct','?')}% regions={len(meta.get('regions') or [])}")

        # Describe or compare
        if state.prev_description is None:
            # First real analysis - describe the scene
            description = ollama_analyze(img, DESCRIBE_PROMPT)
            logger.info(f"  SCENE: {description[:200]}")
            state.prev_description = description
            state.prev_bytes = img
            return {"action": "baseline_described", "description": description}
        else:
            # Compare to previous
            prompt = COMPARE_PROMPT.format(prev_scene=state.prev_description)
            comparison = ollama_analyze(img, prompt)

            is_none = comparison.strip().upper().startswith("NONE")

            if is_none:
                state.consecutive_none += 1
                state.false_positive_streak += 1
                self.tuner.record_result(eid, was_real=False, ssim_value=meta.get("ssim"), hour=datetime.now().hour)

                # Self-tune: if AI keeps saying NONE, tighten the filter
                if state.false_positive_streak >= 3:
                    adj = self.tuner.get_adjustment(state)
                    if adj > state.ssim_adjustment:
                        state.ssim_adjustment = adj
                        logger.info(f"  SELF-TUNE {profile.name}: ssim_adjustment -> +{adj:.3f}")
                    state.false_positive_streak = 0

                logger.info(f"  NONE - AI found no real changes (streak: {state.consecutive_none})")
                state.prev_bytes = img
                return {"action": "ai_none", "comparison": comparison}

            # ── REAL CHANGE DETECTED ──
            state.consecutive_none = 0
            state.false_positive_streak = 0
            state.total_real_events += 1
            self.tuner.record_result(eid, was_real=True, ssim_value=meta.get("ssim"), hour=datetime.now().hour)

            logger.info(f"  *** REAL CHANGE: {comparison[:300]}")

            # Get updated scene description
            new_desc = ollama_analyze(img, DESCRIBE_PROMPT)
            state.prev_description = new_desc

            # Create or update event
            if state.active_event is None:
                event = EventTracker(eid, profile.name, trigger, EVENTS_DIR)
                event.save_pre_buffer(list(state.buffer))
                state.active_event = event
                logger.info(f"  EVENT STARTED: {event.event_id}")

            state.active_event.add_frame(img, description=comparison)
            state.prev_bytes = img

            return {
                "action": "real_change",
                "comparison": comparison,
                "scene": new_desc,
                "event_id": state.active_event.event_id,
                "regions": meta.get("regions"),
            }

    def _close_event(self, state):
        """Close an active event when the scene has stabilized."""
        event = state.active_event
        if not event or event.closed:
            return

        # Capture final stabilized frame
        final_img = grab_snapshot(state.profile.entity_id)
        if final_img:
            final_desc = ollama_analyze(final_img, DESCRIBE_PROMPT)
            event.add_frame(final_img, description=f"STABILIZED: {final_desc}")

        event_summary = event.close(final_description=state.prev_description)
        event_summary["group"] = state.profile.group
        logger.info(f"  EVENT CLOSED: {event.event_id} | {event.duration_seconds():.0f}s | {len(event.frames)} frames")

        # Log the event summary
        log_path = LOG_DIR / "events.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event_summary, ensure_ascii=False) + "\n")

        state.active_event = None

        # Rebuild the live report
        try:
            self._build_live_report()
        except Exception as e:
            logger.warning(f"  Report generation failed: {e}")

    def _build_live_report(self):
        """Generate a live HTML report from all closed events."""
        events_log = LOG_DIR / "events.jsonl"
        if not events_log.exists():
            return

        events = []
        with open(events_log, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    events.append(json.loads(line.strip()))

        if not events:
            return

        # Build event cards with embedded images
        import base64 as b64mod
        cards_html = ""
        log_rows = ""
        for ev in reversed(events):  # newest first
            cam = ev.get("camera_name", "?")
            group = ev.get("group", "?")
            start = ev.get("start", "")
            dur = ev.get("duration_seconds", 0)
            frames = ev.get("frame_count", 0)
            descs = ev.get("descriptions", [])
            eid = ev.get("event_id", "?")
            event_dir = EVENTS_DIR / eid

            # Summary text = first non-stabilized description
            summary = "No description"
            for d in descs:
                if not d.startswith("STABILIZED:"):
                    summary = d
                    break

            # Time formatting
            try:
                dt = datetime.fromisoformat(start)
                time_str = dt.strftime("%b %d, %I:%M:%S %p")
                time_short = dt.strftime("%I:%M %p")
            except Exception:
                time_str = start
                time_short = start

            group_color = "#4a9eff" if group == "office" else "#ff9f43"
            group_label = "OFFICE" if group == "office" else "HOME"

            # Embed best image (first non-pre-buffer frame)
            img_html = '<div style="width:360px;height:270px;background:#1a1a2e;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#444;flex-shrink:0">No image</div>'
            if event_dir.exists():
                jpgs = sorted([f for f in event_dir.glob("frame_*.jpg")])
                if not jpgs:
                    jpgs = sorted(event_dir.glob("*.jpg"))
                if jpgs:
                    img_data = jpgs[0].read_bytes()
                    img_b64 = b64mod.b64encode(img_data).decode("ascii")
                    img_html = f'<img src="data:image/jpeg;base64,{img_b64}" style="width:360px;height:270px;object-fit:cover;border-radius:8px;flex-shrink:0;border:1px solid #222;cursor:pointer" onclick="this.classList.toggle(\'big\')" />'

            cards_html += f'''
            <div style="background:#0d1117;border-radius:10px;margin-bottom:16px;overflow:hidden;border-left:4px solid {group_color}">
                <div style="display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid #1a1a2e;flex-wrap:wrap">
                    <span style="padding:3px 10px;border-radius:14px;font-size:10px;font-weight:800;color:#000;background:{group_color}">{group_label}</span>
                    <span style="font-weight:600">{cam}</span>
                    <span style="color:#777;font-size:12px">{time_str}</span>
                    <span style="color:#555;font-size:11px;margin-left:auto">{dur:.0f}s | {frames} frames</span>
                </div>
                <div style="padding:8px 16px;font-size:15px;font-weight:600;color:#fff">{summary[:200]}</div>
                <div style="display:flex;gap:16px;padding:12px 16px 16px">
                    {img_html}
                    <div style="flex:1;font-size:12px;color:#999;line-height:1.7">{"<br>".join(d[:300] for d in descs[:5])}</div>
                </div>
            </div>'''

            log_rows += f'<tr><td style="color:#39ff14;font-weight:600;white-space:nowrap">{time_short}</td><td>{summary[:200]}</td><td style="color:#666;font-size:12px">{cam}</td></tr>'

        total = len(events)
        report = f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta http-equiv="refresh" content="60">
<title>Camera Monitor - Live</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:'Segoe UI',sans-serif;background:#08080d;color:#ddd}}
.hero{{background:linear-gradient(135deg,#0a1628,#162447);padding:40px 50px;border-bottom:3px solid #39ff14}}
.hero h1{{font-size:28px;color:#39ff14}}.hero p{{color:#666;font-size:13px;margin-top:4px}}
.section{{padding:32px 50px;border-bottom:1px solid #151520}}
.section h2{{color:#39ff14;font-size:20px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse}}th{{background:#0d1117;color:#39ff14;padding:10px;text-align:left;font-size:11px;text-transform:uppercase}}
td{{padding:8px 10px;border-bottom:1px solid #151520;font-size:13px}}tr:hover{{background:#0d1117}}
img.big{{position:fixed!important;top:2%!important;left:2%!important;width:96%!important;height:96%!important;object-fit:contain!important;z-index:1000;background:#000;border-radius:0!important}}
</style></head><body>
<div class="hero"><h1>Camera Monitor v2 - Live Feed</h1>
<p>Auto-refreshes every 60 seconds &bull; {total} events captured &bull; Last updated: {datetime.now().strftime("%I:%M:%S %p")}</p></div>
<div class="section"><h2>Activity Log</h2>
<table><thead><tr><th>Time</th><th>Event</th><th>Camera</th></tr></thead><tbody>{log_rows}</tbody></table></div>
<div class="section"><h2>Event Details</h2>{cards_html}</div>
<script>document.addEventListener('keydown',e=>{{if(e.key==='Escape')document.querySelectorAll('.big').forEach(i=>i.classList.remove('big'))}});
document.addEventListener('click',e=>{{if(!e.target.closest('img'))document.querySelectorAll('.big').forEach(i=>i.classList.remove('big'))}});</script>
</body></html>'''

        out = BASE_DIR / "live-report.html"
        out.write_text(report, encoding="utf-8")
        logger.info(f"  Live report updated: {out}")

    def run(self):
        """Main monitoring loop."""
        logger.info("=" * 60)
        logger.info("CAMERA MONITOR v2 STARTING")
        logger.info(f"HA: {HA_URL} | Ollama: {OLLAMA_URL} | Model: {MODEL}")
        logger.info(f"Pre-filter: SSIM + pixel diff + IR detection")
        logger.info(f"Events: {EVENTS_DIR}")
        logger.info("=" * 60)

        # Verify Ollama
        try:
            resp = urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5)
            models = json.loads(resp.read())
            model_names = [m["name"] for m in models.get("models", [])]
            if not any(MODEL in m for m in model_names):
                logger.error(f"Model {MODEL} not found. Available: {model_names}")
                logger.info(f"Run: ollama pull {MODEL}")
                return
            logger.info(f"Ollama OK - {MODEL} ready")
        except Exception as e:
            logger.error(f"Cannot connect to Ollama: {e}")
            return

        # List cameras
        enabled = [s for s in self.cam_states.values() if s.profile.enabled]
        disabled = [s for s in self.cam_states.values() if not s.profile.enabled]
        logger.info(f"\nCameras: {len(enabled)} enabled, {len(disabled)} disabled")
        for s in enabled:
            logger.info(f"  [ON]  {s.profile.name} ({s.profile.group}) - {s.profile.location}")
        for s in disabled:
            logger.info(f"  [OFF] {s.profile.name} - {s.profile.location}")

        # Initial baselines
        self.update_baselines()

        # Initial scene descriptions
        logger.info("\nGetting initial scene descriptions...")
        for eid, state in self.cam_states.items():
            if state.profile.enabled and state.baseline_bytes:
                try:
                    desc = ollama_analyze(state.baseline_bytes, DESCRIBE_PROMPT)
                    state.prev_description = desc
                    state.prev_bytes = state.baseline_bytes
                    state.prev_hash = hashlib.md5(state.baseline_bytes).hexdigest()
                    state.prev_size = len(state.baseline_bytes)
                    logger.info(f"  {state.profile.name}: {desc[:200]}")
                except Exception as e:
                    logger.warning(f"  {state.profile.name}: initial analysis failed: {e}")

        self.running = True
        last_baseline = datetime.now()
        cycle = 0

        logger.info("\nMonitoring started. Scanning all cameras continuously...\n")
        logger.info(f"Normal scan interval: {SCAN_INTERVAL}s | Motion-boosted: {MOTION_BOOST_INTERVAL}s")

        try:
            while self.running:
                cycle += 1
                now = datetime.now()

                # 1. Check HA motion events - boost scan rate for triggered cameras
                triggered = self.check_motion_triggers()
                for eid in triggered:
                    state = self.cam_states[eid]
                    state.motion_boost_until = now + timedelta(seconds=MOTION_BOOST_DURATION)
                    logger.info(f"  BOOST: {state.profile.name} - fast scanning for {MOTION_BOOST_DURATION}s")

                # 2. Scan each camera based on its current interval
                for eid, state in self.cam_states.items():
                    if not state.profile.enabled:
                        continue
                    # Skip cameras in offline backoff
                    if state.offline_until and now < state.offline_until:
                        continue

                    # Determine scan interval for this camera right now
                    is_boosted = state.motion_boost_until and now < state.motion_boost_until
                    has_active_event = state.active_event is not None
                    interval = MOTION_BOOST_INTERVAL if (is_boosted or has_active_event) else SCAN_INTERVAL

                    # Check if it's time to scan this camera
                    if state.last_scan_time and (now - state.last_scan_time).total_seconds() < interval:
                        continue

                    # Time to scan
                    trigger = "ha_motion" if eid in triggered else ("boosted" if is_boosted else "scheduled")
                    result = self.process_camera(eid, trigger=trigger)
                    state.last_scan_time = now

                    # If we found a real change, do rapid follow-up captures
                    if result and result["action"] == "real_change":
                        state.motion_boost_until = now + timedelta(seconds=MOTION_BOOST_DURATION)
                        for _ in range(3):
                            time.sleep(EVENT_CAPTURE_INTERVAL)
                            sub = self.process_camera(eid, trigger="event_followup")
                            state.last_scan_time = datetime.now()
                            if sub and sub["action"] in ("filtered", "ai_none"):
                                break

                # 3. Check for events to close (scene stabilized)
                for state in self.cam_states.values():
                    if state.active_event and state.active_event.seconds_since_last_change() > STABILIZE_SECONDS:
                        self._close_event(state)

                # 4. Periodic baseline update
                if (now - last_baseline).total_seconds() > BASELINE_INTERVAL:
                    self.update_baselines()
                    last_baseline = now
                    self.tuner.save()

                # 5. Status log every 5 minutes
                if cycle % 150 == 0:
                    self._log_status()

                time.sleep(MOTION_POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("\nShutting down...")
            for state in self.cam_states.values():
                if state.active_event:
                    self._close_event(state)
            self.tuner.save()
            self._log_status()
            logger.info("Monitor stopped.")

    def _log_status(self):
        logger.info("\n--- STATUS ---")
        for eid, state in self.cam_states.items():
            if not state.profile.enabled:
                continue
            active = "EVENT ACTIVE" if state.active_event else "idle"
            adj = f" ssim_adj=+{state.ssim_adjustment:.3f}" if state.ssim_adjustment > 0 else ""
            logger.info(
                f"  {state.profile.name}: captured={state.total_captured} "
                f"filtered={state.total_filtered} ai={state.total_sent_to_ai} "
                f"real={state.total_real_events} [{active}]{adj}"
            )
        g = self.tuner.stats["global"]
        if g["total_analyzed"] > 0:
            fp_rate = g["total_none"] / g["total_analyzed"] * 100
            logger.info(f"  Global: {g['total_analyzed']} analyzed, {g['total_real']} real, {fp_rate:.0f}% filtered by AI")


# ══════════════════════════════════════════════════════════════
# PREVIEW MODE - Replay old data through new filters
# ══════════════════════════════════════════════════════════════
def run_preview():
    """Replay old monitoring data through the v2 filter pipeline to show what would have been captured."""
    OLD_SNAP_DIR = BASE_DIR.parent / "camera-monitor" / "snapshots"
    OLD_LOG = BASE_DIR.parent / "camera-monitor" / "logs" / "changes.jsonl"

    if not OLD_LOG.exists():
        logger.error(f"Old data not found at {OLD_LOG}")
        return

    logger.info("PREVIEW MODE - Replaying old data through v2 filters")
    logger.info(f"Old data: {OLD_LOG}")

    profiles, _ = load_profiles()
    entries = []
    with open(OLD_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    logger.info(f"Loaded {len(entries)} old events")

    # Build camera states
    cam_states = {}
    for eid, profile in profiles.items():
        cam_states[eid] = CameraState(profile=profile)

    results = {"total": 0, "filtered": 0, "would_analyze": 0, "cameras": defaultdict(lambda: {"filtered": 0, "passed": 0, "reasons": defaultdict(int)})}

    prev_imgs = {}

    for i, entry in enumerate(entries):
        eid = entry["camera"]
        if eid not in cam_states:
            continue

        state = cam_states[eid]
        if not state.profile.enabled:
            results["filtered"] += 1
            results["cameras"][eid]["filtered"] += 1
            results["cameras"][eid]["reasons"]["camera_disabled"] += 1
            results["total"] += 1
            continue

        # Load the snapshot image
        img_file = Path(entry.get("image_file", "")).name
        img_path = OLD_SNAP_DIR / img_file
        if not img_path.exists():
            continue

        img_bytes = img_path.read_bytes()
        results["total"] += 1

        # Run filter
        should_analyze, reason, meta = PreFilter.run(state, img_bytes)

        # Update state
        state.prev_hash = hashlib.md5(img_bytes).hexdigest()
        state.prev_size = len(img_bytes)
        if state.prev_bytes is None:
            should_analyze = True  # Always analyze first frame
            reason = "first_frame"
        state.prev_bytes = img_bytes

        cam_results = results["cameras"][eid]
        if should_analyze:
            results["would_analyze"] += 1
            cam_results["passed"] += 1
        else:
            results["filtered"] += 1
            cam_results["filtered"] += 1

        cam_results["reasons"][reason] += 1

    # Report
    logger.info("\n" + "=" * 60)
    logger.info("PREVIEW RESULTS")
    logger.info("=" * 60)
    pct_filtered = results["filtered"] / max(results["total"], 1) * 100
    logger.info(f"Total events:     {results['total']}")
    logger.info(f"Would filter:     {results['filtered']} ({pct_filtered:.0f}%)")
    logger.info(f"Would analyze:    {results['would_analyze']} ({100-pct_filtered:.0f}%)")

    for eid in sorted(results["cameras"]):
        cr = results["cameras"][eid]
        name = profiles[eid].name if eid in profiles else eid
        total_cam = cr["filtered"] + cr["passed"]
        logger.info(f"\n  {name} ({total_cam} events):")
        logger.info(f"    Filtered: {cr['filtered']}  |  Would analyze: {cr['passed']}")
        for reason, count in sorted(cr["reasons"].items(), key=lambda x: -x[1]):
            logger.info(f"      {reason}: {count}")

    # Generate preview report
    _generate_preview_report(entries, results, profiles, OLD_SNAP_DIR)


def _generate_preview_report(entries, results, profiles, snap_dir):
    """Generate an HTML preview showing what the v2 system would have captured."""
    import base64 as b64mod
    from collections import Counter

    # Find events that would have passed filters and embed a sample
    cam_states = {}
    for eid, profile in profiles.items():
        cam_states[eid] = CameraState(profile=profile)

    passed_events = []
    for entry in entries:
        eid = entry["camera"]
        if eid not in cam_states or not cam_states[eid].profile.enabled:
            continue

        state = cam_states[eid]
        img_file = Path(entry.get("image_file", "")).name
        img_path = snap_dir / img_file
        if not img_path.exists():
            continue

        img_bytes = img_path.read_bytes()
        should_analyze, reason, meta = PreFilter.run(state, img_bytes)

        state.prev_hash = hashlib.md5(img_bytes).hexdigest()
        state.prev_size = len(img_bytes)
        if state.prev_bytes is None:
            should_analyze = True
        state.prev_bytes = img_bytes

        if should_analyze:
            passed_events.append({
                "entry": entry,
                "file": img_file,
                "path": img_path,
                "meta": meta,
                "reason": reason,
            })

    logger.info(f"\n{len(passed_events)} events would have been sent to AI")

    # Embed images for the passed events
    cards_html = ""
    for i, pe in enumerate(passed_events):
        entry = pe["entry"]
        img_data = pe["path"].read_bytes()
        img_b64 = b64mod.b64encode(img_data).decode("ascii")
        dt = datetime.fromisoformat(entry["timestamp"])
        cam = entry["name"]
        group = profiles.get(entry["camera"], CameraProfile("?","?","?","?",False,[],[],0,0,0,0)).group
        ssim = pe["meta"].get("ssim", "N/A")
        diff = pe["meta"].get("diff_pct", "N/A")
        old_level = entry.get("change_level", entry.get("type", "?"))
        old_desc = entry.get("comparison", entry.get("changes_detected", entry.get("analysis", "")))[:300]

        group_color = "#4a9eff" if group == "office" else "#ff9f43"

        cards_html += f'''
        <div class="card">
            <div class="card-top">
                <span class="badge" style="background:{group_color}">{"OFFICE" if group=="office" else "HOME"}</span>
                <span class="cam">{cam}</span>
                <span class="ts">{dt.strftime("%b %d, %I:%M:%S %p")}</span>
                <span class="metrics">SSIM: {ssim} | Diff: {diff}% | Old level: {old_level}</span>
            </div>
            <div class="card-body">
                <img src="data:image/jpeg;base64,{img_b64}" />
                <div class="desc">
                    <strong>Filter result:</strong> {pe["reason"]}<br><br>
                    <strong>Old AI said:</strong> {old_desc}
                </div>
            </div>
        </div>'''

    total = results["total"]
    filtered = results["filtered"]
    analyzed = results["would_analyze"]

    html = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Camera Monitor v2 Preview</title>
<style>
* {{ margin:0;padding:0;box-sizing:border-box; }}
body {{ font-family:'Segoe UI',sans-serif; background:#0a0a0f; color:#ddd; }}
.hero {{ background:linear-gradient(135deg,#0a1628,#162447); padding:50px 60px; border-bottom:3px solid #39ff14; }}
.hero h1 {{ color:#39ff14; font-size:32px; }}
.hero .sub {{ color:#888; font-size:14px; margin-top:8px; }}
.stats {{ display:flex; background:#0d1117; }}
.stat {{ flex:1; text-align:center; padding:28px; border-right:1px solid #1a1a2e; }}
.stat:last-child {{ border:none; }}
.stat .num {{ font-size:40px; font-weight:700; }}
.stat .lbl {{ font-size:11px; color:#666; text-transform:uppercase; letter-spacing:1px; margin-top:4px; }}
.section {{ padding:40px 60px; }}
.section h2 {{ color:#39ff14; font-size:22px; margin-bottom:16px; }}
.card {{ background:#0d1117; border-radius:10px; margin-bottom:16px; overflow:hidden; border-left:3px solid #39ff14; }}
.card-top {{ padding:12px 16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; border-bottom:1px solid #1a1a2e; }}
.badge {{ padding:3px 10px; border-radius:10px; font-size:10px; font-weight:700; color:#000; }}
.cam {{ font-weight:600; }}
.ts {{ color:#888; font-size:13px; }}
.metrics {{ color:#39ff14; font-size:11px; margin-left:auto; }}
.card-body {{ display:flex; gap:16px; padding:16px; }}
.card-body img {{ width:360px; height:270px; object-fit:cover; border-radius:8px; flex-shrink:0; }}
.desc {{ flex:1; font-size:13px; color:#bbb; line-height:1.7; }}
.verdict {{ background:linear-gradient(135deg,#0a2e0a,#0d1117); border:1px solid #39ff14; border-radius:10px; padding:24px; margin:20px 0; }}
.verdict h3 {{ color:#39ff14; margin-bottom:10px; }}
.verdict p {{ color:#bbb; font-size:14px; line-height:1.7; }}
</style></head><body>
<div class="hero">
    <h1>Camera Monitor v2 - PREVIEW</h1>
    <div class="sub">What the new filter system would have captured from the old {total} events</div>
</div>
<div class="stats">
    <div class="stat"><div class="num" style="color:#ff4444">{total}</div><div class="lbl">Old Events</div></div>
    <div class="stat"><div class="num" style="color:#39ff14">{analyzed}</div><div class="lbl">v2 Would Analyze</div></div>
    <div class="stat"><div class="num" style="color:#666">{filtered}</div><div class="lbl">v2 Would Filter Out</div></div>
    <div class="stat"><div class="num" style="color:#39ff14">{filtered/max(total,1)*100:.0f}%</div><div class="lbl">Noise Eliminated</div></div>
</div>
<div class="section">
    <div class="verdict">
        <h3>v2 Filter Preview</h3>
        <p>Out of the original <strong>{total} events</strong>, the v2 pre-filter pipeline would have sent only
        <strong style="color:#39ff14">{analyzed} frames</strong> to AI for analysis - eliminating
        <strong style="color:#ff4444">{filtered/max(total,1)*100:.0f}%</strong> of the noise before it ever
        reaches Ollama. That means the AI can focus entirely on real changes: vehicles, people, animals,
        and weather - not shadows, IR switches, and identical frames.</p>
    </div>
    <h2>Events That Would Have Been Analyzed ({analyzed})</h2>
    {cards_html}
</div>
</body></html>'''

    out = BASE_DIR / "preview-report.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"Preview report: {out}")


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Camera Monitor v2")
    parser.add_argument("--preview", action="store_true", help="Preview mode: replay old data through filters")
    parser.add_argument("--test-cameras", action="store_true", help="Test camera connectivity")
    args = parser.parse_args()

    if args.preview:
        run_preview()
    elif args.test_cameras:
        profiles, _ = load_profiles()
        for eid, p in profiles.items():
            img = grab_snapshot(eid)
            status = f"OK ({len(img):,} bytes)" if img else "FAILED"
            en = "ENABLED" if p.enabled else "DISABLED"
            print(f"  [{en}] {p.name}: {status}")
    else:
        monitor = CameraMonitor()
        monitor.run()


if __name__ == "__main__":
    main()
