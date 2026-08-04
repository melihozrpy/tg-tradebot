from app.telegram.stock_ai_handlers import (
    _enforce_compact_ai_output,
    format_compact_ai_fallback,
)


def _context():
    return {
        "symbol": "THYAO",
        "compact_analysis": {
            "price": 320.0,
            "ema50": 310.0,
            "ema200": 290.0,
            "supertrend_direction": 1,
            "rsi": 58.0,
            "macd_histogram": 1.2,
            "adx": 27.0,
            "obv_rising": True,
            "relative_volume": 1.3,
            "zone": {
                "zone_low": 305.2,
                "zone_high": 308.5,
                "zone_kind": "OB",
                "direction": "LONG",
            },
            "staged_entry": {
                "status": "PENDING",
                "zone_low": 305.2,
                "zone_high": 308.5,
                "invalidation": 303.8,
                "levels": [
                    {"price": 308.5},
                    {"price": 306.85},
                    {"price": 305.2},
                ],
            },
            "confluence": {
                "confirmations": ["EMA", "Supertrend", "ADX", "OBV"],
                "minimum_required": 3,
            },
        },
    }


def test_compact_ai_fallback_has_fixed_8_to_12_line_contract() -> None:
    text = format_compact_ai_fallback(_context())
    lines = text.splitlines()
    assert 8 <= len(lines) <= 12
    assert "PENDING" in text
    assert "şu an entry YOK" in text
    assert "320.00" not in next(line for line in lines if "Senaryo:" in line)


def test_noncompliant_long_model_answer_is_replaced_by_verified_template() -> None:
    unsafe = "\n".join(["Kesin al ve garanti kazan"] * 20)
    output = _enforce_compact_ai_output(unsafe, _context())
    assert "📈 THYAO — AI Analiz" in output
    assert "garanti kazan" not in output
