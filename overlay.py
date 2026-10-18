"""overlay.py

PyQt6 frameless, borderless full-screen blackout window logic pinned to Monitor 3.
Provides high-performance show/hide operations without stealing application focus,
plus an interactive GUI calibration mode for template capture and ROI bounding.
"""

import ctypes
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple
import cv2
import numpy as np
from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap, QRegion
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("HuluBlocker.Overlay")

# Windows 10 2004+: window is visible to the user but omitted from screen capture.
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def exclude_window_from_capture(widget: QWidget) -> None:
  """Keep the overlay on-screen for the viewer without covering mss/OpenCV grabs.

  If capture includes the blackout window, commercial recovery never matches the
  live scoreboard/bug and the TV stays black forever.
  """
  try:
    hwnd = int(widget.winId())
    ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
  except Exception as exc:
    logger.debug("Could not exclude window from capture: %s", exc)


def find_screen_by_mss_bounds(
    app: QApplication, mss_monitor: dict
) -> Tuple[Tuple[int, int, int, int], Optional[Any]]:
  """Find the exact PyQt geometry coordinates and QScreen matching an mss monitor dictionary."""
  target_left = mss_monitor.get("left", 0)
  target_top = mss_monitor.get("top", 0)
  target_width = mss_monitor.get("width", 1920)
  target_height = mss_monitor.get("height", 1080)

  screens = app.screens()
  # Look for screen matching coordinates
  for screen in screens:
    geo = screen.geometry()
    if geo.x() == target_left and geo.y() == target_top:
      logger.info(
          "Matched Monitor by origin: %s at (%d, %d)",
          screen.name(),
          geo.x(),
          geo.y(),
      )
      return (geo.x(), geo.y(), geo.width(), geo.height()), screen

  # Fallback to direct mss coordinates if no exact origin match
  logger.info(
      "Using direct coordinates from mss: (%d, %d, %d, %d)",
      target_left,
      target_top,
      target_width,
      target_height,
  )
  return (target_left, target_top, target_width, target_height), None


class FrostedCutoutWidget(QWidget):
  """Frameless stay-on-top blur overlay that obscures commercial video inside cutouts.

  Uses Windows 11 ACCENT_ENABLE_BLURBEHIND so human eyes see an elegant frosted
  dark acrylic blur instead of moving commercial ads, while mss and OpenCV
  can still detect when the bright live scoreboard/badge returns.
  """

  def __init__(self, x: int, y: int, w: int, h: int, parent: Optional[QWidget] = None):
    super().__init__(parent)
    self.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
        | Qt.WindowType.WindowDoesNotAcceptFocus
    )
    self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    self.setGeometry(x, y, w, h)
    self._apply_acrylic_blur()
    exclude_window_from_capture(self)

  def _apply_acrylic_blur(self) -> None:
    """Apply Windows 11 DWM hardware acrylic blur."""
    try:
      class ACCENT_POLICY(ctypes.Structure):
        _fields_ = [
            ("AccentState", ctypes.c_uint32),
            ("AccentFlags", ctypes.c_uint32),
            ("GradientColor", ctypes.c_uint32),
            ("AnimationId", ctypes.c_uint32),
        ]

      class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
        _fields_ = [
            ("Attribute", ctypes.c_uint32),
            ("Data", ctypes.POINTER(ACCENT_POLICY)),
            ("SizeOfData", ctypes.c_size_t),
        ]

      accent = ACCENT_POLICY()
      accent.AccentState = 3  # ACCENT_ENABLE_BLURBEHIND
      accent.AccentFlags = 2
      accent.GradientColor = 0xD0000000  # Dark 82% opacity frosted tint
      data = WINDOWCOMPOSITIONATTRIBDATA()
      data.Attribute = 19
      data.Data = ctypes.pointer(accent)
      data.SizeOfData = ctypes.sizeof(accent)
      ctypes.windll.user32.SetWindowCompositionAttribute(
          int(self.winId()), ctypes.byref(data)
      )
    except Exception as e:
      logger.debug("Accent blur note: %s", e)

  def paintEvent(self, event) -> None:
    """Render subtle dark frosted tint over cutout."""
    painter = QPainter(self)
    painter.fillRect(self.rect(), QColor(10, 10, 15, 175))


class BlackoutOverlay(QWidget):
  """Frameless, borderless, stay-on-top pure black window pinned to target monitor.

  Configured to not steal focus or create taskbar entries.
  """

  sig_show_blackout = pyqtSignal()
  sig_hide_blackout = pyqtSignal()

  def __init__(
      self,
      geometry_tuple: Tuple[int, int, int, int],
      bg_color: str = "#000000",
      qscreen: Optional[Any] = None,
      cutout_roi: Optional[Any] = None,
      cutout_enabled: bool = True,
      blur_cutouts: bool = False,
      # For backward compatibility:
      badge_roi: Optional[dict] = None,
      cutout_badge: Optional[bool] = None,
      parent: Optional[QWidget] = None,
  ):
    super().__init__(parent)
    self.x, self.y, self.w, self.h = geometry_tuple
    self.bg_color = bg_color
    self.qscreen = qscreen
    self.blur_cutouts = blur_cutouts
    self.frosted_widgets: List[FrostedCutoutWidget] = []

    # Support single ROI dict or list of ROI dicts
    if cutout_roi:
      self.cutout_rois = cutout_roi if isinstance(cutout_roi, list) else [cutout_roi]
    elif badge_roi:
      self.cutout_rois = [badge_roi]
    else:
      self.cutout_rois = []

    if cutout_badge is not None:
      self.cutout_enabled = cutout_badge
    else:
      self.cutout_enabled = cutout_enabled

    self._setup_window_attributes()
    self._connect_signals()

  def set_cutout_rois(self, rois: Any) -> None:
    """Update cutout ROIs dynamically."""
    if isinstance(rois, list):
      self.cutout_rois = rois
    elif isinstance(rois, dict):
      self.cutout_rois = [rois]
    else:
      self.cutout_rois = []
    if self.isVisible():
      self._apply_mask()
      if self.blur_cutouts:
        self._update_frosted_widgets()
        for fw in self.frosted_widgets:
          fw.show()
          fw.raise_()

  def _setup_window_attributes(self) -> None:
    """Configure PyQt window flags for frameless stay-on-top behavior without focus stealing."""
    self.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
        | Qt.WindowType.WindowDoesNotAcceptFocus
    )
    self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
    self.setStyleSheet(f"background-color: {self.bg_color};")

    if self.qscreen:
      self.setScreen(self.qscreen)
      geo = self.qscreen.geometry()
      self.setGeometry(geo)
      self.w = geo.width()
      self.h = geo.height()
    else:
      self.setGeometry(self.x, self.y, self.w, self.h)
    self.hide()

  def _update_frosted_widgets(self) -> None:
    """Create or reposition frosted blur widgets over cutout inspection holes."""
    for fw in self.frosted_widgets:
      fw.hide()
      fw.deleteLater()
    self.frosted_widgets = []

    if not self.blur_cutouts or not self.cutout_enabled:
      return

    dpr = self.qscreen.devicePixelRatio() if self.qscreen else 1.0
    win_x = self.geometry().x()
    win_y = self.geometry().y()

    for r in self.cutout_rois:
      if isinstance(r, dict) and r.get("width", 0) > 0 and r.get("height", 0) > 0:
        rx = int(max(0, r.get("left", 0)) / dpr)
        ry = int(max(0, r.get("top", 0)) / dpr)
        rw = int(max(10, r.get("width", 100)) / dpr)
        rh = int(max(10, r.get("height", 100)) / dpr)
        fw = FrostedCutoutWidget(win_x + rx, win_y + ry, rw, rh)
        self.frosted_widgets.append(fw)

  def _apply_mask(self) -> None:
    """Apply window mask with cutouts over inspection ROIs if enabled."""
    if self.cutout_enabled and self.cutout_rois:
      full_w = self.width() if self.width() > 0 else self.w
      full_h = self.height() if self.height() > 0 else self.h
      full_region = QRegion(0, 0, full_w, full_h)
      dpr = self.qscreen.devicePixelRatio() if self.qscreen else 1.0
      for r in self.cutout_rois:
        if isinstance(r, dict) and r.get("width", 0) > 0 and r.get("height", 0) > 0:
          rx = int(max(0, r.get("left", 0)) / dpr)
          ry = int(max(0, r.get("top", 0)) / dpr)
          rw = int(max(10, r.get("width", 100)) / dpr)
          rh = int(max(10, r.get("height", 100)) / dpr)
          cutout_region = QRegion(rx, ry, rw, rh)
          full_region = full_region.subtracted(cutout_region)
      self.setMask(full_region)
      logger.debug("Applied blackout mask with %d cutouts (DPR=%.2f)", len(self.cutout_rois), dpr)
    else:
      self.clearMask()

  def _connect_signals(self) -> None:
    """Connect thread-safe signals to slots."""
    self.sig_show_blackout.connect(self.show_blackout)
    self.sig_hide_blackout.connect(self.hide_blackout)

  @pyqtSlot()
  def show_blackout(self) -> None:
    """Display the full-screen pure black blackout overlay and frosted blur cutouts."""
    if not self.isVisible():
      if self.qscreen:
        self.setScreen(self.qscreen)
        self.setGeometry(self.qscreen.geometry())
      else:
        self.setGeometry(self.x, self.y, self.w, self.h)
      self._apply_mask()
      self.show()
      self.raise_()
      exclude_window_from_capture(self)
      if self.blur_cutouts:
        self._update_frosted_widgets()
        for fw in self.frosted_widgets:
          fw.show()
          fw.raise_()
          exclude_window_from_capture(fw)
      logger.debug("Blackout overlay SHOW")

  @pyqtSlot()
  def hide_blackout(self) -> None:
    """Hide the blackout overlay and return display to live broadcast."""
    if self.isVisible():
      for fw in self.frosted_widgets:
        fw.hide()
      self.hide()
      logger.debug("Blackout overlay HIDE")

  def keyPressEvent(self, event) -> None:
    """Allow user to manually dismiss the overlay with Esc, Space, or Q, or quit on Ctrl+C."""
    if event.key() == Qt.Key.Key_C and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
      logger.info("Ctrl+C pressed on overlay. Exiting application...")
      QApplication.quit()
    elif event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Space, Qt.Key.Key_Q):
      logger.info("Manual overlay dismiss requested by user keypress.")
      self.hide_blackout()


class CalibrationOverlay(QWidget):
  """Interactive fullscreen calibration helper on target monitor.

  Allows clicking and dragging a rectangle to capture a live sports Scoreboard,
  persistent Broadcast Bug / Watermark, or Hulu's 'Ad' badge, and automatically
  saves the cropped template and ROI coordinates.
  """

  def __init__(
      self,
      geometry_tuple: Tuple[int, int, int, int],
      screenshot_bgr: np.ndarray,
      config_path: str = "config.json",
      template_path: Optional[str] = None,
      monitor_name: str = "Monitor",
      mode: str = "sports",
      target_type: str = "scoreboard",  # "scoreboard", "broadcast_bug", or "badge"
      sport: str = "baseball",
      qscreen: Optional[Any] = None,
      add_template: bool = False,
      parent: Optional[QWidget] = None,
  ):
    super().__init__(parent)
    self.x, self.y, self.w, self.h = geometry_tuple
    self.screenshot_bgr = screenshot_bgr
    self.config_path = config_path
    self.monitor_name = monitor_name
    self.mode = mode
    self.target_type = target_type
    self.sport = sport.lower().strip()
    self.add_template = add_template

    if template_path:
      self.template_path = template_path
    elif target_type == "broadcast_bug":
      primary = f"templates/{self.sport}_bug.png"
      self.template_path = (
          self._next_numbered_path(self.sport, "bug") if add_template else primary
      )
    elif mode == "sports" or target_type == "scoreboard":
      primary = f"templates/{self.sport}_scoreboard.png"
      self.template_path = (
          self._next_numbered_path(self.sport, "scoreboard") if add_template else primary
      )
    else:
      self.template_path = "templates/ad_badge.png"

    self.qscreen = qscreen

    self.start_point: Optional[QPoint] = None
    self.end_point: Optional[QPoint] = None
    self.is_selecting = False
    self.selected_roi: Optional[QRect] = None

    self._setup_window()
    self._prepare_background_pixmap()

  @staticmethod
  def _next_numbered_path(sport: str, kind: str) -> str:
    """templates/{sport}_{kind}.png, then _2, _3, ... for extra channel looks."""
    primary = f"templates/{sport}_{kind}.png"
    if not os.path.exists(primary):
      return primary
    idx = 2
    while os.path.exists(f"templates/{sport}_{kind}_{idx}.png"):
      idx += 1
    return f"templates/{sport}_{kind}_{idx}.png"

  def _setup_window(self) -> None:
    """Configure calibration window attributes."""
    self.setWindowFlags(
        Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
    )
    if self.qscreen:
      self.setScreen(self.qscreen)
      self.setGeometry(self.qscreen.geometry())
    else:
      self.setGeometry(self.x, self.y, self.w, self.h)
    self.setMouseTracking(True)

  def _prepare_background_pixmap(self) -> None:
    """Convert screenshot numpy array to QPixmap for rendering."""
    h, w, ch = self.screenshot_bgr.shape
    bytes_per_line = ch * w
    # Convert BGR to RGB
    rgb = cv2.cvtColor(self.screenshot_bgr, cv2.COLOR_BGR2RGB)
    q_img = QImage(
        rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888
    ).copy()
    self.bg_pixmap = QPixmap.fromImage(q_img)

  def mousePressEvent(self, event) -> None:
    """Handle mouse down to begin rectangle selection."""
    if event.button() == Qt.MouseButton.LeftButton:
      self.start_point = event.pos()
      self.end_point = event.pos()
      self.is_selecting = True
      self.update()

  def mouseMoveEvent(self, event) -> None:
    """Handle mouse drag to update selection rectangle."""
    if self.is_selecting:
      self.end_point = event.pos()
      self.update()

  def mouseReleaseEvent(self, event) -> None:
    """Handle mouse up to finalize selection rectangle."""
    if event.button() == Qt.MouseButton.LeftButton and self.is_selecting:
      self.end_point = event.pos()
      self.is_selecting = False
      if self.start_point and self.end_point:
        self.selected_roi = QRect(self.start_point, self.end_point).normalized()
      self.update()

  def keyPressEvent(self, event) -> None:
    """Handle keyboard navigation: Enter to save, Esc to exit without saving."""
    if event.key() == Qt.Key.Key_C and (
        event.modifiers() & Qt.KeyboardModifier.ControlModifier
    ):
      print("\n[CALIBRATION] Canceled by Ctrl+C.")
      QApplication.quit()
    elif (
        event.key() == Qt.Key.Key_Return
        or event.key() == Qt.Key.Key_Enter
        or event.key() == Qt.Key.Key_S
    ):
      self.save_selection_and_exit()
    elif event.key() == Qt.Key.Key_Escape:
      print("\n[CALIBRATION] Canceled by user.")
      self.close()

  def paintEvent(self, event) -> None:
    """Paint the captured screen, dark translucent overlay, and selection rectangle."""
    painter = QPainter(self)

    # 1. Draw the screenshot scaled to window geometry (fixes High-DPI 125%/150% zoom-in)
    if hasattr(self, "bg_pixmap"):
      painter.drawPixmap(self.rect(), self.bg_pixmap)

    # 2. Draw slight dark tint over unselected area
    painter.fillRect(self.rect(), QColor(0, 0, 0, 90))

    # Instructions stay at the BOTTOM and hide while dragging so top-corner
    # bugs / watermarks are never covered by the header.
    show_banner = not self.is_selecting
    banner_h = 56
    banner_y = max(0, self.height() - banner_h)

    if self.target_type == "broadcast_bug":
      title = f"BROADCAST BUG CALIBRATION ({self.monitor_name})"
      instructions = (
          "Drag a box around the top-corner watermark / network logo. "
          "Press [Enter] or [S] to save, [Esc] to cancel."
      )
    elif self.mode == "sports" or self.target_type == "scoreboard":
      title = f"SCOREBOARD CALIBRATION ({self.monitor_name})"
      instructions = (
          "Drag a box around the scoreboard. Press [Enter] or [S] to save, "
          "[Esc] to cancel."
      )
    else:
      title = f"AD BADGE CALIBRATION ({self.monitor_name})"
      instructions = (
          "Drag a box around Hulu's Ad badge. Press [Enter] or [S] to save, "
          "[Esc] to cancel."
      )

    if show_banner:
      painter.fillRect(0, banner_y, self.width(), banner_h, QColor(20, 20, 25, 210))
      painter.setPen(QColor("#00FF88"))
      painter.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
      painter.drawText(20, banner_y + 22, title)
      painter.setPen(QColor("#FFFFFF"))
      painter.setFont(QFont("Segoe UI", 10))
      painter.drawText(20, banner_y + 44, instructions)

    # 3b. Check if entire captured screen is black (Hardware Acceleration DRM protection)
    if hasattr(self, "screenshot_bgr") and float(np.mean(self.screenshot_bgr)) < 2.0:
      box_w, box_h = 750, 160
      bx = (self.width() - box_w) // 2
      by = (self.height() - box_h) // 2
      painter.fillRect(bx, by, box_w, box_h, QColor(200, 30, 30, 240))
      painter.setPen(QColor("#FFFFFF"))
      painter.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
      painter.drawText(bx + 30, by + 40, "DRM BLACK SCREEN DETECTED (Hardware Acceleration is ON)")
      painter.setFont(QFont("Segoe UI", 11))
      painter.drawText(bx + 30, by + 75, "1. In Brave or Chrome, open: brave://settings/system")
      painter.drawText(bx + 30, by + 105, "2. Turn OFF 'Use graphics acceleration when available'")
      painter.drawText(bx + 30, by + 135, "3. Click 'Relaunch' and re-run calibration to see your game!")

    # 4. Draw active selection rectangle
    curr_rect = None
    if self.is_selecting and self.start_point and self.end_point:
      curr_rect = QRect(self.start_point, self.end_point).normalized()
    elif self.selected_roi:
      curr_rect = self.selected_roi

    if curr_rect and curr_rect.width() > 3 and curr_rect.height() > 3:
      painter.setCompositionMode(
          QPainter.CompositionMode.CompositionMode_SourceOver
      )
      painter.fillRect(curr_rect, QColor(0, 255, 136, 40))

      pen = QPen(QColor("#00FF88"), 2, Qt.PenStyle.DashLine)
      painter.setPen(pen)
      painter.drawRect(curr_rect)

      scale_x = self.screenshot_bgr.shape[1] / max(1, self.width())
      scale_y = self.screenshot_bgr.shape[0] / max(1, self.height())
      phys_w_preview = int(curr_rect.width() * scale_x)
      phys_h_preview = int(curr_rect.height() * scale_y)
      coord_text = f"ROI: {phys_w_preview}x{phys_h_preview} px"
      pill_y = curr_rect.y() - 26 if curr_rect.y() >= 30 else curr_rect.y() + curr_rect.height() + 5
      painter.fillRect(
          curr_rect.x(),
          pill_y,
          len(coord_text) * 9 + 10,
          22,
          QColor(0, 0, 0, 200),
      )
      painter.setPen(QColor("#00FF88"))
      painter.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
      painter.drawText(
          curr_rect.x() + 5, pill_y + 15, coord_text
      )

  def save_selection_and_exit(self) -> None:
    """Save the cropped template image and update config.json with new ROI."""
    if not self.selected_roi or self.selected_roi.width() < 5 or self.selected_roi.height() < 5:
      print("\n[!] Please click and drag to select a region first.")
      return

    rx = self.selected_roi.x()
    ry = self.selected_roi.y()
    rw = self.selected_roi.width()
    rh = self.selected_roi.height()

    # Map logical window coordinates to physical screenshot pixels (High-DPI 125%/150% scaling)
    scale_x = self.screenshot_bgr.shape[1] / max(1, self.width())
    scale_y = self.screenshot_bgr.shape[0] / max(1, self.height())

    phys_rx = int(round(rx * scale_x))
    phys_ry = int(round(ry * scale_y))
    phys_rw = int(round(rw * scale_x))
    phys_rh = int(round(rh * scale_y))

    img_h, img_w = self.screenshot_bgr.shape[:2]
    phys_rx = max(0, min(img_w - 10, phys_rx))
    phys_ry = max(0, min(img_h - 10, phys_ry))
    phys_rw = max(10, min(img_w - phys_rx, phys_rw))
    phys_rh = max(10, min(img_h - phys_ry, phys_rh))

    crop = self.screenshot_bgr[phys_ry : phys_ry + phys_rh, phys_rx : phys_rx + phys_rw]

    # Validate crop is not pure black
    if float(np.mean(crop)) < 1.0:
      print("\n" + "!" * 75)
      print("[!] ERROR: Selected template is PURE BLACK!")
      print("    Brave/Chrome Hardware Acceleration is preventing screen capture.")
      print("    Please disable Hardware Acceleration in brave://settings/system")
      print("    and relaunch Brave before capturing.")
      print("!" * 75 + "\n")
      return

    os.makedirs(os.path.dirname(self.template_path) or ".", exist_ok=True)
    cv2.imwrite(self.template_path, crop)
    print(f"\n[+] Saved template crop to: {self.template_path} ({phys_rw}x{phys_rh} px physical)")

    try:
      with open(self.config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    except Exception:
      cfg = {}

    cfg["detection_mode"] = self.mode

    if self.mode == "sports":
      if "sports_profiles" not in cfg:
        cfg["sports_profiles"] = {}
      if self.sport not in cfg["sports_profiles"]:
        cfg["sports_profiles"][self.sport] = {
            "name": self.sport.capitalize(),
        }
      sp_profile = cfg["sports_profiles"][self.sport]

      if "sports_detection" not in cfg:
        cfg["sports_detection"] = {}
      cfg["sports_detection"]["enabled"] = True
      
      sp_profile["calibrated_frame"] = {
          "width": img_w,
          "height": img_h,
      }
      cfg["sports_detection"]["calibrated_frame"] = {
          "width": img_w,
          "height": img_h,
      }

      if self.target_type == "broadcast_bug":
        cfg["sports_detection"]["broadcast_bug_enabled"] = True
        sp_profile["broadcast_bug_enabled"] = True
        sp_profile["broadcast_bug_threshold"] = float(
            sp_profile.get("broadcast_bug_threshold", 0.60)
        )
        if self.add_template and os.path.normpath(
            sp_profile.get("broadcast_bug_template_path", "")
        ) not in ("", os.path.normpath(self.template_path)):
          extras = list(sp_profile.get("broadcast_bug_extra_templates") or [])
          if self.template_path not in extras:
            extras.append(self.template_path)
          sp_profile["broadcast_bug_extra_templates"] = extras
          print(
              f"[+] Added extra Broadcast Bug template for '{self.sport}': {self.template_path}"
          )
        else:
          cfg["sports_detection"]["broadcast_bug_template_path"] = self.template_path
          cfg["sports_detection"]["broadcast_bug_roi"] = {
              "left": phys_rx,
              "top": phys_ry,
              "width": phys_rw,
              "height": phys_rh,
          }
          sp_profile["broadcast_bug_template_path"] = self.template_path
          sp_profile["broadcast_bug_roi"] = {
              "left": phys_rx,
              "top": phys_ry,
              "width": phys_rw,
              "height": phys_rh,
          }
          print(
              f"[+] Configured Broadcast Bug ROI for '{self.sport}': X={phys_rx}, Y={phys_ry}, {phys_rw}x{phys_rh} physical"
          )
      else:
        if self.add_template and os.path.normpath(
            sp_profile.get("scoreboard_template_path", "")
        ) not in ("", os.path.normpath(self.template_path)):
          extras = list(sp_profile.get("scoreboard_extra_templates") or [])
          if self.template_path not in extras:
            extras.append(self.template_path)
          sp_profile["scoreboard_extra_templates"] = extras
          print(
              f"[+] Added extra Scoreboard template for '{self.sport}': {self.template_path}"
          )
        else:
          cfg["sports_detection"]["template_path"] = self.template_path
          cfg["sports_detection"]["scoreboard_roi"] = {
              "left": phys_rx,
              "top": phys_ry,
              "width": phys_rw,
              "height": phys_rh,
          }
          sp_profile["scoreboard_template_path"] = self.template_path
          sp_profile["scoreboard_roi"] = {
              "left": phys_rx,
              "top": phys_ry,
              "width": phys_rw,
              "height": phys_rh,
          }
          sp_profile["match_threshold"] = 0.60
          print(
              f"[+] Configured Scoreboard ROI for '{self.sport}': X={phys_rx}, Y={phys_ry}, {phys_rw}x{phys_rh} physical"
          )

      if "match_threshold" not in cfg["sports_detection"]:
        cfg["sports_detection"]["match_threshold"] = 0.60
      if "search_padding" not in cfg["sports_detection"]:
        cfg["sports_detection"]["search_padding"] = 50
      if "disappear_debounce_seconds" not in cfg["sports_detection"]:
        cfg["sports_detection"]["disappear_debounce_seconds"] = 0.0
      if "reappear_debounce_frames" not in cfg["sports_detection"]:
        cfg["sports_detection"]["reappear_debounce_frames"] = 1
    else:
      if "visual_detection" not in cfg:
        cfg["visual_detection"] = {}
      cfg["visual_detection"]["badge_detection_enabled"] = True
      cfg["visual_detection"]["badge_roi"] = {
          "left": phys_rx,
          "top": phys_ry,
          "width": phys_rw,
          "height": phys_rh,
      }
      cfg["visual_detection"]["template_path"] = self.template_path
      print(f"[+] Configured Ad Badge ROI: X={phys_rx}, Y={phys_ry}, {phys_rw}x{phys_rh} physical")

    with open(self.config_path, "w", encoding="utf-8") as f:
      json.dump(cfg, f, indent=2)

    print("[+] Calibration saved successfully! Exiting calibration mode...")
    self.close()
