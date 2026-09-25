"""Configurare centralizata a aplicatiei, incarcata din variabile de mediu.

Toate valorile implicite sunt sigure pentru dezvoltare locala. In productie,
variabilele critice (SECRET_KEY, DATABASE_URL, parolele) trebuie furnizate
explicit -- nu exista parole implicite publice.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General ---
    app_name: str = "EMS Platform"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    secret_key: str = Field(
        default="dev-only-insecure-secret-change-me",
        description="Cheie folosita pentru semnarea sesiunilor si a token-urilor CSRF.",
    )
    base_url: str = "http://localhost:8000"
    default_timezone: str = "Europe/Bucharest"

    # --- Database ---
    database_url: str = "postgresql+psycopg://ems:ems@localhost:5432/ems"
    database_pool_size: int = 10
    database_max_overflow: int = 10
    sql_echo: bool = False

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # --- Sessions / cookies ---
    session_cookie_name: str = "ems_session"
    session_cookie_secure: bool = False
    session_ttl_hours: int = 24 * 14
    csrf_cookie_name: str = "ems_csrf"

    # --- Auth ---
    bootstrap_admin_token: str | None = Field(
        default=None,
        description=(
            "Token cu o singura folosire necesar pentru a crea primul cont platform_admin."
            " Trebuie generat manual si furnizat prin variabila de mediu; nu exista implicit."
        ),
    )
    password_reset_token_ttl_minutes: int = 60
    invite_token_ttl_hours: int = 72
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 300
    # In lipsa unui SMTP configurat (issue #149), invitatiile pot fi livrate
    # printr-un link copiabil afisat o singura data unui actor autorizat, in
    # loc sa se bazeze pe un email nelivrabil in productie:
    # - "email": comportamentul istoric, trimite prin adaptorul configurat;
    # - "manual_link": nu apeleaza deloc adaptorul de email pentru invitatii;
    #   ruta care creeaza/regenereaza afiseaza URL-ul complet o singura data.
    # Reset-ul de parola NU foloseste acest mod -- ramane pe adaptorul de
    # email (vezi `email_deliverable` mai jos), ca sa nu expuna public un
    # link de resetare a parolei.
    invitation_delivery_mode: Literal["email", "manual_link"] = "email"

    # --- Email adapter ---
    email_backend: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str = "no-reply@ems-platform.local"
    smtp_use_tls: bool = True

    notifications_email_enabled: bool = False
    notifications_push_enabled: bool = False
    notifications_vapid_private_key: str | None = None
    notifications_vapid_public_key: str | None = None
    notifications_vapid_subject: str | None = None
    energy_assistant_enabled: bool = False

    # --- Device API ---
    device_claim_code_ttl_minutes: int = 15
    device_telemetry_batch_max_items: int = 500
    device_api_rate_limit_per_minute: int = 120
    device_max_payload_bytes: int = 262_144  # 256 KiB
    # Cat ramane un enrollment automat (issue #16) in starea "pending" fara
    # sa fie alocat de un admin, inainte de a deveni expirat si a necesita
    # reenrollment. Separat de device_claim_code_ttl_minutes (codul clasic,
    # generat de operator).
    device_enrollment_ttl_hours: int = 72
    device_activation_attempts_per_hour: int = 10
    # Fluxul clasic cu cod de asociere manual (`ClaimCode`, 15 minute, doar
    # cunoastere = posesie) ramas din perioada dinaintea Device Code-ului
    # sigilat (issue #44/#59). Este dezactivat implicit in orice mediu;
    # testele sau simulatoarele legacy trebuie sa-l activeze explicit --
    # NICIODATA in productie, unde `model_post_init` refuza pornirea daca e
    # activat. Cand e dezactivat, atat ruta web (`/stations/{id}/claim-codes`)
    # cat si cea de dispozitiv (`POST /api/v1/devices/claim`) refuza cererea
    # explicit, in loc sa raspunda tacit cu succes.
    legacy_claim_code_enabled: bool = False

    # --- OPCOM ---
    opcom_base_url: str = "https://www.opcom.ro/rapoarte-pzu-raportPIP-export-csv"
    opcom_request_timeout_seconds: float = 20.0
    opcom_max_retries: int = 4
    opcom_use_synthetic_fixture_on_failure: bool = False

    # --- Weather ---
    weather_provider: Literal["open-meteo"] = "open-meteo"
    weather_base_url: str = "https://api.open-meteo.com/v1/forecast"
    weather_request_timeout_seconds: float = 15.0
    weather_max_retries: int = 3
    weather_retry_backoff_seconds: float = 0.2
    weather_cache_ttl_minutes: int = 30
    weather_rate_limit_per_minute: int = 300

    # --- Optimization ---
    optimization_horizon_hours: int = 36
    optimization_interval_minutes: int = 15
    optimization_solver_timeout_seconds: float = 30.0
    optimization_shadow_mode_default: bool = True
    # Prag de prospetime pentru ultima telemetrie SOC folosita ca punct de plecare
    # al optimizarii. SOC lipsa/invechit peste acest prag blocheaza planul LIVE
    # (raman doar planuri shadow, calculate cu ultima valoare cunoscuta sau o
    # presupunere documentata drept atare).
    optimization_soc_max_age_minutes: int = 10

    # --- Deye Cloud (issue #43) ---
    # appId/appSecret sunt configurate per conexiune de statie, in UI-ul Deye
    # Cloud, si stocate criptat unde e cazul. Setarile globale de mai jos
    # controleaza doar regiunea, endpoint-ul, timeout/retry si limitele locale.
    # Doar UE in aceasta versiune (vezi docs/LIMITATIONS.md) -- celelalte
    # centre de date documentate de Deye (am/india) raman nefolosite.
    deye_cloud_region: Literal["eu"] = "eu"
    deye_cloud_base_url: str = "https://eu1-developer.deyecloud.com"
    deye_cloud_request_timeout_seconds: float = 20.0
    deye_cloud_max_retries: int = 3
    deye_cloud_connect_attempts_per_hour: int = 10
    # Cat de recenta trebuie sa fie ultima telemetrie de la un dispozitiv EMS
    # local (RS485) ca sa fie considerat "activ" si sa aiba prioritate fata
    # de Deye Cloud pentru aceeasi statie -- acelasi prag ca detectia
    # device_offline din `alerts_task`.
    deye_cloud_local_device_active_minutes: int = 15

    # --- Retention ---
    telemetry_raw_retention_days: int = 90
    telemetry_aggregate_retention_days: int = 730
    audit_log_retention_days: int = 365

    # --- Firmware OTA fleet management (issue #168) ---
    # "Firmware" is strictly the EMS-device-code AGENT installed on the
    # Raspberry Pi -- never DEYE inverter firmware, never Raspberry Pi OS.
    # Artifacts are never stored on Railway's ephemeral disk: `local_dev_only`
    # mirrors `email_backend="console"` (safe for local dev, refused at
    # startup in production -- see model_post_init below); `s3` targets any
    # S3-compatible provider via `firmware_s3_endpoint_url`.
    firmware_storage_backend: Literal["s3", "local_dev_only"] = "local_dev_only"
    firmware_s3_bucket: str | None = None
    firmware_s3_region: str | None = None
    firmware_s3_endpoint_url: str | None = None
    firmware_s3_access_key_id: str | None = None
    firmware_s3_secret_access_key: str | None = None
    firmware_local_storage_dir: str = "./.data/firmware-releases-dev-only"
    firmware_download_url_ttl_seconds: int = 300
    firmware_max_artifact_bytes: int = 200 * 1024 * 1024  # 200 MiB
    # Ed25519 signing key (PEM PKCS8 private key), provided ONLY via
    # environment -- never stored in the DB, never logged. Publishing a
    # release without this configured is refused explicitly (see
    # app/services/firmware_signing.py), so most of the app/tests never
    # need it set.
    firmware_signing_private_key_pem: str | None = None
    firmware_signing_key_id: str = "dev-unconfigured"
    firmware_deployment_offer_ttl_minutes: int = 60
    firmware_deployment_confirmation_timeout_minutes: int = 15

    # --- Demo mode ---
    demo_mode_enabled: bool = False

    @field_validator("environment")
    @classmethod
    def _validate_prod_secret(cls, v: str, info):
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def email_deliverable(self) -> bool:
        """True doar daca exista un adaptor care poate livra efectiv un
        email unui destinatar extern. Backend-ul 'console' scrie doar in
        loguri (si acelea redactate) -- niciun UI nu trebuie sa pretinda ca
        a "trimis" ceva cand backend-ul e 'console' (issue #149: reset de
        parola fara SMTP)."""
        return self.email_backend == "smtp"

    @property
    def invitation_manual_link(self) -> bool:
        return self.invitation_delivery_mode == "manual_link"

    @property
    def celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def celery_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        """Railway (si alte PaaS) furnizeaza DATABASE_URL ca 'postgres://' sau
        'postgresql://' fara driver -- normalizam la driverul psycopg3 folosit
        de aplicatie, ca sa nu fie nevoie de o transformare manuala in fiecare mediu."""
        if v.startswith("postgres://"):
            return "postgresql+psycopg://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            return "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    def model_post_init(self, __context) -> None:
        if self.notifications_email_enabled and (self.email_backend != "smtp" or not self.smtp_host):
            raise ValueError("Notificarile email necesita SMTP configurat.")
        if self.notifications_push_enabled and not all((self.notifications_vapid_private_key, self.notifications_vapid_public_key, self.notifications_vapid_subject)):
            raise ValueError("Notificarile push necesita cheile VAPID si contactul configurate.")
        if self.is_production and self.secret_key == "dev-only-insecure-secret-change-me":
            raise RuntimeError(
                "SECRET_KEY implicit nu poate fi folosit in productie. "
                "Seteaza variabila de mediu SECRET_KEY cu o valoare unica si secreta."
            )
        if self.is_production and self.demo_mode_enabled:
            raise RuntimeError("DEMO_MODE_ENABLED nu poate fi activat in productie.")
        if self.is_production and self.opcom_use_synthetic_fixture_on_failure:
            raise RuntimeError("Datele OPCOM sintetice nu pot fi activate in productie.")
        if self.is_production and not self.session_cookie_secure:
            raise RuntimeError("SESSION_COOKIE_SECURE trebuie activat in productie.")
        if self.is_production and self.email_backend == "console" and self.invitation_delivery_mode == "email":
            raise RuntimeError(
                "EMAIL_BACKEND=console nu poate fi folosit in productie cu INVITATION_DELIVERY_MODE=email "
                "(niciun email nu ar fi livrat efectiv). Configureaza SMTP sau seteaza "
                "INVITATION_DELIVERY_MODE=manual_link pentru a invita membri printr-un link copiabil, fara SMTP."
            )
        if self.is_production and self.legacy_claim_code_enabled:
            raise RuntimeError(
                "Fluxul legacy de asociere prin cod manual (15 minute) nu poate fi activat in productie. "
                "Seteaza LEGACY_CLAIM_CODE_ENABLED=false (implicit dezactivat trebuie confirmat explicit "
                "doar pentru medii non-productie)."
            )
        if self.is_production and self.firmware_storage_backend == "local_dev_only":
            raise RuntimeError(
                "FIRMWARE_STORAGE_BACKEND=local_dev_only nu poate fi folosit in productie -- artifactele "
                "de firmware nu pot fi pastrate pe discul efemer Railway. Seteaza FIRMWARE_STORAGE_BACKEND=s3 "
                "si variabilele FIRMWARE_S3_*."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
