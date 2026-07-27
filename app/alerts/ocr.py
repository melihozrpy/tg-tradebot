from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError


class OCRUnavailableError(RuntimeError):
    pass


class InvalidAlarmImageError(ValueError):
    pass


@dataclass(frozen=True)
class OCRResult:
    text: str
    confidence: float
    passes: int
    warnings: tuple[str, ...] = ()


def validate_alarm_image(content: bytes, *, maximum_bytes: int = 10 * 1024 * 1024) -> Image.Image:
    if not content or len(content) > maximum_bytes:
        raise InvalidAlarmImageError("Görsel boş veya izin verilen boyutu aşıyor.")
    try:
        image = Image.open(io.BytesIO(content))
        image.verify()
        image = Image.open(io.BytesIO(content))
        image = ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidAlarmImageError("Dosya geçerli bir JPG, PNG veya WEBP görseli değil.") from exc
    if image.width * image.height > 40_000_000:
        raise InvalidAlarmImageError("Görsel çözünürlüğü güvenli sınırı aşıyor.")
    return image


def preprocessing_variants(image: Image.Image) -> tuple[Image.Image, ...]:
    scale = max(1, min(3, 1800 // max(image.width, 1)))
    resized = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(resized)
    contrast = ImageEnhance.Contrast(gray).enhance(2.0)
    sharpened = contrast.filter(ImageFilter.UnsharpMask(radius=2, percent=170, threshold=3))
    threshold = sharpened.point(lambda p: 255 if p > 155 else 0)
    inverted = ImageOps.invert(threshold)
    return resized, sharpened, threshold, inverted


def extract_alarm_text(content: bytes, *, language: str = "tur+eng",
                       ocr_engine: Callable | None = None, maximum_bytes: int = 10 * 1024 * 1024) -> OCRResult:
    image = validate_alarm_image(content, maximum_bytes=maximum_bytes)
    if ocr_engine is None:
        try:
            import pytesseract
        except ImportError as exc:
            raise OCRUnavailableError(
                "Yerel OCR kurulu değil. Manuel veya toplu metin alarmı çalışmaya devam eder."
            ) from exc
        def ocr_engine(value):
            return pytesseract.image_to_data(value, lang=language, config="--psm 6",
                                              output_type=pytesseract.Output.DICT)
    candidates: list[tuple[str, float]] = []
    for variant in preprocessing_variants(image):
        payload = ocr_engine(variant)
        if isinstance(payload, str):
            candidates.append((payload.strip(), 0.70 if payload.strip() else 0.0)); continue
        words, confidences = [], []
        for word, confidence in zip(payload.get("text", []), payload.get("conf", [])):
            word = str(word).strip()
            try: score = float(confidence)
            except (TypeError, ValueError): score = -1
            if word:
                words.append(word)
                if score >= 0: confidences.append(score)
        text = " ".join(words)
        candidates.append((text, (sum(confidences) / len(confidences) / 100) if confidences else 0.0))
    best_text, best_confidence = max(candidates, key=lambda item: (len(item[0]), item[1]), default=("", 0.0))
    if not best_text:
        raise InvalidAlarmImageError("Görselden okunabilir alarm satırı çıkarılamadı.")
    warnings = ("Düşük güvenli OCR satırlarını mutlaka düzeltin.",) if best_confidence < .80 else ()
    return OCRResult(best_text, round(best_confidence, 3), len(candidates), warnings)
