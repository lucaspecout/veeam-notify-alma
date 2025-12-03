import email
from datetime import datetime
import imaplib
from email.header import decode_header
from typing import List

from flask import current_app

from . import db
from .models import Client, EmailConfig, STATUS_FAILED, STATUS_MISSING, STATUS_OK, STATUS_WARNING


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


def find_matching_subject(message_ids: List[bytes], client: Client, mail: imaplib.IMAP4) -> tuple[str | None, str | None]:
    matched_subject = None
    note = None
    for msg_id in reversed(message_ids):
        status, msg_data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data:
            note = "Impossible de récupérer le message." if not note else note
            continue
        raw_email = msg_data[0][1]
        message = email.message_from_bytes(raw_email)
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

        if not config.imap_host or not config.imap_username or not config.imap_password:
            for client in clients:
                client.last_status = STATUS_MISSING
                client.last_checked_at = datetime.utcnow()
                client.last_note = "Configuration IMAP incomplète."
            db.session.commit()
            return

        try:
            mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port) if config.use_ssl else imaplib.IMAP4(config.imap_host, config.imap_port)
            mail.login(config.imap_username, config.imap_password)
            mail.select("INBOX")
            today = datetime.now().strftime("%d-%b-%Y")
            status, search_data = mail.search(None, f'(SINCE "{today}")')
            if status != "OK":
                raise RuntimeError("Impossible de parcourir la boîte mail.")
            message_ids = search_data[0].split()

            for client in clients:
                matched_subject, note = find_matching_subject(message_ids, client, mail)
                if matched_subject:
                    client.last_status = extract_status_from_subject(matched_subject)
                    client.last_subject = matched_subject
                    client.last_note = None
                else:
                    client.last_status = STATUS_MISSING
                    client.last_subject = None
                    client.last_note = note or "Aucun message du jour ne correspond à l'objet attendu."
                client.last_checked_at = datetime.utcnow()

            mail.logout()
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            for client in clients:
                client.last_status = STATUS_MISSING
                client.last_checked_at = datetime.utcnow()
                client.last_note = f"Erreur IMAP: {exc}"
            db.session.commit()
