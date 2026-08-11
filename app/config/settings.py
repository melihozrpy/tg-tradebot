from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Ortam degiskenlerinden okunan uygulama ayarlari.

    Hicbir sir (secret) kod icinde sabit tutulmaz; hepsi .env dosyasindan
    veya gercek ortam degiskenlerinden okunur.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")
    bot_name: str = Field(default="Montana Finans Robotu")

    telegram_bot_token: str = Field(default="")
    admin_telegram_user_ids: str = Field(default="")
    telegram_mode: str = Field(default="polling")  # polling | webhook
    telegram_webhook_url: str = Field(default="")
    telegram_webhook_secret: str = Field(default="")

    database_url: str = Field(default="sqlite:///./mergen_quant.db")

    market_data_provider: str = Field(default="mock")  # mock | csv
    licensed_market_data_base_url: str = Field(default="")
    licensed_market_data_api_key: str = Field(default="")
    licensed_market_data_api_key_header: str = Field(default="X-API-Key")
    licensed_market_data_quote_path: str = Field(default="/quote/{symbol}")
    licensed_market_data_ohlcv_path: str = Field(default="/ohlcv/{symbol}")
    licensed_market_data_market_state_path: str = Field(default="/market-state")
    licensed_market_data_provider_name: str = Field(default="licensed_rest")
    licensed_market_data_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    kap_provider: str = Field(default="disabled")
    broker_flow_provider: str = Field(default="disabled")
    fundamental_provider: str = Field(default="auto")
    fundamental_secondary_provider: str = Field(default="disabled")
    fundamental_cross_check_enabled: bool = Field(default=False)
    fundamental_cross_check_strict: bool = Field(default=True)
    fundamental_allow_secondary_fallback: bool = Field(default=True)
    fundamental_allow_yahoo_fallback: bool = Field(default=True)
    fundamental_cross_check_relative_tolerance: float = Field(default=0.03, ge=0)
    fundamental_cross_check_absolute_tolerance: float = Field(default=1.0, ge=0)
    fundamental_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    # Fintables yalnizca kullanicinin/lisans sahibinin OAuth yetkisiyle MCP
    # uzerinden kullanilir. Web sayfasi kazima veya hesabi ortak kullanma yoktur.
    fintables_mcp_url: str = Field(default="https://evo.fintables.com/mcp")
    fintables_mcp_bearer_token: str = Field(default="")
    # Eski/alternatif env adi geriye uyumluluk icin korunur.
    fintables_oauth_bearer_token: str = Field(default="")
    fintables_mcp_tool_name: str = Field(default="")
    fintables_mcp_symbol_argument: str = Field(default="symbol")
    # KAP REST erisimi Borsa Istanbul sozlesmesi/API anahtari gerektirir.
    kap_rest_base_url: str = Field(default="")
    kap_rest_api_key: str = Field(default="")
    kap_rest_api_key_header: str = Field(default="X-API-Key")
    kap_rest_endpoint_path: str = Field(default="/fundamentals/{symbol}")
    kap_rest_fundamental_path_template: str = Field(default="/fundamentals/{symbol}")
    kap_rest_disclosures_path: str = Field(default="/disclosures")
    kap_rest_disclosure_detail_path: str = Field(default="/disclosureDetail/{id}")
    kap_rest_symbol_query_param: str = Field(default="symbol")
    csv_data_dir: str = Field(default="./data_csv")
    yfinance_timeout_seconds: int = Field(default=10)
    yfinance_max_retries: int = Field(default=3)
    provider_retry_max_attempts: int = Field(default=2)
    provider_retry_base_seconds: float = Field(default=1.0)
    provider_circuit_failure_threshold: int = Field(default=3)
    provider_circuit_recovery_seconds: int = Field(default=120)
    yahoo_chart_fallback_enabled: bool = Field(default=True)
    data_cache_dir: str = Field(default="./data/cache/ohlcv")
    data_cache_max_age_daily_minutes: int = Field(default=720)
    data_cache_max_age_intraday_minutes: int = Field(default=30)
    technical_price_mode: str = Field(default="unadjusted")  # unadjusted | adjusted
    # Aşama 5e: geçmiş teknik serinin fiyat düzeltme modu. Eski
    # TECHNICAL_PRICE_MODE alanı geriye uyumluluk için korunur.
    price_adjustment_mode: str = Field(default="adjusted")  # adjusted | raw

    groq_enabled: bool = Field(default=False)
    groq_api_key: str = Field(default="")
    groq_model: str = Field(default="")
    groq_timeout_seconds: int = Field(default=20)
    groq_max_retries: int = Field(default=2)
    groq_daily_request_limit: int = Field(default=50)

    # ---- OpenRouter: kapsamli metin + grafik gorseli analiz asistani ----
    # Fiyat, indikatör ve finansal kalemler LLM tarafinda hesaplanmaz. Botun
    # doğruladigi bağlam modele verilir; eksik alanlar açıkça "veri yok" kalır.
    openrouter_enabled: bool = Field(default=False)
    openrouter_api_key: str = Field(default="")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    openrouter_model: str = Field(default="nvidia/nemotron-3-ultra-550b-a55b:free")
    openrouter_model_fallbacks: str = Field(
        default=(
            "inclusionai/ling-3.0-flash:free,"
            "nvidia/nemotron-3-super-120b-a12b:free,"
            "openai/gpt-oss-20b:free"
        )
    )
    # openrouter/free bazen metin-only modele yonlenebildigi icin grafiklerde
    # resmi dogrudan kabul eden sabit bir ucretsiz model kullanilir.
    openrouter_vision_model: str = Field(default="google/gemma-4-31b-it:free")
    openrouter_vision_model_fallbacks: str = Field(
        default=(
            "google/gemma-4-26b-a4b-it:free,"
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,"
            "nvidia/nemotron-nano-12b-v2-vl:free"
        )
    )
    openrouter_timeout_seconds: int = Field(default=90, ge=5, le=180)
    openrouter_max_tokens: int = Field(default=3000, ge=256, le=16000)
    openrouter_max_retries: int = Field(default=2, ge=0, le=5)
    # 0 = yapay bot kotası kapalı. Gerçek sağlayıcı kotası ve model fallback'i geçerlidir.
    openrouter_daily_request_limit: int = Field(default=0, ge=0, le=100000)
    openrouter_local_rate_limit_enabled: bool = Field(default=False)
    openrouter_max_image_bytes: int = Field(default=10_485_760, ge=1024, le=20_971_520)

    # ---- V3.2 (Asama 4): GDELT haber radari ----
    gdelt_enabled: bool = Field(default=True)
    gdelt_timeout_seconds: int = Field(default=20)
    gdelt_max_retries: int = Field(default=2)
    news_cache_ttl_minutes: int = Field(default=30)
    company_aliases_path: str = Field(default="app/config/company_aliases.yaml")

    default_total_capital: float = Field(default=100000.0)

    # ---- V3: XU100 / piyasa rejimi ----
    xu100_symbol: str = Field(default="XU100.IS")

    # ---- V3: gun ici / kesinlesmis kapanis analiz modu ----
    intraday_preview_enabled: bool = Field(default=True)
    confirmed_close_required: bool = Field(default=True)
    timezone_name: str = Field(default="Europe/Istanbul")
    close_scan_time: str = Field(default="18:20")
    close_scan_retry_minutes: int = Field(default=15)
    close_scan_max_retries: int = Field(default=4)
    close_scan_enabled: bool = Field(default=True)

    # ---- V3: skor esikleri (env'den de degistirilebilir) ----
    minimum_signal_score: float = Field(default=65.0)
    minimum_risk_reward: float = Field(default=2.0)
    risk_per_trade_percent: float = Field(default=0.75)
    maximum_position_percent: float = Field(default=20.0)
    maximum_sector_exposure_percent: float = Field(default=30.0)

    # ---- V3: cache ----
    cache_enabled: bool = Field(default=True)
    daily_cache_ttl_minutes: int = Field(default=30)
    intraday_cache_ttl_minutes: int = Field(default=5)

    # ---- V3: yfinance rate-limit koruma ----
    yfinance_request_delay_seconds: float = Field(default=1.0)

    # ---- Asama 5d: profesyonel grafik ----
    chart_dpi: int = Field(default=120)
    chart_width: float = Field(default=13.0)
    chart_height: float = Field(default=11.0)
    chart_theme: str = Field(default="dark")  # light | dark; Telegram için canlı koyu tema varsayılan
    chart_cache_ttl_minutes: int = Field(default=30)
    chart_cache_dir: str = Field(default="./data/cache/charts")

    # ---- Zone-based staged entry / clean scenario chart (additive) ----
    staged_entry_enabled: bool = Field(default=True)
    staged_entry_allocations: str = Field(default="40,35,25")

    # ---- Full-universe technical screener (additive) ----
    technical_screener_enabled: bool = Field(default=True)
    technical_screener_interval_minutes: int = Field(default=30, ge=15, le=120)
    technical_screener_min_confluence: int = Field(default=3, ge=3, le=7)
    technical_screener_workers: int = Field(default=3, ge=1, le=12)
    technical_screener_chat_id: int | None = Field(default=None)
    technical_screener_max_symbols_per_run: int = Field(default=1000, ge=1, le=5000)
    rsi_overbought: float = Field(default=75.0, ge=50, le=100)
    rsi_oversold: float = Field(default=25.0, ge=0, le=50)
    intraday_vwap_scan_enabled: bool = Field(default=True)
    intraday_vwap_scan_minute_step: int = Field(default=30, ge=15, le=60)

    # ---- Çoklu gösterge fırsat radarı ----
    # RSI remains an input only; it never generates a stand-alone notification.
    trade_scenario_scan_enabled: bool = Field(default=True)
    trade_scenario_scan_minutes: int = Field(default=180, ge=15, le=240)
    trade_scenario_max_results: int = Field(default=5, ge=3, le=12)
    trade_scenario_minimum_core_confirmations: int = Field(default=3, ge=3, le=5)
    trade_scenario_minimum_ten_confirmations: int = Field(default=8, ge=3, le=10)
    # ---- Saatlik günlük ilk 5 teknik + temel doğrulama listesi ----
    daily_top_picks_enabled: bool = Field(default=True)
    daily_top_picks_max_results: int = Field(default=5, ge=1, le=10)
    daily_top_picks_minimum_confirmations: int = Field(default=6, ge=5, le=8)
    daily_top_picks_fundamental_candidates: int = Field(default=20, ge=5, le=40)
    # Sağlam firma etiketi ancak temel veri kaynağı gerçekten doğruladığında kullanılır.
    daily_top_picks_require_fundamental: bool = Field(default=True)
    market_opportunity_max_results: int = Field(default=8, ge=3, le=12)
    market_opportunity_minimum_confluence: int = Field(default=5, ge=3, le=10)

    # ---- VIOP egitim ve spot-dayanakli senaryo modulu ----
    # Bu liste canli sozlesme/veri akisi degil, resmi kaynaktan tarihli izleme
    # evrenidir. Emirden once araci kurum ekranindaki aktif vade/teminat esastir.
    viop_underlyings_json_path: str = Field(default="app/config/viop_underlyings.json")
    viop_watchlist_max_results: int = Field(default=8, ge=3, le=15)
    viop_risk_percent: float = Field(default=0.5, gt=0, le=1)

    # ---- 24-48 hour symbol news digest ----
    news_digest_cache_minutes: int = Field(default=15, ge=1, le=1440)
    news_digest_lookback_hours: int = Field(default=48, ge=1, le=168)
    news_scrape_urls: str = Field(default="")

    # ---- Asama 5d: gelismis alarm taramasi ----
    enhanced_alarm_scan_enabled: bool = Field(default=True)
    enhanced_alarm_scan_minutes: int = Field(default=15)
    enhanced_alarm_default_cooldown_minutes: int = Field(default=120)
    breakout_minimum_volume_ratio: float = Field(default=1.2)
    daily_brief_enabled: bool = Field(default=True)
    daily_brief_time: str = Field(default="09:10")
    tcmb_policy_rate_percent: float | None = Field(default=None)

    # ---- SMXM sabah/akşam raporları ve PDF tabanlı BIST evreni ----
    # JSON dizi (örn. ["THYAO","EURUSD"]) veya virgüllü liste kabul edilir.
    # Boş bırakılırsa otomatik rapor yalnızca XU100 ile çalışır; 571 kodluk
    # tam evren ayrıca BIST_UNIVERSE_JSON_PATH üzerinden kullanılmaya devam eder.
    instruments: str = Field(default="")
    bist_universe_json_path: str = Field(default="app/config/bist_instruments.json")
    morning_report_enabled: bool = Field(default=True)
    morning_report_time: str = Field(default="09:00")
    evening_market_report_enabled: bool = Field(default=True)
    evening_market_report_time: str = Field(default="21:00")
    report_chart_output_dir: str = Field(default="/tmp/mergen_quant_reports")
    report_max_news_events: int = Field(default=8, ge=0, le=50)
    # KAP sağlayıcısı sembol bazlıysa, raporu rate-limit'e sokmamak için yalnız
    # teknik olarak önceliklendirilmiş kısa liste kontrol edilir.
    report_kap_symbol_limit: int = Field(default=20, ge=4, le=40)
    report_news_impact_minimum_score: int = Field(default=70, ge=1, le=100)
    economic_calendar_enabled: bool = Field(default=True)
    economic_calendar_url: str = Field(default="https://tr.investing.com/economic-calendar/")
    economic_calendar_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    vix_symbol: str = Field(default="^VIX")
    dxy_symbol: str = Field(default="DX-Y.NYB")

    # ---- Tüm Hisseler / en iyi giriş taraması ----
    universe_scan_top_n: int = Field(default=50, ge=1, le=100)
    universe_scan_max_symbols_per_run: int = Field(default=1000, ge=1, le=5000)
    universe_scan_workers: int = Field(default=3, ge=1, le=12)
    universe_scan_cache_minutes: int = Field(default=60, ge=1, le=1440)
    universe_scan_minimum_score: int = Field(default=68, ge=0, le=100)

    # ---- Kalici, kullanici tanimli fiyat alarmlari (0008) ----
    user_price_alerts_enabled: bool = Field(default=True)
    user_price_alert_poll_seconds: int = Field(default=30, ge=5, le=3600)
    user_price_alert_delivery_poll_seconds: int = Field(default=5, ge=1, le=300)
    user_price_alert_default_repeat_seconds: int = Field(default=60, ge=30, le=86400)
    user_price_alert_min_repeat_seconds: int = Field(default=30, ge=30, le=86400)
    user_price_alert_max_active_per_user: int = Field(default=500, ge=1, le=10_000)
    user_price_alert_max_bulk_import: int = Field(default=250, ge=1, le=5_000)
    user_price_alert_stale_after_seconds: int = Field(default=180, ge=5, le=86400)
    user_price_alert_max_deliveries_per_minute_per_user: int = Field(default=10, ge=1, le=120)
    user_price_alert_max_global_deliveries_per_minute: int = Field(default=500, ge=1, le=10_000)
    user_price_alert_audio_enabled: bool = Field(default=True)
    user_price_alert_ocr_enabled: bool = Field(default=True)
    user_price_alert_ocr_language: str = Field(default="tur+eng")
    user_price_alert_temp_file_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    user_price_alert_max_image_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024
    )
    user_price_alert_max_file_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024
    )

    # ---- V3: kamuya acik bildirim (KAP yerine) ----
    public_disclosure_provider: str = Field(default="disabled")

    # ---- V3: sektor mapping / sembol evreni ----
    sector_map_path: str = Field(default="app/config/sector_map.yaml")
    bist_symbols_csv_path: str = Field(default="data/symbols/bist_symbols.csv")

    # ---- V3: performans raporu ----
    performance_minimum_sample_size: int = Field(default=20)

    # ---- V3: sinyal yasam dongusu ----
    signal_expiry_trading_days: int = Field(default=10)
    conservative_execution: bool = Field(default=True)

    # ---- Ultra BIST: emir/sinyal yasam dongusu ve canli izleme ----
    long_only: bool = Field(default=True)
    signal_monitor_enabled: bool = Field(default=True)
    signal_monitor_interval_seconds: int = Field(default=5, ge=1, le=3600)
    allow_delayed_data_for_live_trigger: bool = Field(default=False)
    max_market_data_staleness_seconds: int | None = Field(default=None)
    default_risk_percent: float = Field(default=1.0, gt=0, le=100)
    max_position_percent: float = Field(default=20.0, gt=0, le=100)
    max_daily_volume_participation_percent: float = Field(default=1.0, gt=0, le=100)
    default_tp1_allocation: float = Field(default=40.0, ge=0, le=100)
    default_tp2_allocation: float = Field(default=35.0, ge=0, le=100)
    default_tp3_allocation: float = Field(default=25.0, ge=0, le=100)
    move_stop_to_breakeven_after_tp1: bool = Field(default=True)
    move_stop_to_tp1_after_tp2: bool = Field(default=True)
    backtest_entry_mode: str = Field(default="next_session_level_touch")
    backtest_intrabar_mode: str = Field(default="lower_timeframe_then_conservative")
    backtest_fill_model: str = Field(default="conservative_volume_limited")
    backtest_limit_lock_mode: str = Field(default="conservative")
    backtest_price_mode: str = Field(default="split_adjusted")
    backtest_commission_rate: float | None = Field(default=None)
    backtest_commission_minimum: float | None = Field(default=None)
    backtest_commission_tax_rate: float | None = Field(default=None)
    backtest_include_dividends: bool = Field(default=True)

    # ---- Asama 5g: gercekci backtest / walk-forward / sanal islem ----
    backtest_commission_bps: float = Field(default=0.0)
    backtest_slippage_bps: float = Field(default=0.0)
    backtest_spread_bps: float = Field(default=0.0)
    backtest_bsmv_bps: float = Field(default=0.0)
    backtest_minimum_cost: float = Field(default=0.0)
    backtest_initial_capital: float = Field(default=100_000.0)
    backtest_max_position_pct: float = Field(default=20.0)
    backtest_intrabar_policy: str = Field(default="conservative")
    backtest_entry_model: str = Field(default="next_open")
    backtest_minimum_sample_size: int = Field(default=30)
    backtest_max_concurrent_per_user: int = Field(default=1)
    backtest_timeout_seconds: int = Field(default=600)
    walk_forward_train_days: int = Field(default=504)
    walk_forward_validation_days: int = Field(default=126)
    walk_forward_test_days: int = Field(default=126)
    walk_forward_step_days: int = Field(default=126)
    walk_forward_mode: str = Field(default="rolling")
    calibration_minimum_sample_size: int = Field(default=30)
    paper_trading_initial_capital: float = Field(default=100_000.0)
    paper_trading_scan_minutes: int = Field(default=15)

    # ---- Çoklu sanal portföy / SMXM simülasyon kuralları ----
    virtual_portfolio_max_per_user: int = Field(default=3, ge=1, le=20)
    virtual_portfolio_max_strategies: int = Field(default=2, ge=1, le=10)
    virtual_trade_risk_percent: float = Field(default=1.0, gt=0, le=100)
    virtual_trade_after_loss_risk_percent: float = Field(default=0.5, gt=0, le=100)
    virtual_trade_minimum_rr: float = Field(default=2.0, gt=0)
    virtual_trade_minimum_checklist: int = Field(default=5, ge=0, le=6)
    virtual_trade_blocked_weekdays: str = Field(default="0,4")

    # ---- V3.1: gun ici onizleme zaman dilimi ----
    intraday_snapshot_timeframe: str = Field(default="15m")
    intraday_max_lag_minutes: int = Field(default=30)

    # ---- V3.1: coklu zaman dilimi motoru ----
    multi_timeframe_enabled: bool = Field(default=True)
    timeframes: str = Field(default="5m,15m,1h,1d,1wk")
    timeframe_weight_weekly: int = Field(default=30)
    timeframe_weight_daily: int = Field(default=25)
    timeframe_weight_4h: int = Field(default=20)
    timeframe_weight_1h: int = Field(default=15)
    timeframe_weight_15m: int = Field(default=7)
    timeframe_weight_5m: int = Field(default=3)

    # ---- V3.1: likidite filtresi ----
    liquidity_filter_enabled: bool = Field(default=True)
    minimum_average_volume: float = Field(default=100_000.0)
    minimum_average_turnover_try: float = Field(default=5_000_000.0)
    maximum_atr_percent: float = Field(default=12.0)
    strong_signal_minimum_liquidity_score: float = Field(default=45.0)

    @property
    def timeframe_list(self) -> List[str]:
        return [tf.strip() for tf in self.timeframes.split(",") if tf.strip()]

    @field_validator("market_data_provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        normalized = v.strip().lower()
        allowed = {"mock", "csv", "yfinance", "licensed_rest"}
        if normalized not in allowed:
            raise ValueError(
                f"MARKET_DATA_PROVIDER '{normalized}' desteklenmiyor. Izin verilenler: {allowed}. "
                "'mock' yalnizca test/gelistirme icindir; canli tetikler icin lisansli kaynak kullanin."
            )
        return normalized

    @field_validator("openrouter_vision_model")
    @classmethod
    def _upgrade_generic_vision_router(cls, v: str) -> str:
        """Eski genel router ayarını gerçek görsel modelle güvenli biçimde değiştirir."""

        normalized = str(v or "").strip()
        if not normalized or normalized.casefold() == "openrouter/free":
            return "google/gemma-4-31b-it:free"
        return normalized

    @field_validator("public_disclosure_provider")
    @classmethod
    def _validate_disclosure_provider(cls, v: str) -> str:
        allowed = {"disabled", "rss"}
        if v not in allowed:
            raise ValueError(
                f"PUBLIC_DISCLOSURE_PROVIDER '{v}' desteklenmiyor. Izin verilenler: {allowed}."
            )
        return v

    @field_validator("fundamental_provider", "fundamental_secondary_provider")
    @classmethod
    def _validate_fundamental_provider(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized == "yfinance":
            normalized = "yahoo"
        allowed = {"disabled", "kap_rest", "fintables_mcp", "yahoo", "auto"}
        if normalized not in allowed:
            raise ValueError(
                f"FUNDAMENTAL_PROVIDER desteklenmiyor: {normalized}. Izin verilenler: {allowed}."
            )
        return normalized

    @field_validator(
        "technical_screener_chat_id",
        "max_market_data_staleness_seconds",
        "backtest_commission_rate",
        "backtest_commission_minimum",
        "backtest_commission_tax_rate",
        mode="before",
    )
    @classmethod
    def _empty_optional_number_is_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("max_market_data_staleness_seconds")
    @classmethod
    def _validate_optional_staleness(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 86400:
            raise ValueError(
                "MAX_MARKET_DATA_STALENESS_SECONDS 1-86400 araliginda olmali veya bos birakilmali."
            )
        return v

    @field_validator(
        "backtest_commission_rate",
        "backtest_commission_minimum",
        "backtest_commission_tax_rate",
    )
    @classmethod
    def _validate_optional_cost(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("Backtest komisyon/vergi degerleri negatif olamaz.")
        return v

    @field_validator("backtest_commission_rate", "backtest_commission_tax_rate")
    @classmethod
    def _validate_optional_rate(cls, v: float | None) -> float | None:
        if v is not None and v > 1:
            raise ValueError("Backtest komisyon ve vergi oranları 0 ile 1 arasında olmalıdır.")
        return v

    @field_validator("backtest_entry_mode")
    @classmethod
    def _validate_backtest_entry_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        allowed = {"next_session_level_touch", "next_open", "next_vwap", "next_close"}
        if normalized not in allowed:
            raise ValueError(f"BACKTEST_ENTRY_MODE desteklenmiyor: {normalized}")
        return normalized

    @field_validator("backtest_intrabar_mode")
    @classmethod
    def _validate_backtest_intrabar_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        allowed = {
            "lower_timeframe_then_conservative",
            "conservative",
            "optimistic",
            "nearest_to_open",
        }
        if normalized not in allowed:
            raise ValueError(f"BACKTEST_INTRABAR_MODE desteklenmiyor: {normalized}")
        return normalized

    @field_validator("backtest_fill_model")
    @classmethod
    def _validate_backtest_fill_model(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"full_fill", "volume_limited", "conservative_volume_limited"}:
            raise ValueError(f"BACKTEST_FILL_MODEL desteklenmiyor: {normalized}")
        return normalized

    @field_validator("backtest_limit_lock_mode")
    @classmethod
    def _validate_backtest_limit_lock_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"conservative", "volume_confirmed", "disabled"}:
            raise ValueError(f"BACKTEST_LIMIT_LOCK_MODE desteklenmiyor: {normalized}")
        return normalized

    @field_validator("backtest_price_mode")
    @classmethod
    def _validate_backtest_price_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"split_adjusted", "adjusted", "raw"}:
            raise ValueError(f"BACKTEST_PRICE_MODE desteklenmiyor: {normalized}")
        return normalized

    @field_validator("technical_price_mode")
    @classmethod
    def _validate_price_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"adjusted", "unadjusted"}:
            raise ValueError("TECHNICAL_PRICE_MODE 'adjusted' veya 'unadjusted' olmalı.")
        return normalized

    @field_validator("price_adjustment_mode")
    @classmethod
    def _validate_adjustment_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"adjusted", "raw"}:
            raise ValueError("PRICE_ADJUSTMENT_MODE 'adjusted' veya 'raw' olmalı.")
        return normalized

    @field_validator("backtest_intrabar_policy")
    @classmethod
    def _validate_backtest_intrabar_policy(cls, v: str) -> str:
        normalized = v.strip().lower()
        allowed = {"conservative", "optimistic", "nearest_to_open", "lower_timeframe"}
        if normalized not in allowed:
            raise ValueError(f"BACKTEST_INTRABAR_POLICY desteklenmiyor: {normalized}")
        return normalized

    @field_validator("backtest_entry_model")
    @classmethod
    def _validate_backtest_entry_model(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"next_open", "next_vwap", "next_close"}:
            raise ValueError(f"BACKTEST_ENTRY_MODEL desteklenmiyor: {normalized}")
        return normalized

    @field_validator("walk_forward_mode")
    @classmethod
    def _validate_walk_forward_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"rolling", "expanding"}:
            raise ValueError("WALK_FORWARD_MODE 'rolling' veya 'expanding' olmali.")
        return normalized

    @field_validator(
        "timeframe_weight_weekly", "timeframe_weight_daily", "timeframe_weight_4h",
        "timeframe_weight_1h", "timeframe_weight_15m", "timeframe_weight_5m",
    )
    @classmethod
    def _validate_timeframe_weight(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Zaman dilimi ağırlığı negatif olamaz.")
        return v

    @field_validator("chart_theme")
    @classmethod
    def _validate_chart_theme(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"light", "dark"}:
            raise ValueError("CHART_THEME 'light' veya 'dark' olmalı.")
        return normalized

    @model_validator(mode="after")
    def _forbid_mock_in_production(self):
        if self.app_env.strip().lower() in {"production", "prod"} and self.market_data_provider == "mock":
            raise ValueError("Production ortamında MARKET_DATA_PROVIDER=mock kullanılamaz.")
        if self.user_price_alert_default_repeat_seconds < self.user_price_alert_min_repeat_seconds:
            raise ValueError(
                "USER_PRICE_ALERT_DEFAULT_REPEAT_SECONDS, "
                "USER_PRICE_ALERT_MIN_REPEAT_SECONDS degerinden kucuk olamaz."
            )
        allocation_total = (
            self.default_tp1_allocation
            + self.default_tp2_allocation
            + self.default_tp3_allocation
        )
        if abs(allocation_total - 100.0) > 1e-9:
            raise ValueError("DEFAULT_TP1/TP2/TP3_ALLOCATION toplami tam olarak 100 olmali.")
        return self

    @property
    def admin_ids(self) -> List[int]:
        raw = self.admin_telegram_user_ids.strip()
        if not raw:
            return []
        result = []
        for part in raw.split(","):
            part = part.strip()
            if part:
                result.append(int(part))
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_strategy_config() -> dict:
    path = CONFIG_DIR / "strategy.yaml"
    if not path.exists():
        raise RuntimeError(f"Strateji config dosyasi bulunamadi: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    required_sections = ["strategy", "timeframes", "thresholds", "risk", "filters"]
    for section in required_sections:
        if section not in data:
            raise RuntimeError(
                f"strategy.yaml gecersiz: zorunlu bolum eksik -> '{section}'. "
                "Hatali config ile uygulama baslatilamaz."
            )

    thresholds = data["thresholds"]
    numeric_threshold_keys = [
        "strong_buy_score",
        "buy_score",
        "watch_score",
        "risk_reduce_score",
        "sell_risk_score",
        "minimum_relative_volume",
        "minimum_risk_reward",
        "signal_cooldown_minutes",
    ]
    for key in numeric_threshold_keys:
        if key not in thresholds:
            raise RuntimeError(f"strategy.yaml thresholds bolumunde '{key}' eksik.")

    risk = data["risk"]
    if not (0 < risk["risk_per_trade_percent"] <= 100):
        raise RuntimeError("risk_per_trade_percent 0-100 araliginda olmali.")

    return data


def load_sector_map(path: str | None = None) -> dict:
    """Sektor eslestirme dosyasini yukler. Dosya yoksa bos sozluk doner
    (uydurma sektor atamasi YAPILMAZ; 'Sektor eslestirmesi bulunamadi'
    davranisi cagiran taraftaki servis katmaninda uygulanir).
    """
    target = Path(path) if path else (BASE_DIR / "app" / "config" / "sector_map.yaml")
    if not target.exists():
        return {"symbols": {}}
    with open(target, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "symbols" not in data:
        data["symbols"] = {}
    return data


def save_sector_map(data: dict, path: str | None = None) -> None:
    target = Path(path) if path else (BASE_DIR / "app" / "config" / "sector_map.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def load_company_aliases(path: str | None = None) -> dict:
    """Sirket adi / alternatif isim eslestirme tablosunu yukler (bolum 1, Asama 4).

    Dosya yoksa veya bir sembol icin kayit yoksa UYDURMA YAPILMAZ: bos sozluk /
    eksik anahtar doner, cagiran taraf bunu 'sirket eslestirmesi yok' olarak
    isler ve o sembol icin haber aramasi yapmaz.
    """
    target = Path(path) if path else (BASE_DIR / "app" / "config" / "company_aliases.yaml")
    if not target.exists():
        return {"symbols": {}}
    with open(target, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "symbols" not in data:
        data["symbols"] = {}
    return data
