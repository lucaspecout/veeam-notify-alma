import email
import os
from datetime import datetime, timedelta
import imaplib
from email.header import decode_header
from typing import List
from zoneinfo import ZoneInfo

from flask import current_app

from . import db
from .models import (
    Client,
    EmailConfig,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_WARNING,
    add_log,
)


def decode_subject(raw_subject: str) -> str:
    decoded_parts = decode_header(raw_subject)
    subject = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            subject += part.decode(encoding or "utf-8", errors="ignore")
        else:
            subject += part or ""
    return subject


def extract_status_from_subject(subject: str) -> str:
    lowered = subject.lower()
    if "[failed]" in lowered:
        return STATUS_FAILED
    if "[warning]" in lowered:
        return STATUS_WARNING
    return STATUS_OK


def parse_email_date(date_header: str | None, tz: ZoneInfo) -> datetime | None:
    if not date_header:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_header)
    except Exception:  # noqa: BLE001
        return None
    if not parsed:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def find_matching_subject(
    message_ids: List[bytes],
    client: Client,
    mail: imaplib.IMAP4,
    start_time: datetime,
    end_time: datetime,
    tz: ZoneInfo,
) -> tuple[str | None, str | None]:
    matched_subject = None
    note = None
    for msg_id in reversed(message_ids):
        status, msg_data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data:
            note = "Impossible de récupérer le message." if not note else note
            continue
        raw_email = msg_data[0][1]
        message = email.message_from_bytes(raw_email)
        received_at = parse_email_date(message.get("Date"), tz)
        if not received_at:
            note = note or "Date du message introuvable."
            continue
        if received_at and (received_at < start_time or received_at > end_time):
            continue
        subject = decode_subject(message.get("Subject", ""))
        if client.expected_subject.lower() in subject.lower():
            matched_subject = subject
            break
    return matched_subject, note


def run_email_checks(app=None):
    app = app or current_app._get_current_object()
    with app.app_context():
        clients = Client.query.all()
        config = EmailConfig.get_singleton()
        tz = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
        now = datetime.now(tz=tz)
        start_time = (now - timedelta(days=1)).replace(
            hour=16, minute=0, second=0, microsecond=0
        )

        if not config.imap_host or not config.imap_username or not config.imap_password:
            for client in clients:
                client.last_status = STATUS_MISSING
                client.last_checked_at = now
                client.last_note = "Configuration IMAP incomplète."
            db.session.commit()
            add_log("Vérification impossible : configuration IMAP incomplète.", level="warning")
            return

        try:
            if config.use_ssl:
                mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
            else:
                mail = imaplib.IMAP4(config.imap_host, config.imap_port)
            mail.login(config.imap_username, config.imap_password)
            mail.select("INBOX")
            date_filter = start_time.strftime("%d-%b-%Y")
            status, search_data = mail.search(None, f'(SINCE "{date_filter}")')
            if status != "OK":
                raise RuntimeError("Impossible de parcourir la boîte mail.")
            message_ids = search_data[0].split()

            for client in clients:
                matched_subject, note = find_matching_subject(
                    message_ids, client, mail, start_time, now, tz
                )
                if matched_subject:
                    client.last_status = extract_status_from_subject(matched_subject)
                    client.last_subject = matched_subject
                    client.last_note = None
                else:
                    client.last_status = STATUS_MISSING
                    client.last_subject = None
                    window = f"depuis {start_time.strftime('%d/%m %H:%M')} ({tz})"
                    client.last_note = note or f"Aucun message reçu {window} ne correspond à l'objet attendu."
                client.last_checked_at = now

            mail.logout()
            db.session.commit()
            add_log(f"Vérification des emails effectuée pour {len(clients)} clients.")
        except Exception as exc:  # noqa: BLE001
            for client in clients:
                client.last_status = STATUS_MISSING
                client.last_checked_at = now
                client.last_note = f"Erreur IMAP: {exc}"
            db.session.commit()
            add_log(f"Erreur lors de la vérification des emails: {exc}", level="error")
