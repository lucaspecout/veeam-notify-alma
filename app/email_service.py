import email
import os
import smtplib
from datetime import datetime, timedelta
import imaplib
from email.header import decode_header
from email.message import EmailMessage
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


def extract_status_from_subject(subject: str, client: Client) -> str | None:
    subject_lower = subject.lower()
    expected_pairs = [
        (STATUS_FAILED, client.subject_failed),
        (STATUS_WARNING, client.subject_warning),
        (STATUS_OK, client.subject_ok),
    ]

    for status, expected in expected_pairs:
        if expected and expected.lower() in subject_lower:
            return status

    return None


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
) -> tuple[str | None, str | None, str | None]:
    matched_subject = None
    matched_status = None
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
        matched_status = extract_status_from_subject(subject, client)
        if matched_status:
            matched_subject = subject
            break
    return matched_subject, note, matched_status


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
                matched_subject, note, matched_status = find_matching_subject(
                    message_ids, client, mail, start_time, now, tz
                )
                if matched_subject:
                    client.last_status = matched_status or STATUS_OK
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


def build_status_report(clients: list[Client], tz: ZoneInfo) -> str:
    header = ["Rapport de statut Veeam", "======================", ""]
    lines = header
    now = datetime.now(tz=tz)
    lines.append(f"Généré le {now.strftime('%d/%m/%Y %H:%M')} ({tz})")
    lines.append("")
    for client in clients:
        checked_at = (
            client.last_checked_at.strftime("%d/%m/%Y %H:%M")
            if client.last_checked_at
            else "Jamais vérifié"
        )
        lines.append(f"- {client.name}: {client.status_label()}")
        lines.append(f"  Dernier sujet : {client.last_subject or '—'}")
        lines.append(f"  Dernière vérification : {checked_at}")
        if client.last_note:
            lines.append(f"  Note : {client.last_note}")
        lines.append("")

    return "\n".join(lines)


def send_status_report(app=None) -> tuple[bool, str]:
    app = app or current_app._get_current_object()
    with app.app_context():
        config = EmailConfig.get_singleton()
        tz = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
        recipients = [
            email.strip()
            for email in (config.report_recipients or "").split(",")
            if email.strip()
        ]

        if not recipients:
            message = "Aucun destinataire configuré pour le rapport."
            add_log(message, level="warning")
            return False, message

        missing_smtp = not (
            config.smtp_host and config.smtp_port and config.smtp_username and config.smtp_password
        )
        if missing_smtp:
            message = "Configuration SMTP incomplète pour l'envoi du rapport."
            add_log(message, level="error")
            return False, message

        clients = Client.query.order_by(Client.name).all()
        body = build_status_report(clients, tz)

        msg = EmailMessage()
        msg["Subject"] = f"Rapport Veeam - {datetime.now(tz=tz).strftime('%d/%m/%Y %H:%M')}"
        msg["From"] = config.smtp_username
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)

        server = None
        try:
            use_ssl_direct = config.use_ssl and config.smtp_port == 465
            if use_ssl_direct:
                server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10)
                if config.use_ssl:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
            server.login(config.smtp_username, config.smtp_password)
            server.send_message(msg)
            add_log(f"Rapport envoyé à {len(recipients)} destinataire(s).")
            return True, "Rapport envoyé avec succès."
        except Exception as exc:  # noqa: BLE001
            message = f"Échec de l'envoi du rapport : {exc}"
            add_log(message, level="error")
            return False, message
        finally:
            if server:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
