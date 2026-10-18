# Live Sports & Streaming Ad Blocker (TV Blackout & Browser Mute)

A production-grade, high-performance Windows 11 desktop automation tool that monitors live sports broadcasts or streaming shows on your TV (**Monitor 3 / TCL 55R617**), automatically mutes the target browser process (`chrome.exe`, `brave.exe`, `msedge.exe`), and displays a pure black (`#000000`), borderless, stay-on-top fullscreen blackout overlay whenever commercial breaks occur.

---

## Detection Modes

### 1. Dual-Anchor Sports Keep-Alive Mode (Default)
In live sports (baseball, football, basketball, hockey), broadcasts do **not** use standard on-demand "Ad" badges.
Instead, this mode uses **Dual-Anchor Keep-Alive Detection**:
- **Dual Anchors**: Tracks both the **Scoreboard** (e.g. at the bottom right) and the persistent **Broadcast Watermark / Bug** (e.g. `'FOX NLCS GAME 1'` or network logo in the top right).
- **Game Active**: While the broadcast is already visible, either the scoreboard or broadcast watermark can keep it visible (including replays when the scoreboard disappears). After a confirmed bug match, lower bug similarity scores (default `0.55`) and brief misses (default `0.5` seconds) are tolerated. A bug score of at least `0.60` also holds the visible picture for up to 15 seconds after a scoreboard score of at least `0.90`.
- **Commercial Blackout**: During commercial breaks, BOTH anchors must be absent for `disappear_debounce_seconds` before the TV blacks out and the browser is muted. The default `0.1` seconds helps avoid brief black flashes while keeping the delay short.
- **Confirmed Recovery**: After blackout begins, only the scoreboard (or football scoreboard OCR) can restore video and audio. Strong scoreboard matches at or above `scoreboard_fast_recovery_threshold` (default `0.85`) restore immediately; weaker matches require three consecutive frames. A bug-only match cannot reopen the screen because commercials can contain similar network graphics.
- **Frosted Acrylic Blur Cutouts**: Windows 11 hardware acrylic blur (`ACCENT_ENABLE_BLURBEHIND`) covers the inspection hole during commercial breaks, completely obscuring moving commercial ads from human vision while allowing OpenCV normalized template matching to detect the bright game logo when it returns.
- **Multi-Sport Profiles & Auto-Switching**: Seamlessly switch between Baseball and Football without recalibration. In `auto` mode, the blocker automatically senses which sport is playing and switches on the fly!

### 2. Ad Badge Mode
For standard on-demand TV shows and movies where an on-screen "Ad" or "Commercial Break" badge appears during ads.
- Blackout and muting engage when the badge is detected.
- Screen and audio restore when the badge clears.

---

## Multi-Sport Setup & Switching

Both **Baseball (MLB)** and **Football (NFL/CFB)** profiles are pre-configured.

### Auto-Detection (Default)
Run without extra flags:
```powershell
python main.py
```
When `active_sport` is set to `"auto"`, the engine monitors your game. If you change channels from a baseball game to a football game, it detects the football scoreboard/bug and automatically switches profiles in real time!

### Calibrating Football
When watching a football game on your TV:
```powershell
# 1. Calibrate the Football Scoreboard (usually bottom left or bottom banner)
python main.py --sport football --calibrate-scoreboard

# 2. Calibrate the Football Broadcast Bug / Network Logo (usually top corner)
python main.py --sport football --calibrate-bug
```
Once saved, you never have to calibrate it again! You can switch back and forth between baseball and football freely.

Broadcast bugs can differ by network, channel, and sport. Detection uses visual templates rather than reading the words, so substantially different graphics or changing text may not match automatically. Add another captured look for each variation:
```powershell
python main.py --sport baseball --calibrate-bug --add-template
```
Use `--sport football` for football. The extra template is saved alongside that sport's existing bug templates. Scoreboards can also be captured with `--calibrate-scoreboard --add-template`; rapidly changing scores or text can still make a whole-scoreboard image a less reliable anchor than a persistent channel bug.

Template confidence is visual similarity, not proof that a logo or bug is present. Matching uses `score >= threshold`, so a score exactly at the threshold still counts as a match. The baseball bug anchor is enabled with a `0.76` acquisition threshold and searches only near its calibrated location, rather than falling back to a broad top-of-screen scan. Once matched, the anchor can be held at `sports_detection.bug_hold_threshold` (default `0.55`) and briefly bridge misses for `sports_detection.bug_miss_grace_seconds` (default `0.5`). Replay fallback also holds the visible picture when the bug score is at least `sports_detection.replay_bug_threshold` (default `0.60`) for up to `sports_detection.bug_only_grace_seconds` (default `15`) after a scoreboard score of at least `sports_detection.replay_scoreboard_threshold` (default `0.90`). Bug-only matching cannot restore the picture after blackout, since the bug may resemble graphics shown during commercials. `sports_detection.reappear_debounce_frames` (default `3`) applies to normal scoreboard-based recovery. Strong scoreboard matches at or above `sports_detection.scoreboard_fast_recovery_threshold` (default `0.85`) restore immediately.

Automatic sport switching requires consecutive matches of the other sport's scoreboard (or football OCR), rather than switching on a single bug-template match. Configure the count with `sports_detection.auto_switch_confirm_frames` (default `3`). Alternate-sport checks start only after blackout is active, so slow OCR cannot delay the initial blackout; a genuine change to the other sport may therefore briefly black out before detection restores the picture.

The football profile also enables an OCR fallback: instead of matching a fixed scoreboard picture, it scans the top and bottom of the video for a game clock together with a period or down-and-distance cue. This is independent of team names, scores, and which corner the scorebug uses. The existing image templates remain as a fast first check. OCR scans at the interval configured by `sports_detection.scoreboard_ocr_interval_seconds` (default `0.2` seconds). It increases CPU use and is a heuristic rather than a guarantee; unusual graphics, tiny text, halftime layouts, or ads containing similar text can still cause misses or false matches. Install the OCR runtime and bundled English models with `python -m pip install -r requirements.txt`.

RapidOCR's repetitive warning that a scan found no text is suppressed because empty bands are expected during commercials. Other OCR warnings and errors remain enabled.

### Windowed Browser Position
Sports ROIs are mapped from the calibrated monitor image into the detected browser playback area. The browser's top chrome offset is estimated automatically by default. If the video is lower than the estimate, set `sports_detection.windowed_top_inset_px` in `config.json` to the pixel offset from the browser client area's top to the video area (for example, start around `100` and adjust using `--test-detect`). Set it to `null` to return to automatic estimation. This setting affects windowed playback; fullscreen detection ignores it.

### Calibrating Baseball
```powershell
python main.py --sport baseball --calibrate-scoreboard
python main.py --sport baseball --calibrate-bug
```

---

## Cutout Modes & Frosted Blur

To keep detection running while the TV is blacked out, the system uses inspection cutouts:
- **`bug_only`** *(Default & Recommended)*:
  Leaves only the small top-right watermark region unmasked and blurred with frosted acrylic glass. The large scoreboard area remains 100% solid pitch black! When the commercial ends, the watermark reappears and instantly restores your screen.
- **`both`**:
  Frosted blur over both the scoreboard and top-right watermark.
- **`scoreboard_only`**:
  Frosted blur over the scoreboard only.
- **`none`**:
  Pure black across the entire screen without cutouts.

You can set this in `config.json` under `"overlay": {"cutout_mode": "bug_only"}` or pass via CLI:
```powershell
python main.py --cutout-mode bug_only
```

---

## File Structure

```
hulu-blocker/
├── main.py            # Primary detection loop, debounce state machine, and Qt integration
├── audio_manager.py   # pycaw Core Audio wrapper for browser-specific muting & peak sensing
├── overlay.py         # PyQt6 borderless stay-on-top blackout window & calibration UI
├── config.json        # Geometry coordinates, thresholds, and target process settings
├── requirements.txt   # Production dependencies
└── templates/
    └── ad_badge.png   # Hulu "Ad" badge reference template (calibrated or default)
```

---

## Quickstart

Yes, you run all commands directly in your terminal (PowerShell, Command Prompt, or VS Code / Cursor terminal) inside the `C:\Users\PC\Documents\hulu-blocker` directory.

### 1. View Connected Monitors Catalog
Check how Windows and the program identify your displays:
```powershell
python main.py --list-monitors
```
*(Your 55" TCL TV is detected as `"55R617"` at index 2).*

### 2. Run Instant 5-Second Simulation Test
To immediately test the entire system without waiting for a commercial:
```powershell
python main.py --test-simulation
```
- Instantly mutes your browser (`brave.exe`, `chrome.exe`, `msedge.exe`).
- Covers your 55" TV with a pure black stay-on-top fullscreen overlay.
- Counts down 5 seconds.
- Demonstrates the 1.5-second hysteresis debounce hold.
- Automatically un-mutes your browser and removes the black overlay.

### 3. Run Detection Diagnostics (10 Frames Telemetry)
Test screen grabbing speed and audio meter hooks:
```powershell
python main.py --test-detect
```

### 4. Calibrate the Scoreboard on Your 55" TV
With your game (baseball, football, etc.) playing on your TV (on Hulu, Prime Video, ESPN, FOX, or anywhere):
```powershell
python main.py --calibration
```
*(or explicitly: `python main.py --calibrate-scoreboard`)*
- A freeze frame opens on your 55" TV.
- Click and drag a box around the on-screen **Scoreboard**, **Team Bug**, or **Score Bug** (e.g. the team abbreviations and score area).
- Press **[Enter]** or **[S]** to save the template to `templates/scoreboard.png` and update `config.json`.
- Press **[Esc]** to cancel.

### 5. Start the Automated Ad Blocker
```powershell
python main.py
```
*(You can also override the monitor target at any time, e.g. `python main.py --monitor 55R617` or `python main.py --monitor 2`).*

> [!TIP]
> **Browser Graphics Acceleration & DRM Note**:
> If your browser (Brave / Chrome / Edge) protects DRM video with Hardware Acceleration, Windows may return black pixels to screen capture tools. If you notice screen captures are black, toggle off hardware acceleration:
> `Brave/Chrome Settings -> System -> toggle off "Use graphics acceleration when available" -> Relaunch Browser`.

---

## Configuration (`config.json`)

```json
{
  "detection_mode": "sports",
  "target": {
    "monitor_target": "55R617",
    "monitor_index": 2,
    "browser_executables": [
      "chrome.exe",
      "brave.exe",
      "msedge.exe"
    ]
  },
  "capture": {
    "fps": 10
  },
  "sports_detection": {
    "enabled": true,
    "template_path": "templates/scoreboard.png",
    "scoreboard_roi": {
      "left": 60,
      "top": 1200,
      "width": 380,
      "height": 140
    },
    "match_threshold": 0.60,
    "search_padding": 50,
    "disappear_debounce_seconds": 0.1,
    "windowed_top_inset_px": null,
    "reappear_debounce_frames": 3,
    "scoreboard_fast_recovery_threshold": 0.85,
    "bug_only_grace_seconds": 15.0,
    "bug_hold_threshold": 0.55,
    "bug_miss_grace_seconds": 0.5,
    "replay_scoreboard_threshold": 0.90,
    "replay_bug_threshold": 0.60
  },
  "debounce": {
    "max_commercial_seconds": 240
  },
  "overlay": {
    "background_color": "#000000",
    "stay_on_top": true,
    "cutout_roi": true
  }
}
```

---

## Terminal Status Log Format

While active, `main.py` provides clean real-time status output:

- **Live Content**:
  ```
  [LIVE CONTENT] | Check: 24.90ms | FPS: 12.0 | Audio Peak: 0.016 | Sat: 35.3
  ```
- **Commercial Detected (Muted & Covered)**:
  ```
  [COMMERCIAL DETECTED - MUTED & COVERED] | Trigger: AD_BADGE (conf: 0.88) | Response: 22.40ms
  ```
- **Debounce State (Hysteresis Hold)**:
  ```
  [DEBOUNCING] Live candidate for 0.9s / 1.5s | Check: 21.15ms
  ```
- **Resumed Live Broadcast**:
  ```
  [LIVE CONTENT] Commercial break finished | Resumed live broadcast | Response: 23.10ms
  ```
