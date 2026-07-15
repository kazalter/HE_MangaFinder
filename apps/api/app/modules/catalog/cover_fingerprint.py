import math
import statistics
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageOps

FINGERPRINT_VERSION = 2
_CROP_RATIOS = (0.75, 1.0, 4 / 3, 16 / 9)
_RATIO_TOLERANCE = 0.09
_DCT_SIZE = 32
_DCT_COSINES = [
    [
        math.cos(math.pi * (2 * position + 1) * frequency / (2 * _DCT_SIZE))
        for position in range(_DCT_SIZE)
    ]
    for frequency in range(8)
]


@dataclass(frozen=True)
class CoverComparison:
    distance: int
    mode: str
    legacy_distance: int | None = None
    reliable_negative: bool = False


def hash_distance(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


def fingerprint_image(image: Image.Image) -> dict[str, Any]:
    normalized = ImageOps.exif_transpose(image).convert("RGB")
    width, height = normalized.size
    if width < 8 or height < 8:
        raise ValueError("封面尺寸过小")
    variants = [_variant(normalized, "full", width / height)]
    seen_boxes: set[tuple[int, int, int, int]] = {(0, 0, width, height)}
    for ratio in _CROP_RATIOS:
        for position, box in _crop_boxes(width, height, ratio):
            if box in seen_boxes:
                continue
            seen_boxes.add(box)
            variants.append(
                _variant(normalized.crop(box), f"crop:{ratio:.4f}:{position}", ratio)
            )
    return {
        "version": FINGERPRINT_VERSION,
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 5),
        "variants": variants,
    }


def compare_fingerprints(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    *,
    left_legacy: str | None = None,
    right_legacy: str | None = None,
) -> CoverComparison | None:
    legacy = hash_distance(left_legacy, right_legacy)
    if not is_current_fingerprint(left) or not is_current_fingerprint(right):
        if legacy is None:
            return None
        # Legacy dHash remains useful as positive evidence, but is too crop-sensitive
        # to support a negative conclusion.
        return CoverComparison(legacy, "legacy", legacy, False)

    comparisons: list[tuple[int, str, int, int, int]] = []
    for left_variant in left.get("variants", []):
        for right_variant in right.get("variants", []):
            if not _ratios_match(left_variant, right_variant):
                continue
            dhash = hash_distance(left_variant.get("dhash"), right_variant.get("dhash"))
            phash = hash_distance(left_variant.get("phash"), right_variant.get("phash"))
            if dhash is None or phash is None:
                continue
            histogram = _histogram_distance(
                left_variant.get("histogram"), right_variant.get("histogram")
            )
            distance = round(0.34 * dhash + 0.46 * phash + 0.20 * histogram)
            mode = (
                "full"
                if left_variant.get("kind") == right_variant.get("kind") == "full"
                else "crop"
            )
            comparisons.append((distance, mode, dhash, phash, histogram))
    if not comparisons:
        if legacy is None:
            return None
        return CoverComparison(legacy, "legacy", legacy, False)

    distance, mode, dhash, phash, histogram = min(
        comparisons,
        key=lambda item: (item[0], max(item[2], item[3]), item[4]),
    )
    # A low blended score must still be supported by both structural hashes.
    if max(dhash, phash) > 18:
        distance = max(distance, 14)
    return CoverComparison(
        distance=max(0, min(64, distance)),
        mode=mode,
        legacy_distance=legacy,
        reliable_negative=True,
    )


def _variant(image: Image.Image, kind: str, ratio: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "ratio": round(ratio, 5),
        "dhash": _dhash(image),
        "phash": _phash(image),
        "histogram": _color_histogram(image),
    }


def _crop_boxes(
    width: int, height: int, target_ratio: float
) -> list[tuple[str, tuple[int, int, int, int]]]:
    ratio = width / height
    if abs(math.log(ratio / target_ratio)) <= 0.025:
        return [("center", (0, 0, width, height))]
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    if ratio > target_ratio:
        crop_width = max(8, min(width, round(height * target_ratio)))
        available = width - crop_width
        offsets = (0, available // 2, available)
        for name, left in zip(("start", "center", "end"), offsets, strict=True):
            boxes.append((name, (left, 0, left + crop_width, height)))
    else:
        crop_height = max(8, min(height, round(width / target_ratio)))
        available = height - crop_height
        offsets = (0, available // 2, available)
        for name, top in zip(("start", "center", "end"), offsets, strict=True):
            boxes.append((name, (0, top, width, top + crop_height)))
    return boxes


def _dhash(image: Image.Image) -> str:
    gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = _pixels(gray)
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | int(
                pixels[row * 9 + column] > pixels[row * 9 + column + 1]
            )
    return f"{bits:016x}"


def _phash(image: Image.Image) -> str:
    size = _DCT_SIZE
    gray = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = _pixels(gray)
    row_dct = [
        [
            sum(
                pixels[row * size + column] * _DCT_COSINES[u][column]
                for column in range(size)
            )
            for row in range(size)
        ]
        for u in range(8)
    ]
    coefficients = [
        sum(row_dct[u][row] * _DCT_COSINES[v][row] for row in range(size))
        for v in range(8)
        for u in range(8)
    ]
    threshold = statistics.median(coefficients[1:])
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient > threshold)
    return f"{bits:016x}"


def _color_histogram(image: Image.Image) -> list[int]:
    sample = image.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
    channels = sample.split()
    result: list[int] = []
    for channel in channels:
        histogram = channel.histogram()
        bins = [sum(histogram[index : index + 32]) for index in range(0, 256, 32)]
        total = sum(bins) or 1
        result.extend(round(value * 255 / total) for value in bins)
    return result


def _pixels(image: Image.Image) -> list[int]:
    get_pixels = getattr(image, "get_flattened_data", image.getdata)
    return list(get_pixels())


def _histogram_distance(left: object, right: object) -> int:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return 32
    try:
        difference = sum(abs(int(a) - int(b)) for a, b in zip(left, right, strict=True))
    except (TypeError, ValueError):
        return 32
    # Three normalized channels, each with a maximum L1 distance of 510.
    return max(0, min(64, round(difference * 64 / 1530)))


def _ratios_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        return abs(math.log(float(left["ratio"]) / float(right["ratio"]))) <= _RATIO_TOLERANCE
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def is_current_fingerprint(value: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("version") == FINGERPRINT_VERSION
        and isinstance(value.get("variants"), list)
        and value["variants"]
    )
