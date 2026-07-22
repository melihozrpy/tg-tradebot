from pathlib import Path

from app.telegram.handlers_stage5g import _run_keyboard


def test_68_stage5g_uses_buttons_for_details_close_and_cancel():
    keyboard = _run_keyboard("btjob_example")
    callback_data = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert any("btmetric" in item for item in callback_data)
    assert any("bttrades" in item for item in callback_data)
    source = Path("app/telegram/handlers_stage5g.py").read_text(encoding="utf-8")
    assert "stage5g_paper_closeconfirm_" in source
    assert "stage5g_btcancel_" in source
