"""audio_manager.py

High-performance, COM-safe process audio management for Windows 11 using pycaw.
Targets ONLY specified browser executables (Chrome, Brave, Edge) to mute commercials
and inspect real-time audio dynamics (peak levels and compression profiling).
"""

from collections import deque
import logging
from typing import List, Optional
import comtypes
import numpy as np
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation, ISimpleAudioVolume

logger = logging.getLogger("HuluBlocker.Audio")


class AudioManager:
  """Manages per-process audio sessions and peak dynamics for web browsers."""

  def __init__(
      self,
      target_processes: Optional[List[str]] = None,
      window_size: int = 15,
      high_peak_threshold: float = 0.65,
      sustained_peak_threshold: float = 0.50,
      low_dynamic_range_threshold: float = 0.08,
  ):
    """Initialize the audio manager with target browser executables and dynamics thresholds."""
    if target_processes is None:
      self.target_processes = ["chrome.exe", "brave.exe", "msedge.exe"]
    else:
      self.target_processes = [p.lower() for p in target_processes]

    self.window_size = window_size
    self.high_peak_threshold = high_peak_threshold
    self.sustained_peak_threshold = sustained_peak_threshold
    self.low_dynamic_range_threshold = low_dynamic_range_threshold

    self._peak_history = deque(maxlen=window_size)
    self._is_muted = False
    self._com_initialized = False
    self._ensure_com()

  def _ensure_com(self) -> None:
    """Ensure COM library is initialized for the current thread."""
    try:
      comtypes.CoInitialize()
      self._com_initialized = True
    except Exception as e:
      logger.debug("COM initialization note: %s", e)

  def get_active_browser_sessions(self) -> list:
    """Enumerate all active audio sessions belonging specifically to target browser processes.

    Returns a list of matching pycaw AudioSession objects.
    """
    self._ensure_com()
    matching_sessions = []
    try:
      sessions = AudioUtilities.GetAllSessions()
      for session in sessions:
        if session.Process:
          try:
            proc_name = session.Process.name().lower()
            if any(t in proc_name for t in self.target_processes):
              matching_sessions.append(session)
          except Exception:
            # Process may have terminated during query
            continue
    except Exception as e:
      logger.error("Failed to query audio sessions: %s", e)

    return matching_sessions

  def get_peak_volume(self) -> float:
    """Query current instantaneous peak volume (0.0 to 1.0) across all browser audio sessions.

    Returns 0.0 if no sessions are active or audio is silent.
    """
    sessions = self.get_active_browser_sessions()
    if not sessions:
      return 0.0

    max_peak = 0.0
    for session in sessions:
      try:
        meter = session._ctl.QueryInterface(IAudioMeterInformation)
        peak = meter.GetPeakValue()
        if peak > max_peak:
          max_peak = peak
      except Exception:
        continue

    return max_peak

  def update_audio_dynamics(self) -> dict:
    """Sample current peak volume and update rolling dynamics buffer.

    Commercials exhibit heavy multiband compression and limiting with sustained
    peak levels near 0dB (high average peak) and compressed dynamic range (low
    variance).
    """
    current_peak = self.get_peak_volume()
    self._peak_history.append(current_peak)

    if len(self._peak_history) == 0:
      return {
          "current_peak": 0.0,
          "mean_peak": 0.0,
          "variance": 0.0,
          "is_compressed_commercial": False,
      }

    samples = np.array(self._peak_history)
    mean_peak = float(np.mean(samples))
    variance = float(np.std(samples))

    # Commercial audio profile: sustained high volume or high peak with low dynamic swing
    is_compressed = (
        len(self._peak_history) >= 5
        and (
            mean_peak >= self.sustained_peak_threshold
            or current_peak >= self.high_peak_threshold
        )
        and variance <= self.low_dynamic_range_threshold
    )

    return {
        "current_peak": current_peak,
        "mean_peak": mean_peak,
        "variance": variance,
        "is_compressed_commercial": is_compressed,
    }

  def set_browser_mute(self, mute: bool) -> int:
    """Mute or un-mute ONLY the target browser processes without touching master audio volume.

    Args:
        mute: True to mute, False to un-mute.

    Returns:
        Number of audio sessions affected.
    """
    self._ensure_com()
    sessions = self.get_active_browser_sessions()
    affected_count = 0

    mute_val = 1 if mute else 0
    for session in sessions:
      try:
        vol = session.SimpleAudioVolume
        vol.SetMute(mute_val, None)
        affected_count += 1
      except Exception as e:
        logger.debug(
            "Failed to set mute state on session %s: %s", session.ProcessId, e
        )

    self._is_muted = mute
    return affected_count

  @property
  def is_muted(self) -> bool:
    """Return whether the browser audio is currently set to muted."""
    return self._is_muted
