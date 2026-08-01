from __future__ import annotations

import math
import os
import struct
import tempfile
import wave

SOUND_CHOICES = {"zil", "radar", "acil"}


def normalize_sound(value: str | None) -> str:
    sound = (value or "zil").strip().casefold()
    return sound if sound in SOUND_CHOICES else "zil"


def generate_alarm_wav(sound: str = "zil") -> str:
    """Harici ses dosyası gerektirmeden kısa Telegram alarm sesi üretir."""
    sound = normalize_sound(sound)
    patterns = {
        "zil": [(880, .22), (1175, .28)],
        "radar": [(660, .16), (0, .12), (660, .16), (0, .12), (880, .22)],
        "acil": [(980, .18), (620, .18), (980, .18), (620, .18), (980, .25)],
    }
    rate = 22050
    path = os.path.join(tempfile.gettempdir(), f"mergen_alarm_{sound}.wav")
    with wave.open(path, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        for frequency, duration in patterns[sound]:
            count = int(rate * duration)
            for index in range(count):
                envelope = min(1.0, index / (rate * .015), (count - index) / (rate * .03))
                value = 0 if frequency == 0 else int(15000 * envelope * math.sin(2 * math.pi * frequency * index / rate))
                wav.writeframesraw(struct.pack("<h", value))
    return path


async def send_alarm(bot, chat_id: int, text: str, sound: str = "zil") -> None:
    await bot.send_message(chat_id=chat_id, text=text)
    path = generate_alarm_wav(sound)
    with open(path, "rb") as audio:
        await bot.send_audio(chat_id=chat_id, audio=audio, title=f"MONTANA FİNANS ROBOTU alarmı • {normalize_sound(sound)}")
