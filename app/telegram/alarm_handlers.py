from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.alerts.enums import AlarmMode, ImportSource, SoundMode
from app.alerts.imports import parse_csv_bytes, parse_xlsx_bytes
from app.alerts.messages import format_alarm_summary
from app.alerts.ocr import OCRUnavailableError, extract_alarm_text
from app.alerts.parser import normalize_symbol, parse_alarm_line, parse_bulk_text, parse_condition, parse_decimal
from app.alerts.repository import owned_alert, page_alerts
from app.alerts.schemas import AlarmDraft
from app.alerts.schemas import BulkParseResult
from app.alerts.service import (
    AlarmServiceError, DuplicateAlarmError, acknowledge_alarm, confirm_import, create_alarm,
    create_import_preview, delete_alarm, get_alarm_settings, pause_alarm, resume_alarm, snooze_alarm,
)
from app.config.settings import get_settings
from app.models.database import AlarmImportJob, AlarmImportRow, get_session_factory
from app.services.watchlist_service import get_or_create_user
from app.services.alarm_sound_service import SOUND_CHOICES, normalize_sound
from app.telegram.handlers import _reject_unauthorized


def _db_user(update: Update):
    db = get_session_factory()()
    settings = get_settings()
    user = get_or_create_user(
        db, update.effective_user.id, update.effective_user.id in settings.admin_ids,
        settings.default_total_capital,
    )
    return db, user


def _draft_payload(draft: AlarmDraft) -> dict:
    return {"symbol": draft.symbol, "target": str(draft.target_price), "condition": draft.condition.value,
            "mode": draft.mode.value, "repeat": draft.repeat_interval_seconds, "note": draft.note,
            "sound_mode": draft.sound_mode.value, "source": draft.source.value,
            "base_price": str(draft.base_price) if draft.base_price is not None else None,
            "percentage_value": str(draft.percentage_value) if draft.percentage_value is not None else None,
            "near_tolerance": str(draft.near_tolerance) if draft.near_tolerance is not None else None,
            "sound_name": draft.sound_name}


def _payload_draft(value: dict) -> AlarmDraft:
    from app.alerts.enums import AlarmCondition
    return AlarmDraft(value["symbol"], Decimal(value["target"]), AlarmCondition(value["condition"]),
                      AlarmMode(value["mode"]), int(value["repeat"]), value.get("note"),
                      SoundMode(value.get("sound_mode", "FIRST_TRIGGER")),
                      ImportSource(value.get("source", "TEXT")),
                      Decimal(value["base_price"]) if value.get("base_price") else None,
                      Decimal(value["percentage_value"]) if value.get("percentage_value") else None,
                      Decimal(value["near_tolerance"]) if value.get("near_tolerance") else None,
                      1.0, value.get("sound_name"))


def _preview_text(draft: AlarmDraft) -> str:
    operator = {"PRICE_GTE": "eşit veya üzerine çıkarsa", "PRICE_LTE": "eşit veya altına düşerse",
                "CROSS_UP": "aşağıdan yukarı keserse", "CROSS_DOWN": "yukarıdan aşağı keserse",
                "PRICE_NEAR": "hedefe yaklaşırsa"}.get(draft.condition.value, draft.condition.value)
    return (
        "🔔 ALARM ÖZETİ\n━━━━━━━━━━━━━━━━━━\n"
        f"Hisse: {draft.symbol}\nKoşul: Fiyat {draft.target_price:.2f} TL seviyesine {operator}\n"
        f"Mod: {draft.mode.value}\nTekrarlama: {draft.repeat_interval_seconds} saniye\n"
        "Durum: Onay bekliyor\n\n"
        "ℹ️ Telefon sesi Telegram ve cihaz bildirim ayarlarına bağlıdır; bot sessiz modu aşamaz."
    )


async def cmd_alarm_kur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    if not context.args:
        context.user_data["alarm_flow"] = "single_symbol"
        await update.message.reply_text("🔔 Tekli alarm kurulumu\n\nHisse sembolünü yaz: ASELS")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Kullanım: /alarm_kur ASELS 72.50 üstü")
        return
    try:
        draft = parse_alarm_line(" ".join(context.args))
    except ValueError as exc:
        await update.message.reply_text(f"❌ Alarm anlaşılamadı: {exc}")
        return
    context.user_data["pending_alarm"] = _draft_payload(draft)
    await update.message.reply_text(_preview_text(draft), reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Alarmı Kur", callback_data="upa_confirm_single"),
        InlineKeyboardButton("❌ İptal", callback_data="upa_cancel_flow"),
    ]]))


async def cmd_toplu_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    context.user_data["alarm_flow"] = "bulk_text"
    await update.message.reply_text(
        "📋 Toplu alarm listesini şimdi gönder.\n\n"
        "ASELS 72.50 üstü\nTHYAO 285 altı\nEREGL;31,20;yukarı_kes\n\n"
        f"En fazla {get_settings().user_price_alert_max_bulk_import} satır. Kaydetmeden önce önizleme gösterilir."
    )


async def cmd_alarm_multi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Compatibility command: /alarm 9.20 THYAO ASELS ses=radar kosul=ustu."""
    if await _reject_unauthorized(update): return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanım: /alarm 9.20 THYAO ASELS ses=radar koşul=yaklaşık\n"
            "Koşul: üstü, altı, yukarı_kes, aşağı_kes veya yaklaşık"
        )
        return
    try:
        target = parse_decimal(context.args[0])
        options = {}
        symbol_values = []
        for raw in context.args[1:]:
            if "=" in raw:
                key, value = raw.split("=", 1)
                options[key.casefold()] = value
            else:
                symbol_values.append(raw)
        settings = get_settings()
        if not symbol_values:
            raise ValueError("en az bir hisse sembolü gerekli")
        if len(symbol_values) > settings.user_price_alert_max_bulk_import:
            raise ValueError(f"en fazla {settings.user_price_alert_max_bulk_import} hisse eklenebilir")
        condition = parse_condition(options.get("koşul") or options.get("kosul") or options.get("yon") or "yaklaşık")
        sound = normalize_sound(options.get("ses"))
        repeat = int(options.get("tekrar", settings.user_price_alert_default_repeat_seconds))
        if not settings.user_price_alert_min_repeat_seconds <= repeat <= 86_400:
            raise ValueError("tekrar aralığı izin verilen sınırda değil")
        seen = set()
        drafts = []
        for raw in symbol_values:
            symbol = normalize_symbol(raw)
            if symbol in seen:
                continue
            seen.add(symbol)
            drafts.append(AlarmDraft(
                symbol=symbol, target_price=target, condition=condition,
                repeat_interval_seconds=repeat, sound_name=sound,
            ))
        parsed = BulkParseResult(tuple(drafts), ())
        db, user = _db_user(update)
        try:
            job = create_import_preview(
                db, user, update.effective_chat.id, parsed, ImportSource.TEXT.value,
                settings.user_price_alert_temp_file_ttl_minutes,
            )
        finally:
            db.close()
        await _send_import_preview(update, job.public_id)
    except (ValueError, AlarmServiceError) as exc:
        await update.message.reply_text(f"❌ Alarm listesi hazırlanamadı: {exc}")


async def cmd_alarm_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    context.user_data["alarm_flow"] = "photo"
    await update.message.reply_text("📷 Alarm listesinin ekran görüntüsünü gönder. OCR sonucu kaydedilmeden önce onayına sunulur.")


async def cmd_alarm_dosya(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    context.user_data["alarm_flow"] = "document"
    await update.message.reply_text("📎 CSV veya XLSX alarm dosyasını gönder. Zorunlu sütunlar: hisse, fiyat, koşul.")


async def handle_alarm_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("alarm_flow")
    if not flow or await _reject_unauthorized(update): return
    text = (update.message.text or "").strip()
    if text.casefold() in {"/iptal", "iptal"}:
        context.user_data.pop("alarm_flow", None); context.user_data.pop("pending_alarm", None)
        await update.message.reply_text("❌ Alarm işlemi iptal edildi."); return
    try:
        if flow == "single_symbol":
            context.user_data["alarm_symbol"] = normalize_symbol(text)
            context.user_data["alarm_flow"] = "single_price"
            await update.message.reply_text("Alarm fiyatını yaz: 72,50")
        elif flow == "single_price":
            context.user_data["alarm_price"] = str(parse_decimal(text))
            context.user_data["alarm_flow"] = "single_condition"
            await update.message.reply_text("Koşulu yaz: üstü, altı, yukarı_kes, aşağı_kes veya yaklaşık")
        elif flow == "single_condition":
            draft = AlarmDraft(context.user_data.pop("alarm_symbol"), Decimal(context.user_data.pop("alarm_price")), parse_condition(text))
            context.user_data["pending_alarm"] = _draft_payload(draft)
            context.user_data.pop("alarm_flow", None)
            await update.message.reply_text(_preview_text(draft), reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Alarmı Kur", callback_data="upa_confirm_single"),
                InlineKeyboardButton("❌ İptal", callback_data="upa_cancel_flow"),
            ]]))
        elif flow == "bulk_text":
            settings = get_settings()
            parsed = parse_bulk_text(text, maximum_rows=settings.user_price_alert_max_bulk_import)
            db, user = _db_user(update)
            try: job = create_import_preview(db, user, update.effective_chat.id, parsed, ImportSource.TEXT.value,
                                             settings.user_price_alert_temp_file_ttl_minutes)
            finally: db.close()
            context.user_data.pop("alarm_flow", None)
            await _send_import_preview(update, job.public_id)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}\nTekrar dene veya /iptal yaz.")


async def _send_import_preview(update: Update, job_ref: str) -> None:
    db, user = _db_user(update)
    try:
        job = db.query(AlarmImportJob).filter_by(public_id=job_ref, user_id=user.id).one()
        errors = db.query(AlarmImportRow).filter_by(import_job_id=job.id, status="INVALID").limit(8).all()
        error_text = "\n".join(f"{row.row_number}. {row.validation_error}" for row in errors) or "Yok"
        text = (
            "📋 TOPLU ALARM ÖNİZLEMESİ\n━━━━━━━━━━━━━━━━━━\n"
            f"Geçerli: {job.valid_rows}\nHatalı: {job.invalid_rows}\nTekrar eden: {job.duplicate_rows}\n"
            f"Yeni oluşturulacak: {job.valid_rows}\n\nHatalı satırlar:\n{error_text}\n\n"
            "Hiçbir alarm henüz kaydedilmedi."
        )
    finally: db.close()
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Geçerli Alarmları Kur", callback_data=f"upa_import_{job_ref}"),
        InlineKeyboardButton("❌ İptal", callback_data=f"upa_importcancel_{job_ref}"),
    ]]))


async def handle_alarm_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    if context.user_data.get("alarm_flow") != "photo": return
    if not update.message.photo: return
    settings = get_settings()
    try:
        file = await update.message.photo[-1].get_file()
        content = bytes(await file.download_as_bytearray())
        result = await asyncio.to_thread(
            extract_alarm_text, content, language=settings.user_price_alert_ocr_language,
            maximum_bytes=settings.user_price_alert_max_image_bytes,
        )
        parsed = parse_bulk_text(result.text, maximum_rows=settings.user_price_alert_max_bulk_import, source=ImportSource.OCR)
        if result.confidence < .80:
            context.user_data["alarm_flow"] = "bulk_text"
            await update.message.reply_text(
                "⚠️ OCR güveni düşük; hiçbir alarm kaydedilmedi.\n\n"
                f"Okunan metin:\n{result.text[:3000]}\n\n"
                "Metni kontrol edip düzelt, ardından yeniden gönder."
            )
            return
        db, user = _db_user(update)
        try: job = create_import_preview(db, user, update.effective_chat.id, parsed, ImportSource.OCR.value,
                                         settings.user_price_alert_temp_file_ttl_minutes)
        finally: db.close()
        context.user_data.pop("alarm_flow", None)
        await _send_import_preview(update, job.public_id)
    except (ValueError, OCRUnavailableError) as exc:
        await update.message.reply_text(f"⚠️ Görsel işlenemedi: {exc}")


async def handle_alarm_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    if context.user_data.get("alarm_flow") != "document": return
    doc = update.message.document
    if doc is None: return
    name = (doc.file_name or "").casefold()
    if not (name.endswith(".csv") or name.endswith(".xlsx")): return
    settings = get_settings()
    if doc.file_size and doc.file_size > settings.user_price_alert_max_file_bytes:
        await update.message.reply_text("❌ Dosya izin verilen boyutu aşıyor."); return
    try:
        file = await doc.get_file(); content = bytes(await file.download_as_bytearray())
        parser = parse_xlsx_bytes if name.endswith(".xlsx") else parse_csv_bytes
        parsed = await asyncio.to_thread(parser, content, maximum_rows=settings.user_price_alert_max_bulk_import)
        source = ImportSource.XLSX.value if name.endswith(".xlsx") else ImportSource.CSV.value
        db, user = _db_user(update)
        try: job = create_import_preview(db, user, update.effective_chat.id, parsed, source,
                                         settings.user_price_alert_temp_file_ttl_minutes)
        finally: db.close()
        context.user_data.pop("alarm_flow", None)
        await _send_import_preview(update, job.public_id)
    except (ValueError, RuntimeError) as exc:
        await update.message.reply_text(f"❌ Dosya işlenemedi: {exc}")


async def cmd_alarmlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    status = None
    command = (update.effective_message.text or "").split(maxsplit=1)[0].split("@", 1)[0].casefold()
    if command == "/aktif_alarmlar":
        status = "ACTIVE"
    elif command == "/tetiklenen_alarmlar":
        status = "TRIGGERED"
    if context.args and context.args[0].upper() in {"ACTIVE", "TRIGGERED", "PAUSED", "COMPLETED"}:
        status = context.args[0].upper()
    await _send_alarm_page(update, 1, status)


async def _send_alarm_page(update: Update, page: int, status: str | None = None) -> None:
    db, user = _db_user(update)
    try: items, total = page_alerts(db, user.id, page=page, status=status)
    finally: db.close()
    pages = max(1, math.ceil(total / 8))
    lines = [f"🔔 ALARMLARIM — Sayfa {page}/{pages}", "━━━━━━━━━━━━━━━━━━"]
    lines += [f"\n{index}. {format_alarm_summary(item)}" for index, item in enumerate(items, 1 + (page - 1) * 8)]
    if not items: lines.append("\nKayıtlı alarm bulunamadı.")
    buttons = []
    nav = []
    if page > 1: nav.append(InlineKeyboardButton("◀️ Önceki", callback_data=f"upa_page_{page-1}_{status or 'ALL'}"))
    if page < pages: nav.append(InlineKeyboardButton("▶️ Sonraki", callback_data=f"upa_page_{page+1}_{status or 'ALL'}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton("➕ Yeni Alarm", callback_data="upa_new")])
    await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _reference_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if await _reject_unauthorized(update): return
    if not context.args:
        await update.message.reply_text(f"Kullanım: /alarm_{action} ALR-A7K29M"); return
    db, user = _db_user(update)
    try:
        alert = owned_alert(db, user.id, context.args[0])
        if alert is None: text = "❌ Sana ait alarm bulunamadı."
        elif action == "durdur": text = acknowledge_alarm(db, alert)
        elif action == "devam": text = resume_alarm(db, alert)
        elif action == "sil": text = delete_alarm(db, alert)
        elif action == "ertele":
            try:
                minutes = int(context.args[1]) if len(context.args) > 1 else 5
                if not 1 <= minutes <= 10_080:
                    raise ValueError
                text = snooze_alarm(db, alert, minutes)
            except ValueError:
                text = "❌ Erteleme süresi 1–10080 dakika arasında tam sayı olmalı."
        else: text = format_alarm_summary(alert)
    finally: db.close()
    await update.message.reply_text(text)


async def cmd_alarm_durdur(update, context): await _reference_action(update, context, "durdur")
async def cmd_alarm_devam(update, context): await _reference_action(update, context, "devam")
async def cmd_alarm_sil(update, context): await _reference_action(update, context, "sil")
async def cmd_alarm_ertele(update, context): await _reference_action(update, context, "ertele")
async def cmd_alarm_detay(update, context): await _reference_action(update, context, "detay")


async def cmd_alarm_ayar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    db, user = _db_user(update)
    try:
        value = get_alarm_settings(db, user.id)
        if len(context.args) >= 2:
            key, raw = context.args[0].casefold(), context.args[1].casefold()
            if key in {"aralık", "aralik"}:
                seconds = int(raw)
                if not 30 <= seconds <= 86400: raise ValueError("Aralık 30–86400 saniye olmalı.")
                value.default_repeat_interval_seconds = seconds
            elif key == "ses":
                mapping = {"metin": SoundMode.TEXT_ONLY.value, "ilk": SoundMode.FIRST_TRIGGER.value, "periyodik": SoundMode.PERIODIC.value}
                if raw in SOUND_CHOICES:
                    value.default_sound_name = raw
                elif raw in mapping:
                    value.default_sound_mode = mapping[raw]
                else:
                    raise ValueError("Ses seçimi: zil, radar, acil, metin, ilk veya periyodik")
            elif key == "seans": value.market_hours_only = raw in {"acik", "açık", "true", "1"}
            else: raise ValueError("Ayar: ses, aralık veya seans")
            db.commit()
        text = ("⚙️ ALARM AYARLARI\n"
                f"Tekrar aralığı: {value.default_repeat_interval_seconds} sn\n"
                f"Bildirim sesi: {value.default_sound_name}\nSes tekrarı: {value.default_sound_mode}\n"
                f"Sadece seans: {'Evet' if value.market_hours_only else 'Hayır'}\n\n"
                "/alarm_ayar ses radar\n/alarm_ayar ses ilk\n/alarm_ayar aralık 60\n/alarm_ayar seans açık")
    except ValueError as exc: text = f"❌ {exc}"
    finally: db.close()
    await update.message.reply_text(text)


async def cmd_alarm_yardim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    await update.message.reply_text(
        "🔔 FİYAT ALARMI YARDIMI\n━━━━━━━━━━━━━━━━━━\n"
        "/alarm_kur ASELS 72.50 üstü — tek alarm\n/toplu_alarm — metinden yüzlerce alarm\n"
        "/alarm_foto — ekran görüntüsünden OCR\n/alarm_dosya — CSV/XLSX\n/alarmlar — alarm listesi\n"
        "/alarm_durdur ALR-XXXXXX\n/alarm_ertele ALR-XXXXXX 5\n/alarm_devam ALR-XXXXXX\n"
        "/alarm_sil ALR-XXXXXX\n/alarm_ayar — tekrar/ses/seans ayarı\n\n"
        "Bildirimler durdurulana kadar güvenli aralıkla tekrarlanır. Telegram, telefonun sessiz modunu aşamaz."
    )


async def handle_alarm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    if await _reject_unauthorized(update): return
    data = query.data or ""
    if data == "upa_cancel_flow":
        context.user_data.pop("pending_alarm", None); context.user_data.pop("alarm_flow", None)
        await query.message.reply_text("❌ İşlem iptal edildi."); return
    if data == "upa_confirm_single":
        payload = context.user_data.pop("pending_alarm", None)
        if not payload: await query.message.reply_text("⚠️ Önizlemenin süresi doldu."); return
        db, user = _db_user(update)
        try:
            alert = create_alarm(db, user, update.effective_chat.id, _payload_draft(payload),
                                 maximum_active=get_settings().user_price_alert_max_active_per_user)
            text = f"✅ Alarm kuruldu\n\n{format_alarm_summary(alert)}"
        except DuplicateAlarmError as exc: text = f"⚠️ Aynı alarm zaten mevcut.\nReferans: {exc.existing.public_id}"
        except AlarmServiceError as exc: text = f"❌ {exc}"
        finally: db.close()
        await query.message.reply_text(text); return
    if data.startswith("upa_import_"):
        ref = data.removeprefix("upa_import_")
        db, user = _db_user(update)
        try: rows = confirm_import(db, user, ref, maximum_active=get_settings().user_price_alert_max_active_per_user); text=f"✅ {len(rows)} alarm oluşturuldu."
        except AlarmServiceError as exc: text=f"❌ {exc}"
        finally: db.close()
        await query.message.reply_text(text); return
    if data.startswith("upa_importcancel_"):
        ref=data.removeprefix("upa_importcancel_"); db,user=_db_user(update)
        try:
            job=db.query(AlarmImportJob).filter_by(public_id=ref,user_id=user.id).one_or_none()
            if job and job.status!="CONFIRMED": job.status="CANCELLED"; db.commit()
        finally: db.close()
        await query.message.reply_text("❌ İçe aktarma iptal edildi."); return
    if data.startswith("upa_page_"):
        _,_,page,status=data.split("_",3); await _send_alarm_page(update,int(page),None if status=="ALL" else status); return
    if data == "upa_new":
        context.user_data["alarm_flow"]="single_symbol"; await query.message.reply_text("Hisse sembolünü yaz: ASELS"); return
    action_ref = None
    for action in ("stop", "snooze5", "delete", "analysis"):
        prefix=f"upa_{action}_"
        if data.startswith(prefix): action_ref=(action,data.removeprefix(prefix)); break
    if action_ref:
        action,ref=action_ref; db,user=_db_user(update)
        try:
            alert=owned_alert(db,user.id,ref)
            if alert is None: text="❌ Sana ait alarm bulunamadı."
            elif action=="stop": text=acknowledge_alarm(db,alert)
            elif action=="snooze5": text=snooze_alarm(db,alert,5)
            elif action=="delete": text=delete_alarm(db,alert)
            else: text=f"Analiz için /analiz {alert.symbol} yaz."
        finally: db.close()
        await query.message.reply_text(text)


def register_alarm_handlers(application) -> None:
    application.add_handler(CommandHandler("alarm", cmd_alarm_multi), group=0)
    for name in ("alarm_kur", "tekli_alarm"):
        application.add_handler(CommandHandler(name, cmd_alarm_kur), group=0)
    for name in ("toplu_alarm", "alarm_listesi"):
        application.add_handler(CommandHandler(name, cmd_toplu_alarm), group=0)
    for name in ("alarm_foto", "fotodan_alarm"):
        application.add_handler(CommandHandler(name, cmd_alarm_foto), group=0)
    application.add_handler(CommandHandler("alarm_dosya", cmd_alarm_dosya), group=0)
    for name in ("alarmlar", "aktif_alarmlar", "tetiklenen_alarmlar"):
        application.add_handler(CommandHandler(name, cmd_alarmlar), group=0)
    application.add_handler(CommandHandler("alarm_durdur", cmd_alarm_durdur), group=0)
    application.add_handler(CommandHandler("alarm_devam", cmd_alarm_devam), group=0)
    application.add_handler(CommandHandler("alarm_ertele", cmd_alarm_ertele), group=0)
    application.add_handler(CommandHandler("alarm_sil", cmd_alarm_sil), group=0)
    application.add_handler(CommandHandler("alarm_detay", cmd_alarm_detay), group=0)
    application.add_handler(CommandHandler("alarm_ayar", cmd_alarm_ayar), group=0)
    application.add_handler(CommandHandler("alarm_yardim", cmd_alarm_yardim), group=0)
    application.add_handler(CallbackQueryHandler(handle_alarm_callback, pattern=r"^upa_"), group=0)
    application.add_handler(MessageHandler(filters.PHOTO, handle_alarm_photo), group=1)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_alarm_document), group=1)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_alarm_text), group=1)
