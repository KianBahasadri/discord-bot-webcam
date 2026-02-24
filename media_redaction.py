#!/usr/bin/env python3
"""Helpers for blurring explicit content and visible PII in images."""

import json
import logging
import os
import base64
from typing import Any

import cv2
import requests


logger = logging.getLogger(__name__)


_DETECTION_PROMPT = (
    "Return a JSON array only. "
    "Each item must be an object with keys: box_2d and label. "
    "box_2d must be [y_min, x_min, y_max, x_max] with each value normalized 0..1000. "
    "Detect and include ONLY sensitive regions that should be censored in this image: "
    "(1) sexually explicit visual content (nudity/sexual acts), and "
    "(2) visible PII text such as phone numbers, credit/debit card numbers, API keys, access tokens, passwords, or secrets. "
    "Never include the bot-added timestamp overlay in the bottom-right corner; treat it as safe system text and ignore it. "
    "If none are present, return []. "
    "Do not include masks or extra keys."
)


def _truthy_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _find_json_array_substring(text: str) -> str | None:
    start = text.find("[")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


def _normalize_boxes(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []

    boxes: list[dict[str, Any]] = []
    for item in raw_items:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue

        box = item.get("box_2d")
        if not isinstance(box, list) or len(box) != 4:
            continue

        try:
            y0, x0, y1, x1 = [float(v) for v in box]
        except Exception:
            continue

        y0 = max(0.0, min(1000.0, y0))
        x0 = max(0.0, min(1000.0, x0))
        y1 = max(0.0, min(1000.0, y1))
        x1 = max(0.0, min(1000.0, x1))

        if y1 <= y0 or x1 <= x0:
            continue

        label = str(item.get("label") or "sensitive")
        boxes.append({"box_2d": [y0, x0, y1, x1], "label": label})

    return boxes


def _parse_boxes_from_text(text: str) -> tuple[list[dict[str, Any]], bool]:
    text = (text or "").strip()
    if not text:
        return [], False

    for candidate in (text, _find_json_array_substring(text)):
        if not candidate:
            continue
        try:
            raw = json.loads(candidate)
            if isinstance(raw, dict):
                for key in ("boxes", "items", "detections", "data"):
                    if key in raw:
                        raw = raw[key]
                        break
            boxes = _normalize_boxes(raw)
            return boxes, True
        except Exception:
            continue

    return [], False


def _compute_pixel_box(
    norm_box: list[float],
    width: int,
    height: int,
    padding_ratio: float,
) -> tuple[int, int, int, int] | None:
    y0, x0, y1, x1 = norm_box
    py0 = int(round(y0 / 1000.0 * height))
    px0 = int(round(x0 / 1000.0 * width))
    py1 = int(round(y1 / 1000.0 * height))
    px1 = int(round(x1 / 1000.0 * width))

    pad_x = int(round(max(0, px1 - px0) * padding_ratio))
    pad_y = int(round(max(0, py1 - py0) * padding_ratio))

    px0 = max(0, px0 - pad_x)
    py0 = max(0, py0 - pad_y)
    px1 = min(width, px1 + pad_x)
    py1 = min(height, py1 + pad_y)

    if px1 <= px0 or py1 <= py0:
        return None
    return px0, py0, px1, py1


def _blur_region_inplace(image: Any, x0: int, y0: int, x1: int, y1: int) -> None:
    region = image[y0:y1, x0:x1]
    if region.size == 0:
        return

    region_h, region_w = region.shape[:2]
    small_w = max(1, region_w // 12)
    small_h = max(1, region_h // 12)

    pixelated = cv2.resize(region, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
    pixelated = cv2.resize(pixelated, (region_w, region_h), interpolation=cv2.INTER_NEAREST)
    sigma = max(8.0, min(region_w, region_h) / 6.0)
    blurred = cv2.GaussianBlur(pixelated, (0, 0), sigmaX=sigma, sigmaY=sigma)
    image[y0:y1, x0:x1] = blurred


def _blur_full_frame_inplace(image: Any) -> None:
    h, w = image.shape[:2]
    _blur_region_inplace(image, 0, 0, w, h)


def _looks_like_bottom_right_timestamp(norm_box: list[float], width: int, height: int) -> bool:
    px = _compute_pixel_box(norm_box, width, height, padding_ratio=0.0)
    if px is None:
        return False

    x0, y0, x1, y1 = px
    bw = x1 - x0
    bh = y1 - y0
    if bw <= 0 or bh <= 0:
        return False

    # Timestamp overlay is typically a short, wide strip near the lower-right edge.
    near_bottom_right = x0 >= int(width * 0.60) and y0 >= int(height * 0.70)
    strip_like = bw >= int(width * 0.10) and bh <= int(height * 0.12) and bw >= (bh * 2)
    return near_bottom_right and strip_like


def redact_sensitive_media_inplace(image_path: str) -> dict[str, Any]:
    """Detect explicit/PII regions with Gemini and blur them in place.

    Returns metadata with keys: regions, full_blur, labels.
    Raises RuntimeError when the image cannot be safely processed.
    """

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for palantir redaction.")

    model = os.environ.get("OPENROUTER_REDACTION_MODEL")
    if not model:
        raise RuntimeError("OPENROUTER_REDACTION_MODEL is required for palantir redaction.")
    max_side = int(os.environ.get("WEBCAM_REDACTION_MAX_SIDE", "1280"))
    padding_ratio = float(os.environ.get("WEBCAM_REDACTION_PADDING_RATIO", "0.08"))
    min_side_px = int(os.environ.get("WEBCAM_REDACTION_MIN_BOX_SIDE", "12"))
    fail_closed = _truthy_env("WEBCAM_REDACTION_FAIL_CLOSED", True)
    protect_timestamp = _truthy_env("WEBCAM_REDACTION_PROTECT_TIMESTAMP", True)

    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"Failed to decode image for redaction: {image_path}")

    orig_h, orig_w = image.shape[:2]
    infer_image = image
    if max_side > 0 and max(orig_w, orig_h) > max_side:
        scale = float(max_side) / float(max(orig_w, orig_h))
        infer_w = max(1, int(round(orig_w * scale)))
        infer_h = max(1, int(round(orig_h * scale)))
        infer_image = cv2.resize(image, (infer_w, infer_h), interpolation=cv2.INTER_AREA)

    ok, encoded = cv2.imencode(".jpg", infer_image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise RuntimeError("Failed to encode image for Gemini redaction request.")

    image_data_url = "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://localhost"),
        "X-Title": os.environ.get("OPENROUTER_APP_NAME", "discord-webcam-redaction"),
    }
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _DETECTION_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                    {"type": "text", "text": "Detect sensitive regions and return only JSON array."},
                ],
            },
        ],
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:600]}")

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no choices.")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        else:
            text = str(content or "")

        boxes, parsed_ok = _parse_boxes_from_text(text)
        if not parsed_ok:
            raise RuntimeError(f"Failed to parse OpenRouter JSON response: {text[:600]}")
    except Exception as exc:
        if not fail_closed:
            raise RuntimeError(f"OpenRouter redaction request failed: {exc}") from exc
        logger.warning("OpenRouter redaction request failed; applying full-frame blur: %s", exc)
        _blur_full_frame_inplace(image)
        if not cv2.imwrite(image_path, image):
            raise RuntimeError("Failed to save redacted image after full-frame blur fallback.")
        return {"regions": 0, "full_blur": True, "labels": []}

    if not boxes:
        return {"regions": 0, "full_blur": False, "labels": []}

    applied = 0
    labels: list[str] = []
    for item in boxes:
        if protect_timestamp and _looks_like_bottom_right_timestamp(item["box_2d"], orig_w, orig_h):
            continue

        pixel_box = _compute_pixel_box(item["box_2d"], orig_w, orig_h, padding_ratio)
        if pixel_box is None:
            continue

        x0, y0, x1, y1 = pixel_box
        if (x1 - x0) < min_side_px or (y1 - y0) < min_side_px:
            continue

        _blur_region_inplace(image, x0, y0, x1, y1)
        applied += 1
        labels.append(str(item.get("label") or "sensitive"))

    if applied == 0 and fail_closed:
        _blur_full_frame_inplace(image)
        if not cv2.imwrite(image_path, image):
            raise RuntimeError("Failed to save redacted image after empty-box fallback.")
        return {"regions": 0, "full_blur": True, "labels": []}

    if not cv2.imwrite(image_path, image):
        raise RuntimeError("Failed to save redacted image after applying blur regions.")

    return {"regions": applied, "full_blur": False, "labels": labels}
