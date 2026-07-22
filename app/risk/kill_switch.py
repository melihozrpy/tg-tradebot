from __future__ import annotations

from dataclasses import dataclass


class KillSwitchActiveError(Exception):
    """Kill switch aktifken islem/sinyal uretimi denenirse firlatilir."""


@dataclass
class KillSwitchState:
    active: bool = False
    reason: str = ""


class KillSwitch:
    """Basit, bellek-ici (in-memory) kill switch. Kalici durum DB'de User.kill_switch_active alaninda tutulur."""

    def __init__(self):
        self._state = KillSwitchState()

    def activate(self, reason: str = "manuel durdurma") -> None:
        self._state = KillSwitchState(active=True, reason=reason)

    def deactivate(self) -> None:
        self._state = KillSwitchState(active=False, reason="")

    @property
    def is_active(self) -> bool:
        return self._state.active

    def guard(self) -> None:
        if self._state.active:
            raise KillSwitchActiveError(
                f"Kill switch aktif ({self._state.reason}). Sadece durum ve yeniden etkinlestirme komutlari calisir."
            )
