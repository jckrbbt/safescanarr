"""
safescanarr/nudenet_scanner.py

Wraps NudeNet to analyse a contact sheet image.
Returns a result dict with:
  flagged    bool   — True if any label exceeds the confidence threshold
  labels     list   — list of {label, confidence} dicts that were detected
  max_conf   float  — highest confidence score found (0 if nothing detected)
  error      str    — set if NudeNet failed to run
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Labels we consider NSFW — NudeNet v3 label names
NSFW_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
    "ARMPITS_EXPOSED",   # optional — remove if too sensitive
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


def analyse(image_path: str, threshold: float = 0.6) -> dict:
    """
    Analyse *image_path* with NudeNet.
    Returns a result dict (see module docstring).
    """
    result = {
        "flagged":  False,
        "labels":   [],
        "max_conf": 0.0,
        "error":    None,
    }

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
            log.info("NudeNet flagged %s — labels: %s", image_path, hits)
        else:
            log.debug("NudeNet clean: %s", image_path)

    except Exception as e:
        log.error("NudeNet error for %s: %s", image_path, e)
        result["error"] = str(e)

    return result
