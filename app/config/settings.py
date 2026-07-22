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

    telegram_bot_token: str = Field(default="")
    admin_telegram_user_ids: str = Field(default="")
    telegram_mode: str = Field(default="polling")  # polling | webhook
    telegram_webhook_url: str = Field(default="")
    telegram_webhook_secret: str = Field(default="")

    database_url: str = Field(default="sqlite:///./mergen_quant.db")

    market_data_provider: str = Field(default="mock")  # mock | csv
    kap_provider: str = Field(default="disabled")
    broker_flow_provider: str = Field(default="disabled")
    fundamental_provider: str = Field(default="disabled")
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
    chart_theme: str = Field(default="light")  # light | dark
    chart_cache_ttl_minutes: int = Field(default=30)
    chart_cache_dir: str = Field(default="./data/cache/charts")

    # ---- Asama 5d: gelismis alarm taramasi ----
    enhanced_alarm_scan_enabled: bool = Field(default=True)
    enhanced_alarm_scan_minutes: int = Field(default=15)
    enhanced_alarm_default_cooldown_minutes: int = Field(default=120)
    breakout_minimum_volume_ratio: float = Field(default=1.2)

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

    # ---- Asama 5g: gercekci backtest / walk-forward / sanal islem ----
    backtest_commission_bps: float = Field(default=15.0)
    backtest_slippage_bps: float = Field(default=5.0)
    backtest_spread_bps: float = Field(default=10.0)
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
        allowed = {"mock", "csv", "yfinance"}
        if v not in allowed:
            raise ValueError(
                f"MARKET_DATA_PROVIDER '{v}' desteklenmiyor. Izin verilenler: {allowed}. "
                "'mock' yalnizca test/gelistirme icindir; gercek analiz icin 'yfinance' kullanin."
            )
        return v

    @field_validator("public_disclosure_provider")
    @classmethod
    def _validate_disclosure_provider(cls, v: str) -> str:
        allowed = {"disabled", "rss"}
        if v not in allowed:
            raise ValueError(
                f"PUBLIC_DISCLOSURE_PROVIDER '{v}' desteklenmiyor. Izin verilenler: {allowed}."
            )
        return v

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
