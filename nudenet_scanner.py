"""
safescanarr/nudenet_scanner.py

Analyses a video file for NSFW content by extracting sample frames
using ffmpeg and running NudeNet on each full-resolution frame.

Returns a result dict with:
  flagged    bool   — True if any frame/label exceeds the confidence threshold
  labels     list   — list of {label, confidence, frame} dicts that were detected
  max_conf   float  — highest confidence score found (0 if nothing detected)
  error      str    — set if analysis failed
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Labels we consider NSFW — NudeNet v3 label names
NSFW_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    # Excluded — too many false positives for kids library:
    # "BUTTOCKS_EXPOSED"
    # "MALE_BREAST_EXPOSED"
    # "ARMPITS_EXPOSED"
}

_detector = None


def _get_detector():
    """Lazy-load the NudeDetector so startup isn't slowed down."""
    global _detector
    if _detector is None:
        try:
            from nudenet import NudeDetector
            _detector = NudeDetector()
            log.info("NudeNet detector loaded")
        except ImportError:
            log.error("nudenet is not installed — pip install nudenet")
            raise
    return _detector


def _get_video_duration(video_path: str) -> float:
    """Use ffprobe to get video duration in seconds."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                video_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        import json
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception as e:
        log.warning("Could not get duration for %s: %s", video_path, e)
        return 0.0


def _extract_frames(video_path: str, num_frames: int, tmpdir: str) -> list:
    """
    Extract *num_frames* evenly spaced frames from the video using ffmpeg.
    Returns list of frame file paths.
    """
    duration = _get_video_duration(video_path)
    if duration <= 0:
        # Fall back to extracting frames by interval without knowing duration
        duration = 3600  # assume up to 1 hour

    # Avoid the very start and end (often black frames / credits)
    start    = duration * 0.05
    end      = duration * 0.95
    interval = (end - start) / max(num_frames - 1, 1)

    frames = []
    for i in range(num_frames):
        timestamp = start + i * interval
        out_path  = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-ss", str(timestamp),
                    "-i", video_path,
                    "-frames:v", "1",
                    "-q:v", "2",        # high quality JPEG
                    "-y", out_path,
                ],
                capture_output=True, timeout=30,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                frames.append(out_path)
        except Exception as e:
            log.warning("Frame extraction failed at %.1fs: %s", timestamp, e)

    return frames


def analyse_video(video_path: str, threshold: float = 0.6,
                  num_frames: int = 10) -> dict:
    """
    Analyse *video_path* by extracting *num_frames* full-resolution frames
    and running NudeNet on each one.

    Returns a result dict (see module docstring).
    """
    result = {
        "flagged":  False,
        "labels":   [],
        "max_conf": 0.0,
        "error":    None,
    }

    if not Path(video_path).exists():
        result["error"] = f"Video not found: {video_path}"
        return result

    try:
        detector = _get_detector()
    except Exception as e:
        result["error"] = str(e)
        return result

    with tempfile.TemporaryDirectory() as tmpdir:
        log.info("NudeNet: extracting %d frames from %s", num_frames, video_path)
        frames = _extract_frames(video_path, num_frames, tmpdir)

        if not frames:
            log.warning("NudeNet: no frames extracted from %s", video_path)
            result["error"] = "No frames could be extracted"
            return result

        log.info("NudeNet: analysing %d frames", len(frames))
        hits = []

        for frame_path in frames:
            try:
                detections = detector.detect(frame_path)
                for det in detections:
                    label = det.get("class", "")
                    conf  = float(det.get("score", 0))
                    if label in NSFW_LABELS and conf >= threshold:
                        frame_num = Path(frame_path).stem
                        hits.append({
                            "label":      label,
                            "confidence": round(conf, 3),
                            "frame":      frame_num,
                        })
                        log.info("NudeNet hit: %s %.0f%% in %s",
                                 label, conf * 100, frame_num)
            except Exception as e:
                log.warning("NudeNet error on frame %s: %s", frame_path, e)

        if hits:
            result["flagged"]  = True
            result["labels"]   = hits
            result["max_conf"] = max(h["confidence"] for h in hits)
            log.warning("NudeNet FLAGGED %s — %d hit(s), max confidence %.0f%%",
                        video_path, len(hits), result["max_conf"] * 100)
        else:
            log.info("NudeNet clean: %s (%d frames checked)", video_path, len(frames))

    return result


# Keep old image-based analyse for any direct sheet checks
def analyse(image_path: str, threshold: float = 0.6) -> dict:
    """Analyse a single image (kept for compatibility)."""
    result = {"flagged": False, "labels": [], "max_conf": 0.0, "error": None}
    if not Path(image_path).exists():
        result["error"] = f"Image not found: {image_path}"
        return result
    try:
        detector   = _get_detector()
        detections = detector.detect(image_path)
        hits = []
        for det in detections:
            label = det.get("class", "")
            conf  = float(det.get("score", 0))
            if label in NSFW_LABELS and conf >= threshold:
                hits.append({"label": label, "confidence": round(conf, 3)})
        if hits:
            result["flagged"]  = True
            result["labels"]   = hits
            result["max_conf"] = max(h["confidence"] for h in hits)
    except Exception as e:
        result["error"] = str(e)
    return result
