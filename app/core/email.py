"""Adaptor de email configurabil. Implicit 'console' (scrie in log, util pentru
dev/demo fara SMTP real). Pentru productie, configureaza SMTP_* si
EMAIL_BACKEND=smtp."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


class EmailAdapter:
    def send(self, to: str, subject: str, body: str) -> None:
        raise NotImplementedError


class ConsoleEmailAdapter(EmailAdapter):
    def send(self, to: str, subject: str, body: str) -> None:
        logger.info("email.console_send", to=to, subject=subject, body=body)


class SmtpEmailAdapter(EmailAdapter):
    def __init__(self, host: str, port: int, username: str | None, password: str | None, use_tls: bool, from_addr: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.from_addr = from_addr

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)


def get_email_adapter() -> EmailAdapter:
    settings = get_settings()
    if settings.email_backend == "smtp":
        if not settings.smtp_host:
            raise RuntimeError("EMAIL_BACKEND=smtp necesita SMTP_HOST configurat.")
        return SmtpEmailAdapter(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            from_addr=settings.smtp_from_address,
        )
    return ConsoleEmailAdapter()
