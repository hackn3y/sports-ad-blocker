"""main.py

Live Broadcast & Streaming Ad Blocker for Windows 11.
Supports two detection architectures:
1. Sports Mode (Default): Scoreboard, Broadcast Bug/Watermark, and football field cues.
   - Reliable live-game indicators present -> Live Game Action.
   - Live-game indicators absent for the configured debounce -> Commercial Break.
   - Instant recovery: As soon as live indicators return -> Unblack and unmute immediately!
   - Football field coverage and trusted bugs bridge scoreboard dropouts during replays.
2. Ad Badge Mode: Traditional on-screen 'Ad' / 'Commercial' badge matching for on-demand shows.
"""

import argparse
import atexit
import ctypes
import glob
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import cv2
import mss
import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication

from audio_manager import AudioManager
from overlay import BlackoutOverlay, CalibrationOverlay, find_screen_by_mss_bounds

# Configure formatted logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("HuluBlocker")


class _SuppressEmptyOCRWarning(logging.Filter):
  """Hide RapidOCR's expected warning when a scanned band contains no text."""

  def filter(self, record: logging.LogRecord) -> bool:
    return "text detection result is empty" not in record.getMessage().lower()


def install_qt_interrupt_handling(quit_callback) -> Tuple[QTimer, Any]:
  """Make Ctrl+C stop the app while Qt's C++ event loop is blocking the main thread.

  On Windows, SIGINT is not delivered during QApplication.exec() unless Python
  runs periodically. A console Ctrl handler plus a short QTimer fixes that.
  """
  interrupt_flag = threading.Event()
  armed = {"done": False}

  def request_quit() -> None:
    interrupt_flag.set()

  def pump_check() -> None:
    if not interrupt_flag.is_set() or armed["done"]:
      return
    armed["done"] = True
    print("\n[+] Interrupted by user. Shutting down cleanly...", flush=True)
    try:
      quit_callback()
    except Exception:
      pass

  def _sig_handler(sig, frame):
    request_quit()

  try:
    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
  except Exception:
    pass

  handler_ref = None
  try:
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def _ctrl_handler(ctrl_type: int) -> int:
      # 0=CTRL_C_EVENT, 1=CTRL_BREAK_EVENT
      if ctrl_type in (0, 1):
        request_quit()
        return 1
      return 0

    handler_ref = handler_type(_ctrl_handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler_ref, True)
  except Exception:
    handler_ref = None

  pump = QTimer()
  pump.setInterval(100)
  pump.timeout.connect(pump_check)
  pump.start()
  return pump, handler_ref


def attach_default_desktop() -> bool:
  """Ensure the current thread is attached to the interactive WinSta0\\Default desktop.

  Crucial for screen grab and window operations on Windows 11.
  """
  try:
    import win32con  # type: ignore
    import win32service  # type: ignore

    hdesk = win32service.OpenDesktop(
        "Default", 0, False, win32con.DESKTOP_SWITCHDESKTOP | win32con.GENERIC_ALL
    )
    if hdesk:
      ctypes.windll.user32.SetThreadDesktop(int(hdesk))
      return True
  except Exception:
    pass
  return False


TEMPLATE_SCALES = (0.50, 0.60, 0.70, 0.80, 0.90, 1.00, 1.10, 1.20)
FOOTBALL_CLOCK_PATTERN = re.compile(r"(?<!\d)(?:\d{1,2}\s*)?:\s*[0-5]\d(?!\d)")
FOOTBALL_PERIOD_PATTERN = re.compile(
    r"\b(?:Q[1-4]|[1-4](?:ST|ND|RD|TH)|[1-4]\s*Q|OT|OVERTIME)\b"
)
FOOTBALL_DOWN_PATTERN = re.compile(
    r"\b[1-4](?:ST|ND|RD|TH)?\s*(?:&|AND)\s*(?:GOAL|\d{1,2})\b"
)
FOOTBALL_HALFTIME_PATTERN = re.compile(
    r"\b(?:COUNTDOWN\s+TO\s+(?:THE\s+)?(?:2ND|SECOND)\s+HALF|"
    r"(?:2ND|SECOND)\s+HALF|HALF[\s-]?TIME)\b"
)


def football_scoreboard_ocr_is_halftime(texts: List[str]) -> bool:
  """Detect halftime countdown graphics that can resemble an active game clock."""
  if not texts:
    return False
  normalized = re.sub(r"[^A-Z0-9:&]", " ", " ".join(texts).upper())
  return bool(FOOTBALL_HALFTIME_PATTERN.search(normalized))


def football_scoreboard_ocr_score(texts: List[str]) -> float:
  """Score OCR text that contains football game-clock and period/down cues."""
  if not texts:
    return 0.0
  normalized = re.sub(r"[^A-Z0-9:&]", " ", " ".join(texts).upper())
  if FOOTBALL_HALFTIME_PATTERN.search(normalized):
    return 0.0
  if not FOOTBALL_CLOCK_PATTERN.search(normalized):
    return 0.0
  has_period = bool(FOOTBALL_PERIOD_PATTERN.search(normalized))
  has_down = bool(FOOTBALL_DOWN_PATTERN.search(normalized))
  return 0.8 if has_period or has_down else 0.0


def football_field_green_metrics(frame_bgr: np.ndarray) -> Tuple[float, int]:
  """Measure green coverage and how broadly it is distributed across the frame."""
  if frame_bgr.size == 0:
    return 0.0, 0

  sample = cv2.resize(frame_bgr, (320, 180), interpolation=cv2.INTER_AREA)
  hsv = cv2.cvtColor(sample, cv2.COLOR_BGR2HSV)
  green_mask = cv2.inRange(
      hsv, np.array([25, 35, 30]), np.array([100, 255, 255])
  )
  green = green_mask > 0
  green_ratio = float(np.mean(green))

  green_tiles = 0
  for row in np.array_split(green, 3, axis=0):
    for tile in np.array_split(row, 4, axis=1):
      if float(np.mean(tile)) >= 0.15:
        green_tiles += 1

  return green_ratio, green_tiles


def _process_executable(pid: int) -> str:
  """Return the executable path for a PID, or empty string."""
  PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
  handle = ctypes.windll.kernel32.OpenProcess(
      PROCESS_QUERY_LIMITED_INFORMATION, False, pid
  )
  if not handle:
    return ""
  try:
    buf = ctypes.create_unicode_buffer(32768)
    size = ctypes.c_uint(32768)
    ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
        handle, 0, buf, ctypes.byref(size)
    )
    return buf.value if ok else ""
  finally:
    ctypes.windll.kernel32.CloseHandle(handle)


def find_playback_rect(
    monitor_bounds: Dict[str, int],
    browser_executables: Optional[List[str]] = None,
    windowed_top_inset_px: Optional[int] = None,
) -> Dict[str, int]:
  """Locate the video playback area on the target monitor.

  Fullscreen: the whole monitor. Windowed browser: the client area, with a top
  inset for tabs/address bar so calibrated overlays map onto the video, not chrome.
  Coordinates are relative to the monitor origin.
  """
  mon_left = int(monitor_bounds["left"])
  mon_top = int(monitor_bounds["top"])
  mon_w = int(monitor_bounds["width"])
  mon_h = int(monitor_bounds["height"])
  fallback = {"left": 0, "top": 0, "width": mon_w, "height": mon_h}
  names = {
      os.path.basename(n).lower()
      for n in (browser_executables or ["chrome.exe", "brave.exe", "msedge.exe"])
  }

  try:
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore
  except Exception:
    return fallback

  best: Optional[Tuple[int, Dict[str, int]]] = None

  def _callback(hwnd: int, _: Any) -> bool:
    nonlocal best
    try:
      if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
        return True
      if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
        return True
      if not win32gui.GetWindowText(hwnd):
        return True
      _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
      exe = os.path.basename(_process_executable(pid)).lower()
      if exe not in names:
        return True

      left, top, right, bottom = win32gui.GetWindowRect(hwnd)
      vis_w = min(right, mon_left + mon_w) - max(left, mon_left)
      vis_h = min(bottom, mon_top + mon_h) - max(top, mon_top)
      if vis_w < 400 or vis_h < 300:
        return True

      try:
        _cl, _ct, cw, ch = win32gui.GetClientRect(hwnd)
        c_left, c_top = win32gui.ClientToScreen(hwnd, (0, 0))
      except Exception:
        c_left, c_top, cw, ch = left, top, right - left, bottom - top

      cl = max(c_left, mon_left)
      ct = max(c_top, mon_top)
      cr = min(c_left + cw, mon_left + mon_w)
      cb = min(c_top + ch, mon_top + mon_h)
      pw, ph = cr - cl, cb - ct
      if pw < 400 or ph < 300:
        return True

      fullscreen = pw >= mon_w * 0.92 and ph >= mon_h * 0.92
      if fullscreen:
        top_inset = 0
      elif windowed_top_inset_px is None:
        top_inset = min(150, int(ph * 0.14))
      else:
        top_inset = min(max(0, int(windowed_top_inset_px)), max(0, ph - 300))
      use_h = ph - top_inset
      use_t = ct + top_inset
      if use_h < 300:
        use_t, use_h = ct, ph

      area = pw * use_h
      rel = {
          "left": cl - mon_left,
          "top": use_t - mon_top,
          "width": pw,
          "height": use_h,
      }
      if best is None or area > best[0]:
        best = (area, rel)
    except Exception:
      return True
    return True

  try:
    win32gui.EnumWindows(_callback, None)
  except Exception:
    return fallback
  return best[1] if best else fallback


def collect_template_paths(primary: str, extras: Optional[List[str]], pattern: str) -> List[str]:
  """Unique existing template files: primary, extras, then glob matches."""
  found: List[str] = []
  seen = set()
  candidates = [primary]
  if extras:
    candidates.extend(extras)
  candidates.extend(sorted(glob.glob(pattern)))
  for path in candidates:
    if not path:
      continue
    norm = os.path.normpath(path)
    key = norm.lower()
    if key in seen or not os.path.isfile(norm):
      continue
    seen.add(key)
    found.append(norm)
  return found


def build_scaled_templates(tmpl: np.ndarray) -> List[Tuple[float, np.ndarray]]:
  """Pre-compute a scale pyramid so windowed (smaller) video still matches."""
  scaled: List[Tuple[float, np.ndarray]] = []
  th, tw = tmpl.shape[:2]
  for s in TEMPLATE_SCALES:
    sw, sh = max(8, int(tw * s)), max(8, int(th * s))
    if sw < 8 or sh < 8:
      continue
    interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR
    scaled.append((s, cv2.resize(tmpl, (sw, sh), interpolation=interp)))
  return scaled


def map_roi_into_playback(
    roi: dict,
    playback: dict,
    calibrated_frame: dict,
    monitor_bounds: Dict[str, int],
) -> dict:
  """Map a fullscreen-calibrated ROI into the current playback rectangle."""
  mon_w = int(monitor_bounds["width"])
  mon_h = int(monitor_bounds["height"])
  is_full = (
      playback.get("left", 0) <= 8
      and playback.get("top", 0) <= 8
      and playback.get("width", 0) >= mon_w * 0.92
      and playback.get("height", 0) >= mon_h * 0.92
  )
  if is_full:
    return {
        "left": int(roi.get("left", 0)),
        "top": int(roi.get("top", 0)),
        "width": int(roi.get("width", 100)),
        "height": int(roi.get("height", 100)),
    }

  cal_w = max(1, int(calibrated_frame.get("width") or mon_w))
  cal_h = max(1, int(calibrated_frame.get("height") or mon_h))
  return {
      "left": int(playback["left"] + (roi.get("left", 0) / cal_w) * playback["width"]),
      "top": int(playback["top"] + (roi.get("top", 0) / cal_h) * playback["height"]),
      "width": max(20, int((roi.get("width", 100) / cal_w) * playback["width"])),
      "height": max(20, int((roi.get("height", 100) / cal_h) * playback["height"])),
  }


class SportProfile:
  """Configuration and pre-computed template scales for a specific sport."""

  def __init__(self, key: str, data: Dict[str, Any]):
    self.key = key.lower().strip()
    self.name = data.get("name", key.capitalize())
    self.scoreboard_roi = data.get(
        "scoreboard_roi", {"left": 0, "top": 0, "width": 100, "height": 100}
    )
    self.scoreboard_template_path = (
        data.get("scoreboard_template_path")
        or data.get("template_path", f"templates/{key}_scoreboard.png")
    )
    self.match_threshold = float(data.get("match_threshold", 0.60))
    self.bug_enabled = data.get("broadcast_bug_enabled", False)
    self.bug_roi = data.get(
        "broadcast_bug_roi", {"left": 0, "top": 0, "width": 100, "height": 100}
    )
    self.bug_template_path = (
        data.get("broadcast_bug_template_path")
        or f"templates/{key}_bug.png"
    )
    self.bug_threshold = float(data.get("broadcast_bug_threshold", 0.60))
    self.calibrated_frame = data.get("calibrated_frame") or {}
    self.extra_bug_paths = list(data.get("broadcast_bug_extra_templates") or [])
    self.extra_sb_paths = list(data.get("scoreboard_extra_templates") or [])
    self.scoreboard_ocr_enabled = bool(data.get("scoreboard_ocr_enabled", False))

    self.sb_template_img: Optional[np.ndarray] = None
    self.sb_scaled_templates: List[Tuple[float, np.ndarray]] = []
    self.bug_template_img: Optional[np.ndarray] = None
    self.bug_scaled_templates: List[Tuple[float, np.ndarray]] = []

  def load_templates(self) -> None:
    """Pre-load every matching template (including extras) and scale pyramids."""
    sb_paths = collect_template_paths(
        self.scoreboard_template_path,
        self.extra_sb_paths,
        os.path.join("templates", f"{self.key}_scoreboard*.png"),
    )
    self.sb_scaled_templates = []
    for path in sb_paths:
      tmpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
      if tmpl is None:
        logger.warning("[%s] Failed to decode scoreboard image '%s'", self.name, path)
        continue
      if self.sb_template_img is None:
        self.sb_template_img = tmpl
      self.sb_scaled_templates.extend(build_scaled_templates(tmpl))
      th, tw = tmpl.shape[:2]
      logger.info("[%s] Loaded Scoreboard template: '%s' (%dx%d)", self.name, path, tw, th)

    if not self.bug_enabled:
      return

    bug_paths = collect_template_paths(
        self.bug_template_path,
        self.extra_bug_paths,
        os.path.join("templates", f"{self.key}_bug*.png"),
    )
    self.bug_scaled_templates = []
    for path in bug_paths:
      tmpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
      if tmpl is None:
        logger.warning("[%s] Failed to decode bug image '%s'", self.name, path)
        continue
      if self.bug_template_img is None:
        self.bug_template_img = tmpl
      self.bug_scaled_templates.extend(build_scaled_templates(tmpl))
      th, tw = tmpl.shape[:2]
      logger.info("[%s] Loaded Broadcast Bug template: '%s' (%dx%d)", self.name, path, tw, th)


class DetectionEngine(QObject):
  """Background detection loop running Sports Scoreboard or Ad Badge analysis at ~10 FPS."""

  sig_trigger_blackout = pyqtSignal(bool)  # True = show blackout, False = hide
  sig_sport_changed = pyqtSignal(str)     # Emitted when sport profile switches

  def __init__(
      self,
      config: Dict[str, Any],
      audio_mgr: AudioManager,
      monitor_bounds: Dict[str, int],
      mode_override: Optional[str] = None,
      sport_override: Optional[str] = None,
  ):
    super().__init__()
    self.config = config
    self.audio_mgr = audio_mgr
    self.monitor_bounds = monitor_bounds
    self.sport_override = sport_override

    self._stop_event = threading.Event()
    self._thread: Optional[threading.Thread] = None
    self.is_commercial = False
    self.commercial_start_time: Optional[float] = None

    # Mode: "sports" or "badge"
    self.mode = mode_override or self.config.get("detection_mode", "sports")

    capture_cfg = self.config.get("capture", {})
    self.target_fps = max(5, min(30, capture_cfg.get("fps", 10)))
    self.target_frame_time = 1.0 / self.target_fps

    debounce_cfg = self.config.get("debounce", {})
    self.max_commercial_seconds = float(
        debounce_cfg.get("max_commercial_seconds", 240.0)
    )

    logging_cfg = self.config.get("logging", {})
    self.heartbeat_interval = float(
        logging_cfg.get("heartbeat_interval_sec", 1.5)
    )

    if self.mode == "sports":
      self._init_sports_mode()
    else:
      self._init_badge_mode()

  def _init_sports_mode(self) -> None:
    """Initialize sports profiles and live-game signal settings."""
    sports_cfg = self.config.get("sports_detection", {})
    self.search_padding = int(sports_cfg.get("search_padding", 50))
    # Wait briefly for transient anchor dropouts before blacking out.
    self.disappear_debounce_seconds = float(
        sports_cfg.get("disappear_debounce_seconds", 0.1)
    )
    self.auto_switch_confirm_frames = max(
        1, int(sports_cfg.get("auto_switch_confirm_frames", 3))
    )
    configured_top_inset = sports_cfg.get("windowed_top_inset_px")
    self.windowed_top_inset_px = (
        int(configured_top_inset) if configured_top_inset is not None else None
    )
    self.reappear_debounce_frames = int(
        sports_cfg.get("reappear_debounce_frames", 1)
    )
    self.scoreboard_fast_recovery_threshold = float(
        sports_cfg.get("scoreboard_fast_recovery_threshold", 0.85)
    )
    self.bug_only_grace_seconds = max(
        0.0, float(sports_cfg.get("bug_only_grace_seconds", 5.0))
    )
    self.bug_hold_threshold = float(
        sports_cfg.get("bug_hold_threshold", 0.55)
    )
    self.bug_strong_hold_threshold = float(
        sports_cfg.get("bug_strong_hold_threshold", 0.85)
    )
    self.bug_miss_grace_seconds = max(
        0.0, float(sports_cfg.get("bug_miss_grace_seconds", 0.5))
    )
    self.replay_scoreboard_threshold = float(
        sports_cfg.get("replay_scoreboard_threshold", 0.90)
    )
    self.replay_bug_threshold = float(
        sports_cfg.get("replay_bug_threshold", 0.60)
    )
    self.football_field_min_green_ratio = float(
        sports_cfg.get("football_field_min_green_ratio", 0.18)
    )
    self.football_field_grace_seconds = max(
        0.0, float(sports_cfg.get("football_field_grace_seconds", 1.0))
    )
    self.football_field_sample_interval = max(
        0.1, float(sports_cfg.get("football_field_sample_interval_seconds", 0.25))
    )

    self.live_lost_start: Optional[float] = None
    self.last_scoreboard_seen = time.monotonic()
    self.last_football_scoreboard_seen: Optional[float] = None
    self.last_strong_scoreboard_seen: Optional[float] = None
    self.last_bug_seen: Optional[float] = None
    self.bug_anchor_confirmed = False
    self.consecutive_live_frames = 0
    self.auto_switch_candidate: Optional[str] = None
    self.auto_switch_candidate_frames = 0
    self.browser_executables = list(
        self.config.get("target", {}).get(
            "browser_executables", ["chrome.exe", "brave.exe", "msedge.exe"]
        )
    )
    self._playback_rect: Dict[str, int] = {
        "left": 0,
        "top": 0,
        "width": int(self.monitor_bounds["width"]),
        "height": int(self.monitor_bounds["height"]),
    }
    self._playback_rect_ts = 0.0
    self.scoreboard_ocr_interval = max(
        0.1, float(sports_cfg.get("scoreboard_ocr_interval_seconds", 0.2))
    )
    self.football_halftime_ocr_interval = max(
        0.5, float(sports_cfg.get("football_halftime_ocr_interval_seconds", 1.0))
    )
    self._scoreboard_ocr_engine: Optional[Any] = None
    self._scoreboard_ocr_last_scan = 0.0
    self._scoreboard_ocr_cached_score = 0.0
    self._scoreboard_ocr_disabled = False
    self._football_halftime_last_scan = 0.0
    self._football_halftime_cached = False
    self._football_field_last_scan = 0.0
    self._football_field_cached_metrics = (0.0, 0)

    # Build profiles dictionary
    self.sports_profiles: Dict[str, SportProfile] = {}
    profiles_dict = self.config.get("sports_profiles", {})
    for k, pdata in profiles_dict.items():
      sp = SportProfile(k, pdata)
      sp.load_templates()
      self.sports_profiles[k] = sp

    if any(profile.scoreboard_ocr_enabled for profile in self.sports_profiles.values()):
      try:
        from rapidocr import RapidOCR
      except ImportError as exc:
        raise RuntimeError(
            "Football scoreboard OCR is enabled, but RapidOCR is not installed. "
            "Install the dependencies from requirements.txt."
        ) from exc
      logging.getLogger("RapidOCR").addFilter(_SuppressEmptyOCRWarning())
      self._scoreboard_ocr_engine = RapidOCR()
      logger.info(
          "Football scoreboard OCR enabled (scan interval %.2fs)",
          self.scoreboard_ocr_interval,
      )

    # Ensure baseball exists from top-level config if missing
    if "baseball" not in self.sports_profiles and "template_path" in sports_cfg:
      sp = SportProfile("baseball", sports_cfg)
      sp.load_templates()
      self.sports_profiles["baseball"] = sp

    # Active sport selection
    configured_sport = (
        self.sport_override
        or self.config.get("active_sport", "auto")
    ).lower().strip()

    self.auto_sport = (configured_sport == "auto")
    if not self.auto_sport and configured_sport in self.sports_profiles:
      self.current_sport: Optional[SportProfile] = self.sports_profiles[configured_sport]
    else:
      # In auto mode, find first profile with a valid loaded template
      self.current_sport = self.sports_profiles.get("baseball") or next(
          (p for p in self.sports_profiles.values() if p.sb_template_img is not None),
          next(iter(self.sports_profiles.values()), None)
      )

    active_name = self.current_sport.name if self.current_sport else "None"
    mode_label = "AUTO-SWITCH" if self.auto_sport else "LOCKED"
    logger.info("Sports Mode initialized: Initial Sport='%s' (%s)", active_name, mode_label)

  def _init_badge_mode(self) -> None:
    """Initialize Hulu Ad Badge mode."""
    vis_cfg = self.config.get("visual_detection", {})
    self.badge_roi = vis_cfg.get(
        "badge_roi", {"left": 30, "top": 30, "width": 380, "height": 140}
    )
    self.template_path = vis_cfg.get("template_path", "templates/ad_badge.png")
    self.template_threshold = float(vis_cfg.get("template_threshold", 0.75))

    debounce_cfg = self.config.get("debounce", {})
    self.hysteresis_seconds = float(debounce_cfg.get("hysteresis_seconds", 1.5))
    self.min_trigger_frames = int(debounce_cfg.get("min_trigger_frames", 2))

    self.consecutive_ad_frames = 0
    self.live_candidate_start: Optional[float] = None

    self.badge_template_img: Optional[np.ndarray] = None
    self.badge_scaled_templates: List[Tuple[float, np.ndarray]] = []
    self._load_badge_template()

  def _load_badge_template(self) -> None:
    """Load ad badge template and pre-compute multi-scale variants."""
    if os.path.exists(self.template_path):
      tmpl = cv2.imread(self.template_path, cv2.IMREAD_GRAYSCALE)
      if tmpl is not None:
        self.badge_template_img = tmpl
        self.badge_scaled_templates = []
        th, tw = tmpl.shape[:2]
        for s in [0.7, 0.85, 1.0, 1.15, 1.3]:
          sw, sh = max(10, int(tw * s)), max(10, int(th * s))
          scaled_img = cv2.resize(
              tmpl, (sw, sh),
              interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR
          )
          self.badge_scaled_templates.append((s, scaled_img))
        logger.info(
            "Loaded Ad Badge template from '%s' (%dx%d)",
            self.template_path, tw, th
        )

  def _match_template_in_region(
      self,
      search_bgr: np.ndarray,
      templates: List[Tuple[float, np.ndarray]],
      threshold: float,
  ) -> Tuple[bool, float]:
    """Generic multi-scale template matching against a captured region."""
    if not templates:
      return False, 0.0

    gray_search = cv2.cvtColor(search_bgr, cv2.COLOR_BGR2GRAY)
    sh, sw = gray_search.shape[:2]
    best_score = 0.0

    for _, tmpl in templates:
      th, tw = tmpl.shape[:2]
      if sh < th or sw < tw:
        continue
      res = cv2.matchTemplate(gray_search, tmpl, cv2.TM_CCOEFF_NORMED)
      _, max_val, _, _ = cv2.minMaxLoc(res)
      if max_val > best_score:
        best_score = float(max_val)

    return (best_score >= threshold), best_score

  def check_visual_badge(self, roi_bgr: np.ndarray) -> Tuple[bool, float]:
    """Check if Hulu's 'Ad' badge icon is present in the ROI."""
    return self._match_template_in_region(
        roi_bgr, self.badge_scaled_templates, self.template_threshold
    )

  def start(self) -> None:
    """Start background detection loop on a dedicated thread."""
    if self._thread is not None and self._thread.is_alive():
      return
    self._stop_event.clear()
    target_fn = (
        self._run_sports_loop if self.mode == "sports" else self._run_badge_loop
    )
    self._thread = threading.Thread(
        target=target_fn, name="DetectionThread", daemon=True
    )
    self._thread.start()

  def stop(self) -> None:
    """Signal background thread to terminate cleanly."""
    self._stop_event.set()
    if self._thread and self._thread.is_alive():
      self._thread.join(timeout=2.0)

  def _make_grab_box(self, roi: dict, pad: int = 0) -> dict:
    """Compute screen grab box coordinates with bounds clamping."""
    mon_left = self.monitor_bounds["left"]
    mon_top = self.monitor_bounds["top"]
    mon_w = self.monitor_bounds["width"]
    mon_h = self.monitor_bounds["height"]

    rx = mon_left + roi.get("left", 0)
    ry = mon_top + roi.get("top", 0)
    rw = max(20, roi.get("width", 100))
    rh = max(20, roi.get("height", 100))

    gx = max(mon_left, rx - pad)
    gy = max(mon_top, ry - pad)
    gw = min(mon_left + mon_w - gx, rw + 2 * pad)
    gh = min(mon_top + mon_h - gy, rh + 2 * pad)
    return {"left": gx, "top": gy, "width": max(20, gw), "height": max(20, gh)}

  def _refresh_playback_rect(self) -> Dict[str, int]:
    now = time.time()
    if now - self._playback_rect_ts < 1.0:
      return self._playback_rect
    self._playback_rect = find_playback_rect(
        self.monitor_bounds,
        self.browser_executables,
        self.windowed_top_inset_px,
    )
    self._playback_rect_ts = now
    return self._playback_rect

  def _adaptive_pad(self, playback: dict) -> int:
    return max(self.search_padding, int(min(playback["width"], playback["height"]) * 0.12))

  def _grab_match(
      self,
      sct: Any,
      roi: dict,
      pad: int,
      templates: List[Tuple[float, np.ndarray]],
      threshold: float,
  ) -> Tuple[bool, float]:
    box = self._make_grab_box(roi, pad)
    try:
      raw = sct.grab(box)
      img = np.frombuffer(raw.raw, dtype=np.uint8).reshape(
          (raw.height, raw.width, 4)
      )
      frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
      return self._match_template_in_region(frame, templates, threshold)
    except Exception as exc:
      logger.debug("Grab/match error: %s", exc)
      return False, 0.0

  def _match_football_scoreboard_ocr(
      self, sct: Any, playback: Dict[str, int]
  ) -> Tuple[bool, float]:
    """Recognize football clock plus period/down text in common scorebug bands."""
    if self._scoreboard_ocr_disabled or self._scoreboard_ocr_engine is None:
      return False, 0.0

    now = time.monotonic()
    if now - self._scoreboard_ocr_last_scan < self.scoreboard_ocr_interval:
      score = self._scoreboard_ocr_cached_score
      return score > 0.0, score

    height = playback["height"]
    bands = (
        {
            "left": playback["left"],
            "top": playback["top"],
            "width": playback["width"],
            "height": max(100, int(height * 0.26)),
        },
        {
            "left": playback["left"],
            "top": playback["top"] + int(height * 0.66),
            "width": playback["width"],
            "height": max(100, int(height * 0.34)),
        },
    )
    best_score = 0.0
    for band in bands:
      box = self._make_grab_box(band)
      try:
        raw = sct.grab(box)
      except Exception:
        logger.exception("Football scoreboard OCR screen capture failed.")
        self._scoreboard_ocr_cached_score = 0.0
        self._scoreboard_ocr_last_scan = time.monotonic()
        return False, 0.0

      bgra = np.frombuffer(raw.raw, dtype=np.uint8).reshape(
          (raw.height, raw.width, 4)
      )
      image = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
      if image.shape[1] > 1920:
        scale = 1920 / image.shape[1]
        image = cv2.resize(
            image,
            (1920, max(1, int(image.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
      try:
        result = self._scoreboard_ocr_engine(image)
      except Exception:
        self._scoreboard_ocr_disabled = True
        logger.exception(
            "Football scoreboard OCR inference failed; disabling OCR and retaining template detection."
        )
        self._scoreboard_ocr_cached_score = 0.0
        self._scoreboard_ocr_last_scan = time.monotonic()
        return False, 0.0
      texts = list(getattr(result, "txts", None) or [])
      if football_scoreboard_ocr_is_halftime(texts):
        self._scoreboard_ocr_cached_score = 0.0
        self._scoreboard_ocr_last_scan = time.monotonic()
        return False, 0.0
      candidate_score = football_scoreboard_ocr_score(texts)
      best_score = max(best_score, candidate_score)
      if best_score > 0:
        break

    self._scoreboard_ocr_cached_score = best_score
    self._scoreboard_ocr_last_scan = time.monotonic()
    return best_score > 0.0, best_score

  def _match_football_halftime_countdown(
      self, sct: Any, scoreboard_roi: Dict[str, int]
  ) -> bool:
    """Periodically check scoreboard OCR for a halftime countdown banner."""
    if self._scoreboard_ocr_disabled or self._scoreboard_ocr_engine is None:
      return False

    now = time.monotonic()
    if now - self._football_halftime_last_scan < self.football_halftime_ocr_interval:
      return self._football_halftime_cached

    try:
      raw = sct.grab(self._make_grab_box(scoreboard_roi))
    except Exception:
      logger.exception("Football halftime scoreboard capture failed.")
      self._football_halftime_cached = False
      self._football_halftime_last_scan = time.monotonic()
      return False

    bgra = np.frombuffer(raw.raw, dtype=np.uint8).reshape(
        (raw.height, raw.width, 4)
    )
    image = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
    try:
      result = self._scoreboard_ocr_engine(image)
    except Exception:
      self._scoreboard_ocr_disabled = True
      logger.exception("Football halftime OCR failed; disabling OCR.")
      self._football_halftime_cached = False
      self._football_halftime_last_scan = time.monotonic()
      return False

    texts = list(getattr(result, "txts", None) or [])
    self._football_halftime_cached = football_scoreboard_ocr_is_halftime(texts)
    self._football_halftime_last_scan = time.monotonic()
    return self._football_halftime_cached

  def _sample_football_field(
      self, sct: Any, playback: Dict[str, int]
  ) -> Tuple[float, int]:
    """Sample broad field-color coverage at a capped rate."""
    now = time.monotonic()
    if now - self._football_field_last_scan < self.football_field_sample_interval:
      return self._football_field_cached_metrics

    try:
      raw = sct.grab(self._make_grab_box(playback))
    except Exception:
      logger.exception("Football field screen capture failed.")
      self._football_field_cached_metrics = (0.0, 0)
      self._football_field_last_scan = time.monotonic()
      return self._football_field_cached_metrics

    bgra = np.frombuffer(raw.raw, dtype=np.uint8).reshape(
        (raw.height, raw.width, 4)
    )
    frame = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
    self._football_field_cached_metrics = football_field_green_metrics(frame)
    self._football_field_last_scan = time.monotonic()
    return self._football_field_cached_metrics

  def _match_profile_anchors(
      self, sct: Any, profile: "SportProfile"
  ) -> Tuple[bool, float, bool, float]:
    """Match scoreboard + bug, remapped into the current (possibly windowed) video."""
    playback = self._refresh_playback_rect()
    cal = profile.calibrated_frame or {
        "width": self.monitor_bounds["width"],
        "height": self.monitor_bounds["height"],
    }
    pad = self._adaptive_pad(playback)
    sb_matched, sb_score = False, 0.0
    bug_matched, bug_score = False, 0.0

    if profile.sb_scaled_templates:
      mapped_sb = map_roi_into_playback(
          profile.scoreboard_roi, playback, cal, self.monitor_bounds
      )
      sb_matched, sb_score = self._grab_match(
          sct, mapped_sb, pad, profile.sb_scaled_templates, profile.match_threshold
      )
      scoreboard_center_y = (
          profile.scoreboard_roi.get("top", 0)
          + profile.scoreboard_roi.get("height", 0) / 2
      )
      calibrated_height = int(
          cal.get("height") or self.monitor_bounds["height"]
      )
      if not sb_matched and scoreboard_center_y >= calibrated_height * 0.5:
        band = {
            "left": playback["left"],
            "top": playback["top"] + int(playback["height"] * 0.52),
            "width": playback["width"],
            "height": int(playback["height"] * 0.48),
        }
        sb_matched, sb_score = self._grab_match(
            sct, band, 0, profile.sb_scaled_templates, profile.match_threshold
        )
    if not sb_matched and profile.scoreboard_ocr_enabled:
      sb_matched, sb_score = self._match_football_scoreboard_ocr(sct, playback)

    football_halftime_countdown = False
    if profile.key == "football" and profile.scoreboard_ocr_enabled:
      mapped_scoreboard = map_roi_into_playback(
          profile.scoreboard_roi, playback, cal, self.monitor_bounds
      )
      football_halftime_countdown = self._match_football_halftime_countdown(
          sct, mapped_scoreboard
      )
      if football_halftime_countdown:
        sb_matched, sb_score = False, 0.0

    if profile.bug_enabled and profile.bug_scaled_templates:
      mapped_bug = map_roi_into_playback(
          profile.bug_roi, playback, cal, self.monitor_bounds
      )
      bug_pad = max(12, int(min(playback["width"], playback["height"]) * 0.025))
      bug_matched, bug_score = self._grab_match(
          sct, mapped_bug, bug_pad, profile.bug_scaled_templates, profile.bug_threshold
      )

    return sb_matched, sb_score, bug_matched, bug_score

  def _run_sports_loop(self) -> None:
    """Run sports detection and multi-profile auto-switching."""
    attach_default_desktop()

    active_name = self.current_sport.name if self.current_sport else "None"
    mode_label = "AUTO-SWITCH" if self.auto_sport else "LOCKED"
    logger.info("SPORTS KEEP-ALIVE LOOP STARTED at ~%d FPS [%s: %s]", self.target_fps, mode_label, active_name)
    logger.info(
        "  Commercial blackout delay: %.1fs",
        self.disappear_debounce_seconds,
    )

    last_heartbeat = time.time()
    frames_counted = 0
    fps_timer = time.time()
    current_fps = float(self.target_fps)

    with mss.MSS() as sct:
      while not self._stop_event.is_set():
        t_start = time.perf_counter()

        profile = self.current_sport
        sb_matched, sb_score = False, 0.0
        bug_matched, bug_score = False, 0.0

        if profile is not None:
          sb_matched, sb_score, bug_matched, bug_score = self._match_profile_anchors(
              sct, profile
          )

        anchor_check_time = time.monotonic()
        if sb_matched:
          self.last_scoreboard_seen = anchor_check_time
          if profile is not None and profile.key == "football":
            self.last_football_scoreboard_seen = anchor_check_time
        if sb_score >= self.replay_scoreboard_threshold:
          self.last_strong_scoreboard_seen = anchor_check_time
        if bug_matched:
          self.last_bug_seen = anchor_check_time
          self.bug_anchor_confirmed = True

        field_green_ratio, field_green_tiles = 0.0, 0
        if profile is not None and profile.key == "football" and not self.is_commercial:
          playback = self._refresh_playback_rect()
          field_green_ratio, field_green_tiles = self._sample_football_field(
              sct, playback
          )
        football_halftime_countdown = (
            profile is not None
            and profile.key == "football"
            and profile.scoreboard_ocr_enabled
            and self._football_halftime_cached
        )

        # A matched scoreboard arms football field detection; continued broad
        # field coverage refreshes that evidence while the game remains visible.
        # Bug evidence is separately confidence-gated and cannot reopen blackout.
        replay_bug_threshold = self.replay_bug_threshold
        if profile is not None and profile.key == "football":
          replay_bug_threshold = max(replay_bug_threshold, profile.bug_threshold)
        recent_bug_evidence_time = max(
            self.last_scoreboard_seen,
            self.last_bug_seen if self.last_bug_seen is not None else 0.0,
        )
        bug_evidence_age = anchor_check_time - recent_bug_evidence_time
        bug_is_recent = bug_evidence_age <= self.bug_only_grace_seconds
        bug_signal_holds = self.bug_anchor_confirmed and (
            bug_score >= self.bug_hold_threshold
            or (
                self.last_bug_seen is not None
                and anchor_check_time - self.last_bug_seen
                <= self.bug_miss_grace_seconds
            )
        )
        strong_bug_holds = (
            self.bug_anchor_confirmed
            and bug_score >= max(
                self.bug_strong_hold_threshold,
                profile.bug_threshold if profile is not None else 0.0,
            )
        )
        strong_scoreboard_is_recent = (
            self.last_strong_scoreboard_seen is not None
            and anchor_check_time - self.last_strong_scoreboard_seen
            <= self.bug_only_grace_seconds
        )
        replay_bug_holds = (
            bug_score >= replay_bug_threshold
            and strong_scoreboard_is_recent
        )
        football_field_holds = (
            profile is not None
            and profile.key == "football"
            and not self.is_commercial
            and self.last_football_scoreboard_seen is not None
            and anchor_check_time - self.last_football_scoreboard_seen
            <= self.football_field_grace_seconds
            and field_green_ratio >= self.football_field_min_green_ratio
            and field_green_tiles >= 4
        )
        if football_halftime_countdown:
          live_signal_reason = "halftime-countdown"
        elif sb_matched:
          live_signal_reason = "scoreboard"
        elif strong_bug_holds:
          live_signal_reason = "strong-bug"
        elif bug_signal_holds and bug_is_recent:
          bug_grace_remaining = max(
              0.0,
              self.bug_only_grace_seconds - bug_evidence_age,
          )
          live_signal_reason = f"bug-grace {bug_grace_remaining:.1f}s"
        elif replay_bug_holds:
          live_signal_reason = "replay-bug"
        elif football_field_holds:
          live_signal_reason = "football-field"
        else:
          live_signal_reason = "none"
        live_detected = (
            not football_halftime_countdown
            and (
                sb_matched
                or strong_bug_holds
                or (
                    (
                        bug_signal_holds and bug_is_recent
                        or replay_bug_holds
                        or football_field_holds
                    )
                    and not self.is_commercial
                )
            )
        )

        # Require repeated scoreboard evidence before changing profiles. A bug-only
        # match is too weak because channel graphics can resemble another sport.
        if (
            self.auto_sport
            and not live_detected
            and self.is_commercial
            and not football_halftime_countdown
        ):
          switch_candidate: Optional[Tuple[SportProfile, float, float, bool]] = None
          for other_key, other_sp in self.sports_profiles.items():
            if other_sp == profile:
              continue
            if (
                not other_sp.sb_scaled_templates
                and not other_sp.bug_scaled_templates
                and not other_sp.scoreboard_ocr_enabled
            ):
              continue

            alt_sb_matched, alt_sb_score, alt_bug_matched, alt_bug_score = (
                self._match_profile_anchors(sct, other_sp)
            )

            if alt_sb_matched:
              switch_candidate = (
                  other_sp, alt_sb_score, alt_bug_score, alt_bug_matched
              )
              break

          if switch_candidate is None:
            self.auto_switch_candidate = None
            self.auto_switch_candidate_frames = 0
          else:
            other_sp, alt_sb_score, alt_bug_score, alt_bug_matched = switch_candidate
            if self.auto_switch_candidate == other_sp.key:
              self.auto_switch_candidate_frames += 1
            else:
              self.auto_switch_candidate = other_sp.key
              self.auto_switch_candidate_frames = 1

            if self.auto_switch_candidate_frames >= self.auto_switch_confirm_frames:
              prev_name = profile.name if profile else "None"
              self.current_sport = other_sp
              profile = other_sp
              live_detected = True
              sb_matched, sb_score = True, alt_sb_score
              if other_sp.key == "football":
                self.last_football_scoreboard_seen = anchor_check_time
              bug_score = alt_bug_score
              bug_matched = alt_bug_matched
              self.auto_switch_candidate = None
              self.auto_switch_candidate_frames = 0
              print(
                  f"\n[AUTO-DETECT] Switched Sport: {prev_name} -> {other_sp.name} "
                  f"after {self.auto_switch_confirm_frames} consecutive scoreboard "
                  f"matches | Scoreboard={sb_score:.2f}, Bug={bug_score:.2f}",
                  flush=True,
              )
              self.sig_sport_changed.emit(other_sp.key)
        elif live_detected:
          self.auto_switch_candidate = None
          self.auto_switch_candidate_frames = 0

        now = time.time()
        latency_ms = (time.perf_counter() - t_start) * 1000.0

        # FPS tracking
        frames_counted += 1
        if now - fps_timer >= 1.0:
          current_fps = frames_counted / (now - fps_timer)
          frames_counted = 0
          fps_timer = now

        # 4. State Machine Evaluation
        sport_label = profile.name if profile else "Sports"
        if live_detected:
          # --- GAME IS ACTIVE ---
          self.live_lost_start = None

          if self.is_commercial:
            self.consecutive_live_frames += 1
            required_live_frames = self.reappear_debounce_frames
            if (
                sb_matched
                and sb_score >= self.scoreboard_fast_recovery_threshold
            ):
              required_live_frames = 1
            if self.consecutive_live_frames >= required_live_frames:
              # TRANSITION: Commercial -> LIVE GAME
              self.is_commercial = False
              self.commercial_start_time = None
              self.consecutive_live_frames = 0
              self.audio_mgr.set_browser_mute(False)
              self.sig_trigger_blackout.emit(False)
              match_reason = (
                  f"Scoreboard (conf: {sb_score:.2f})"
                  if sb_matched
                  else f"Broadcast Bug (conf: {bug_score:.2f})"
              )
              print(
                  f"\n[LIVE SPORTS RESUMED - {sport_label}] Detected: {match_reason} | Unmuted & Broadcast Restored | Response: {latency_ms:.1f}ms",
                  flush=True,
              )
          else:
            self.consecutive_live_frames = 0
            if now - last_heartbeat >= self.heartbeat_interval:
              score_str = f"Scoreboard={sb_score:.2f}"
              if profile and profile.bug_enabled:
                score_str += f", Bug={bug_score:.2f}"
              if profile and profile.key == "football":
                score_str += (
                    f", Field={field_green_ratio:.2f}/{field_green_tiles} tiles"
                )
              print(
                  f"[LIVE - {sport_label}] Verified ({score_str}) | "
                  f"Matches=SB:{sb_matched}/Bug:{bug_matched} | "
                  f"Hold={live_signal_reason} | Check: {latency_ms:.1f}ms | "
                  f"FPS: {current_fps:.1f}",
                  flush=True,
              )
              last_heartbeat = now
        else:
          # --- NO LIVE ANCHORS VISIBLE ---
          self.consecutive_live_frames = 0

          if not self.is_commercial:
            if self.live_lost_start is None:
              self.live_lost_start = now

            lost_elapsed = now - self.live_lost_start
            if lost_elapsed >= self.disappear_debounce_seconds:
              # TRANSITION: Live -> COMMERCIAL BREAK CONFIRMED
              self.is_commercial = True
              self.commercial_start_time = now
              self.live_lost_start = None
              self.last_strong_scoreboard_seen = None
              self.last_football_scoreboard_seen = None
              self.bug_anchor_confirmed = False
              self.last_bug_seen = None
              self.audio_mgr.set_browser_mute(True)
              self.sig_trigger_blackout.emit(True)
              print(
                  f"\n[COMMERCIAL BREAK DETECTED] "
                  f"Live indicators absent for {lost_elapsed:.1f}s | "
                  f"SB={sb_score:.2f} (match={sb_matched}) "
                  f"Bug={bug_score:.2f} (match={bug_matched}) | "
                  f"Hold={live_signal_reason} | TV Blacked Out & Browser Muted | "
                  f"Response: {latency_ms:.1f}ms",
                  flush=True,
              )
            else:
              if now - last_heartbeat >= 0.5:
                print(
                    f"[HOLD] Live indicators absent for {lost_elapsed:.1f}s / {self.disappear_debounce_seconds:.1f}s...",
                    flush=True,
                )
                last_heartbeat = now
          else:
            # Still in commercial break
            break_duration = now - (self.commercial_start_time or now)

            # Safety watchdog
            if break_duration > self.max_commercial_seconds:
              self.is_commercial = False
              self.commercial_start_time = None
              self.audio_mgr.set_browser_mute(False)
              self.sig_trigger_blackout.emit(False)
              print(
                  f"\n[WATCHDOG TIMEOUT] Exceeded maximum break limit ({self.max_commercial_seconds:.0f}s) | Restored display & audio",
                  flush=True,
              )
            elif now - last_heartbeat >= self.heartbeat_interval:
              print(
                  f"[COMMERCIAL ACTIVE] Waiting for game return "
                  f"(SB={sb_score:.2f}/{sb_matched}, "
                  f"Bug={bug_score:.2f}/{bug_matched}, "
                  f"{break_duration:.0f}s) | Blackout held",
                  flush=True,
              )
              last_heartbeat = now

        # Pacing
        elapsed = time.perf_counter() - t_start
        sleep_needed = max(0.001, self.target_frame_time - elapsed)
        if self._stop_event.wait(sleep_needed):
          break


  def _run_badge_loop(self) -> None:
    """Hulu Ad Badge detection loop for standard on-demand shows."""
    attach_default_desktop()
    badge_grab_box = self._make_grab_box(self.badge_roi)

    logger.info("AD BADGE DETECTION LOOP STARTED at ~%d FPS", self.target_fps)
    last_heartbeat = time.time()

    with mss.MSS() as sct:
      while not self._stop_event.is_set():
        t_start = time.perf_counter()

        try:
          badge_raw = sct.grab(badge_grab_box)
          badge_img = np.frombuffer(badge_raw.raw, dtype=np.uint8).reshape(
              (badge_raw.height, badge_raw.width, 4)
          )
          badge_bgr = cv2.cvtColor(badge_img, cv2.COLOR_BGRA2BGR)
        except Exception:
          time.sleep(0.05)
          continue

        badge_detected, badge_score = self.check_visual_badge(badge_bgr)
        now = time.time()
        latency_ms = (time.perf_counter() - t_start) * 1000.0

        if badge_detected:
          self.consecutive_ad_frames += 1
          self.live_candidate_start = None

          if self.consecutive_ad_frames >= self.min_trigger_frames:
            if not self.is_commercial:
              self.is_commercial = True
              self.commercial_start_time = now
              self.audio_mgr.set_browser_mute(True)
              self.sig_trigger_blackout.emit(True)
              print(
                  f"\n[COMMERCIAL DETECTED] Ad Badge visible (conf: {badge_score:.2f}) | Muted & Blacked Out | Latency: {latency_ms:.1f}ms",
                  flush=True,
              )
            elif now - last_heartbeat >= self.heartbeat_interval:
              break_dur = now - (self.commercial_start_time or now)
              print(
                  f"[COMMERCIAL ACTIVE] Ad Badge held (conf: {badge_score:.2f}, {break_dur:.0f}s) | Blackout & Mute active",
                  flush=True,
              )
              last_heartbeat = now
        else:
          self.consecutive_ad_frames = 0
          if self.is_commercial:
            if self.live_candidate_start is None:
              self.live_candidate_start = now
            elapsed_live = now - self.live_candidate_start
            if elapsed_live >= self.hysteresis_seconds:
              self.is_commercial = False
              self.commercial_start_time = None
              self.live_candidate_start = None
              self.audio_mgr.set_browser_mute(False)
              self.sig_trigger_blackout.emit(False)
              print(
                  f"\n[LIVE CONTENT RESUMED] Ad Badge cleared | Broadcast restored & unmuted | Latency: {latency_ms:.1f}ms",
                  flush=True,
              )
            elif now - last_heartbeat >= 0.5:
              print(
                  f"[DEBOUNCING] Live candidate for {elapsed_live:.1f}s / {self.hysteresis_seconds:.1f}s...",
                  flush=True,
              )
              last_heartbeat = now
          else:
            if now - last_heartbeat >= self.heartbeat_interval:
              print(f"[LIVE CONTENT PLAYING] | Check: {latency_ms:.1f}ms", flush=True)
              last_heartbeat = now

        elapsed = time.perf_counter() - t_start
        sleep_needed = max(0.001, self.target_frame_time - elapsed)
        if self._stop_event.wait(sleep_needed):
          break


def get_monitors_catalog(app: Optional[QApplication] = None) -> list:
  """Retrieve catalog of all connected physical displays with names and bounds."""
  with mss.MSS() as sct:
    mss_mons = sct.monitors
  pyqt_screens = app.screens() if app else []

  catalog = []
  for idx in range(1, len(mss_mons)):
    m = mss_mons[idx]
    matched_screen = None
    for s in pyqt_screens:
      if s.geometry().x() == m["left"] and s.geometry().y() == m["top"]:
        matched_screen = s
        break
    s_name = (
        matched_screen.name()
        if matched_screen
        else m.get("name", f"Display {idx}")
    )
    uid = m.get("unique_id", "")
    catalog.append({
        "mss_index": idx,
        "name": s_name,
        "unique_id": uid,
        "bounds": m,
        "screen": matched_screen,
        "is_primary": m.get("is_primary", False),
    })
  return catalog


def list_connected_monitors(app: Optional[QApplication] = None) -> None:
  """Print a clean table of all connected monitors for easy configuration."""
  catalog = get_monitors_catalog(app)
  print("\n" + "=" * 75)
  print("                       CONNECTED MONITORS CATALOG                       ")
  print("=" * 75)
  for item in catalog:
    prim = " [PRIMARY]" if item["is_primary"] else ""
    b = item["bounds"]
    print(
        f"  - Monitor Index: {item['mss_index']} | Name: \"{item['name']}\"{prim}"
    )
    print(
        f"    Resolution: {b['width']}x{b['height']} | Position: ({b['left']},"
        f" {b['top']})"
    )
    if "55r617" in item["name"].lower() or "tcl" in item["unique_id"].lower():
      print("    >>> THIS IS YOUR 55\" TCL TV (55R617) <<<")
    elif "sceptre" in item["name"].lower() or "spt" in item["unique_id"].lower():
      print("    >>> THIS IS YOUR SCEPTRE MONITOR <<<")
    print("-" * 75)
  print("\nTip: In config.json or with --monitor, you can use either:")
  print("     \"monitor_target\": \"55R617\"  (by name)")
  print("     \"monitor_index\": 2          (by index)\n")


def resolve_target_monitor(
    config: Dict[str, Any],
    app: Optional[QApplication] = None,
    monitor_override: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[Any], str]:
  """Resolve target monitor by name or index.

  Returns: (mss_monitor_bounds, pyqt_screen_obj, friendly_display_name)
  """
  target_spec = (
      monitor_override
      if monitor_override
      else config.get("target", {}).get("monitor_target")
  )
  if target_spec is None:
    target_spec = config.get("target", {}).get("monitor_index", 2)

  with mss.MSS() as sct:
    mss_mons = sct.monitors

  screens = app.screens() if app else []
  target_str = str(target_spec).strip()

  try:
    idx = int(target_str)
    if 1 <= idx < len(mss_mons):
      m = mss_mons[idx]
      matched_screen = next(
          (
              s
              for s in screens
              if s.geometry().x() == m["left"] and s.geometry().y() == m["top"]
          ),
          None,
      )
      name = matched_screen.name() if matched_screen else m.get("name", f"Mon {idx}")
      return m, matched_screen, f"'{name}' (index {idx})"
  except ValueError:
    pass

  for s in screens:
    if target_str.lower() in s.name().lower():
      matched_m = next(
          (
              m
              for m in mss_mons[1:]
              if m["left"] == s.geometry().x() and m["top"] == s.geometry().y()
          ),
          None,
      )
      m_idx = mss_mons.index(matched_m) if matched_m else "?"
      bounds = (
          matched_m
          if matched_m
          else {
              "left": s.geometry().x(),
              "top": s.geometry().y(),
              "width": s.geometry().width(),
              "height": s.geometry().height(),
          }
      )
      return bounds, s, f"'{s.name()}' (index {m_idx})"

  for idx in range(1, len(mss_mons)):
    m = mss_mons[idx]
    uid = m.get("unique_id", "")
    m_name = m.get("name", "")
    if target_str.lower() in uid.lower() or target_str.lower() in m_name.lower():
      matched_screen = next(
          (
              s
              for s in screens
              if s.geometry().x() == m["left"] and s.geometry().y() == m["top"]
          ),
          None,
      )
      name = matched_screen.name() if matched_screen else m_name
      return m, matched_screen, f"'{name}' (index {idx})"

  fallback_idx = 2 if len(mss_mons) > 2 else 1
  m = mss_mons[fallback_idx]
  matched_screen = next(
      (
          s
          for s in screens
          if s.geometry().x() == m["left"] and s.geometry().y() == m["top"]
      ),
      None,
  )
  name = matched_screen.name() if matched_screen else m.get("name", f"Mon {fallback_idx}")
  return m, matched_screen, f"'{name}' (index {fallback_idx})"


def compute_cutout_rois(
    profile: Optional[SportProfile],
    cutout_mode: str = "bug_only",
    active_mode: str = "sports",
    badge_roi: Optional[dict] = None,
) -> List[dict]:
  """Determine which screen regions to cut out and frost-blur based on cutout_mode."""
  if active_mode != "sports" or profile is None:
    return [badge_roi] if badge_roi else []

  mode = (cutout_mode or "bug_only").lower().strip()
  if mode == "none":
    return []

  rois = []
  if mode in ("both", "bug_only") and profile.bug_enabled and profile.bug_roi:
    rois.append(profile.bug_roi)
  if mode in ("both", "scoreboard_only") and profile.scoreboard_roi:
    rois.append(profile.scoreboard_roi)
  return rois


def run_calibration_mode(
    config_path: str,
    monitor_override: Optional[str] = None,
    mode: str = "sports",
    target_type: str = "scoreboard",
    sport: str = "baseball",
    add_template: bool = False,
) -> None:
  """Launch interactive calibration overlay on target monitor."""
  attach_default_desktop()
  app = QApplication(sys.argv)

  with open(config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

  mon_bounds, qscreen, disp_name = resolve_target_monitor(
      cfg, app, monitor_override
  )

  with mss.MSS() as sct:
    print(f"[CALIBRATION] Capturing freeze frame of {disp_name}: {mon_bounds}")
    raw = sct.grab(mon_bounds)
    img = np.frombuffer(raw.raw, dtype=np.uint8).reshape(
        (raw.height, raw.width, 4)
    )
    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

  # Check if screenshot is completely pitch black (Widevine DRM / Hardware Acceleration warning)
  mean_lum = float(np.mean(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)))
  if mean_lum < 1.0:
    print("\n" + "!" * 75)
    print("WARNING: Screen capture appears completely black!")
    print("This happens when Chromium/Brave/Edge has Hardware Acceleration enabled,")
    print("which causes Windows Widevine DRM to protect video from screen capture.")
    print("\nFIX IN 10 SECONDS:")
    print("  1. In Brave/Chrome, go to: brave://settings/system")
    print("  2. Turn OFF 'Use graphics acceleration when available'")
    print("  3. Click 'Relaunch' and re-run calibration!")
    print("!" * 75 + "\n")

  geom, _ = find_screen_by_mss_bounds(app, mon_bounds)
  calib_win = CalibrationOverlay(
      geometry_tuple=geom,
      screenshot_bgr=bgr,
      config_path=config_path,
      monitor_name=disp_name,
      mode=mode,
      target_type=target_type,
      sport=sport,
      qscreen=qscreen,
      add_template=add_template,
  )
  calib_win.show()
  app._interrupt_keep_alive = install_qt_interrupt_handling(app.quit)
  print(f"\n[CALIBRATION] Calibration window opened on {disp_name} for sport profile '{sport}'.")
  if target_type == "broadcast_bug":
    print("  >>> Click & drag a rectangle around the Broadcast Watermark (e.g. 'FOX NLCS GAME 1').")
  elif mode == "sports":
    print("  >>> Click & drag a rectangle around the Scoreboard / Team Bug on screen.")
  else:
    print("  >>> Click & drag a rectangle around Hulu's 'Ad' badge on screen.")
  print("  >>> Press [Enter] or [S] to Save | Press [Esc] to Cancel.\n")
  app.exec()


def run_test_detection(
    config_path: str,
    monitor_override: Optional[str] = None,
    mode_override: Optional[str] = None,
    sport_override: Optional[str] = None,
) -> None:
  """Run diagnostic detection test and print real-time telemetry."""
  attach_default_desktop()
  app = QApplication(sys.argv)
  with open(config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

  audio_mgr = AudioManager(
      target_processes=cfg.get("target", {}).get("browser_executables")
  )

  mon_bounds, _, disp_name = resolve_target_monitor(cfg, app, monitor_override)
  mode = mode_override or cfg.get("detection_mode", "sports")

  engine = DetectionEngine(
      config=cfg,
      audio_mgr=audio_mgr,
      monitor_bounds=mon_bounds,
      mode_override=mode,
      sport_override=sport_override,
  )

  print("\n" + "=" * 75)
  print(f"RUNNING DETECTION DIAGNOSTICS ON {disp_name} [{mode.upper()} MODE]")
  print("=" * 75)

  sessions = audio_mgr.get_active_browser_sessions()
  print(f"[Audio] Active Browser Sessions Found: {len(sessions)}")

  if mode == "sports":
    p = engine.current_sport
    p_name = p.name if p else "None"
    print(f"[Sports] Active Profile: {p_name} (Auto-Switch: {engine.auto_sport})")
    if p:
      print(f"  - Scoreboard Template: {p.scoreboard_template_path} (Loaded: {p.sb_template_img is not None})")
      print(f"  - Scoreboard ROI: {p.scoreboard_roi} | Threshold: {p.match_threshold}")
      print(f"  - Broadcast Bug: {p.bug_template_path} (Enabled: {p.bug_enabled}, Loaded: {p.bug_template_img is not None})")
      print(f"  - Broadcast Bug ROI: {p.bug_roi} | Threshold: {p.bug_threshold}")
      print(f"  - Bug templates loaded: {len(p.bug_scaled_templates)} scaled variants")
      play = engine._refresh_playback_rect()
      print(f"  - Playback area (monitor-relative): {play}")
    print(f"[Sports] Commercial Trigger Debounce: {engine.disappear_debounce_seconds}s")
    print(f"[Sports] Available Profiles: {list(engine.sports_profiles.keys())}")
  else:
    print(f"[Badge] Template Path: {engine.template_path} | Threshold: {engine.template_threshold}")

  print("-" * 75)
  print("Running 15 sample diagnostic frames (Press Ctrl+C to stop)...")

  with mss.MSS() as sct:
    for i in range(15):
      t0 = time.perf_counter()

      if mode == "sports":
        p = engine.current_sport
        sb_ok, sb_conf = False, 0.0
        bug_ok, bug_conf = False, 0.0

        if p is not None:
          sb_ok, sb_conf, bug_ok, bug_conf = engine._match_profile_anchors(sct, p)

        field_ratio, field_tiles = (0.0, 0)
        if p is not None and p.key == "football":
          field_ratio, field_tiles = engine._sample_football_field(
              sct, engine._refresh_playback_rect()
          )

        live = sb_ok or bug_ok
        status = f"LIVE ({p.name if p else 'Game'})" if live else "COMMERCIAL / ABSENT"
        info = (
            f"SB={sb_conf:.2f}/{sb_ok}"
            if p is not None
            else "SB=unavailable"
        )
        if p and p.bug_enabled:
          info += f", Bug={bug_conf:.2f}/{bug_ok}"
        if p and p.key == "football":
          info += f", Field={field_ratio:.2f}/{field_tiles} tiles"
      else:
        badge_box = engine._make_grab_box(engine.badge_roi)
        raw = sct.grab(badge_box)
        img = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))
        matched, score = engine.check_visual_badge(cv2.cvtColor(img, cv2.COLOR_BGRA2BGR))
        status = "AD DETECTED" if matched else "REGULAR CONTENT"
        info = f"Badge={score:.2f}"

      peak = audio_mgr.get_peak_volume()
      latency = (time.perf_counter() - t0) * 1000.0
      print(
          f"Frame {i + 1:2d} | Status: {status:22s} | "
          f"Signals: ({info}) | Audio: {peak:.3f} | Latency: {latency:4.1f}ms"
      )
      time.sleep(0.1)

  print("=" * 75 + "\n")


def run_simulation_mode(
    config_path: str, monitor_override: Optional[str] = None
) -> None:
  """Run an immediate 5-second simulated commercial break with browser muting & TV blackout."""
  attach_default_desktop()
  app = QApplication(sys.argv)

  with open(config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

  audio_mgr = AudioManager(
      target_processes=cfg.get("target", {}).get("browser_executables")
  )

  mon_bounds, qscreen, disp_name = resolve_target_monitor(
      cfg, app, monitor_override
  )
  geom, _ = find_screen_by_mss_bounds(app, mon_bounds)
  bg_color = cfg.get("overlay", {}).get("background_color", "#000000")
  cutout_enabled = cfg.get("overlay", {}).get("cutout_roi", False)
  blur_cutouts = cfg.get("overlay", {}).get("blur_cutouts", False)
  cutout_mode = cfg.get("overlay", {}).get("cutout_mode", "none")

  sports_profiles = cfg.get("sports_profiles", {})
  first_p = next(iter(sports_profiles.values()), cfg.get("sports_detection", {}))
  mock_p = SportProfile("sim", first_p)
  mock_rois = compute_cutout_rois(mock_p, cutout_mode=cutout_mode)

  overlay = BlackoutOverlay(
      geometry_tuple=geom,
      bg_color=bg_color,
      qscreen=qscreen,
      cutout_roi=mock_rois,
      cutout_enabled=cutout_enabled,
      blur_cutouts=blur_cutouts,
  )

  print("\n" + "=" * 75)
  print("   SIMULATING COMMERCIAL BREAK TEST (Mute + TV Blackout + Frosted Cutouts)   ")
  print("=" * 75)
  print(f"[+] Target Screen: {disp_name}")
  active_sessions = audio_mgr.get_active_browser_sessions()
  print(f"[+] Active Browser Audio Sessions: {len(active_sessions)}")
  print(f"[+] Cutout Mode: {cutout_mode} ({len(mock_rois)} inspection holes, Blur: {blur_cutouts})")

  print("\n>>> STEP 1: Commercial Started! Muting Browser & Blacking Out Screen")
  audio_mgr.set_browser_mute(True)
  overlay.show_blackout()
  app.processEvents()

  for sec in range(5, 0, -1):
    print(f"    [COMMERCIAL ACTIVE] Blackout & Mute held... ({sec}s remaining)")
    time.sleep(1.0)
    app.processEvents()

  print("\n>>> STEP 2: Commercial Ended! Unmuting Browser & Hiding Blackout")
  audio_mgr.set_browser_mute(False)
  overlay.hide_blackout()
  app.processEvents()

  print("=" * 75)
  print("[+] TEST SUCCESSFUL! Both audio muting and TV blackout verified.")
  print("=" * 75 + "\n")


def main() -> None:
  """Application entry point."""
  parser = argparse.ArgumentParser(
      description="Live Sports & Streaming Ad Blocker with Monitor Blackout"
  )
  parser.add_argument(
      "--config",
      type=str,
      default="config.json",
      help="Path to configuration JSON file",
  )
  parser.add_argument(
      "--monitor",
      type=str,
      default=None,
      help="Override target monitor by name (e.g. '55R617') or index (e.g. 2)",
  )
  parser.add_argument(
      "--mode",
      type=str,
      choices=["sports", "badge"],
      default=None,
      help="Force detection mode: 'sports' (keep-alive) or 'badge' (ad icon)",
  )
  parser.add_argument(
      "--sport",
      type=str,
      default=None,
      help="Target sport profile: 'auto', 'baseball', 'football', or custom profile name (default: auto)",
  )
  parser.add_argument(
      "--cutout-mode",
      type=str,
      choices=["bug_only", "both", "scoreboard_only", "none"],
      default=None,
      help="Optional inspection holes (debug only). Default 'none' keeps a full black screen; detection still sees the game because the overlay is hidden from capture.",
  )
  parser.add_argument(
      "--list-monitors",
      action="store_true",
      help="List all connected monitors, names, resolutions, and indices",
  )
  parser.add_argument(
      "--calibration",
      action="store_true",
      help="Open interactive calibration overlay on target monitor for current mode",
  )
  parser.add_argument(
      "--calibrate-scoreboard",
      action="store_true",
      help="Open calibration overlay to select live sports scoreboard / team bug",
  )
  parser.add_argument(
      "--calibrate-bug",
      action="store_true",
      help="Open calibration overlay to select persistent broadcast bug (e.g. 'FOX NLCS GAME 1')",
  )
  parser.add_argument(
      "--add-template",
      action="store_true",
      help="With --calibrate-bug or --calibrate-scoreboard, save an extra look (other channel/network) without replacing the existing template",
  )
  parser.add_argument(
      "--calibrate-badge",
      action="store_true",
      help="Open calibration overlay to select Hulu 'Ad' badge",
  )
  parser.add_argument(
      "--test-detect",
      action="store_true",
      help="Run diagnostic test frames without opening blackout window",
  )
  parser.add_argument(
      "--test-simulation",
      action="store_true",
      help="Simulate a 5s commercial break (mutes browser & blacks out TV)",
  )
  args = parser.parse_args()

  attach_default_desktop()

  if args.list_monitors:
    app = QApplication(sys.argv)
    list_connected_monitors(app)
    return

  config_path = args.config
  if not os.path.exists(config_path):
    print(f"[!] Config file not found at {config_path}.")
    return

  with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

  active_mode = args.mode or config.get("detection_mode", "sports")
  target_sport = (args.sport or config.get("active_sport", "baseball")).lower().strip()
  if target_sport == "auto":
    target_sport = "baseball"

  if args.calibrate_scoreboard:
    run_calibration_mode(
        config_path=config_path,
        monitor_override=args.monitor,
        mode="sports",
        target_type="scoreboard",
        sport=target_sport,
        add_template=args.add_template,
    )
    return

  if args.calibrate_bug:
    run_calibration_mode(
        config_path=config_path,
        monitor_override=args.monitor,
        mode="sports",
        target_type="broadcast_bug",
        sport=target_sport,
        add_template=args.add_template,
    )
    return

  if args.calibrate_badge:
    run_calibration_mode(
        config_path=config_path,
        monitor_override=args.monitor,
        mode="badge",
        target_type="badge",
    )
    return

  if args.calibration:
    run_calibration_mode(
        config_path=config_path,
        monitor_override=args.monitor,
        mode=active_mode,
        target_type="scoreboard",
        sport=target_sport,
        add_template=args.add_template,
    )
    return

  if args.test_simulation:
    run_simulation_mode(config_path=config_path, monitor_override=args.monitor)
    return

  if args.test_detect:
    run_test_detection(
        config_path=config_path,
        monitor_override=args.monitor,
        mode_override=active_mode,
        sport_override=args.sport,
    )
    return

  # --- PRODUCTION MONITORING MODE ---
  app = QApplication(sys.argv)

  mon_bounds, qscreen, disp_name = resolve_target_monitor(
      config, app, args.monitor
  )

  cutout_mode = (
      args.cutout_mode
      or config.get("overlay", {}).get("cutout_mode", "none")
  )
  blur_cutouts = config.get("overlay", {}).get("blur_cutouts", False)
  bg_color = config.get("overlay", {}).get("background_color", "#000000")
  # Visual holes are not needed: the overlay is excluded from screen capture,
  # so OpenCV still sees the live scoreboard/bug under a fully black TV.
  cutout_enabled = bool(config.get("overlay", {}).get("cutout_roi", False))
  if cutout_mode == "none":
    cutout_enabled = False

  print("=" * 75)
  print("   LIVE SPORTS & STREAMING AD BLOCKER (MONITOR BLACKOUT + MUTE)   ")
  print("=" * 75)
  print(f"[+] Target Display: {disp_name}")
  print(f"[+] Target Bounds:  {mon_bounds['width']}x{mon_bounds['height']} at ({mon_bounds['left']}, {mon_bounds['top']})")
  print(f"[+] Target Browsers: {', '.join(config.get('target', {}).get('browser_executables', []))}")
  print(f"[+] Detection Mode: {active_mode.upper()}")
  if active_mode == "sports":
    active_sp = (args.sport or config.get("active_sport", "auto")).lower()
    print(f"[+] Sport Profile:  {active_sp.upper()} (Auto-detect & switch sports: {active_sp == 'auto'})")
    print("[+] Overlay:        Full black (no scoreboard/bug holes)")
    print("    >>> Capture ignores the overlay, so game-return detection still works.")
    print("    >>> Windowed tabs: maps calibration into the browser video area (not just fullscreen).")
    print("    >>> Extra channel bugs: python main.py --calibrate-bug --add-template")
    print(
        "    >>> Blackout delay: %.1fs after both live anchors vanish."
        % float(config.get("sports_detection", {}).get("disappear_debounce_seconds", 0.1))
    )
    print("    >>> Windowed browser top offset: configure sports_detection.windowed_top_inset_px if needed.")
    print(
        "    >>> Recovery: strong scoreboard matches (>= %.2f) restore immediately; "
        "other matches require %d frames."
        % (
            float(config.get("sports_detection", {}).get(
                "scoreboard_fast_recovery_threshold", 0.85
            )),
            int(config.get("sports_detection", {}).get(
                "reappear_debounce_frames", 1
            )),
        )
    )
    print(
        "    >>> Bug-only replay grace: %.1fs after the last scoreboard match."
        % float(config.get("sports_detection", {}).get(
            "bug_only_grace_seconds", 5.0
        ))
    )
  else:
    print("    >>> Ad Badge Trigger: Blackout & Mute engaged when 'Ad' badge appears.")
  print("=" * 75)

  target_procs = config.get("target", {}).get(
      "browser_executables", ["chrome.exe", "brave.exe", "msedge.exe"]
  )
  audio_mgr = AudioManager(target_processes=target_procs)

  def cleanup():
    logger.info("Restoring browser audio before shutdown...")
    try:
      audio_mgr.set_browser_mute(False)
    except Exception:
      pass

  atexit.register(cleanup)

  detector = DetectionEngine(
      config=config,
      audio_mgr=audio_mgr,
      monitor_bounds=mon_bounds,
      mode_override=active_mode,
      sport_override=args.sport,
  )

  initial_rois = compute_cutout_rois(
      detector.current_sport,
      cutout_mode=cutout_mode,
      active_mode=active_mode,
      badge_roi=config.get("visual_detection", {}).get("badge_roi"),
  )

  geom, _ = find_screen_by_mss_bounds(app, mon_bounds)
  overlay = BlackoutOverlay(
      geometry_tuple=geom,
      bg_color=bg_color,
      qscreen=qscreen,
      cutout_roi=initial_rois,
      cutout_enabled=cutout_enabled,
      blur_cutouts=blur_cutouts,
  )

  detector.sig_trigger_blackout.connect(
      lambda show: overlay.show_blackout() if show else overlay.hide_blackout()
  )

  def on_sport_changed(sport_key: str):
    sp = detector.sports_profiles.get(sport_key)
    if sp:
      new_rois = compute_cutout_rois(sp, cutout_mode=cutout_mode, active_mode=active_mode)
      overlay.set_cutout_rois(new_rois)
      logger.info("Updated overlay cutouts for '%s' (%d cutouts)", sp.name, len(new_rois))

  detector.sig_sport_changed.connect(on_sport_changed)

  def shutdown():
    detector.stop()
    cleanup()
    app.quit()

  app._interrupt_keep_alive = install_qt_interrupt_handling(shutdown)

  detector.start()
  exit_code = app.exec()
  detector.stop()
  cleanup()
  sys.exit(exit_code)


if __name__ == "__main__":
  main()
