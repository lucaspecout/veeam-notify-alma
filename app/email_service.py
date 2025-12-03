import email
import html
import imaplib
import os
import re
import smtplib
from datetime import datetime, timedelta
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


def _status_badge(status: str) -> tuple[str, str]:
    palette = {
        STATUS_OK: ("#16a34a", "#e7f7ec"),
        STATUS_WARNING: ("#f59e0b", "#fff7e6"),
        STATUS_FAILED: ("#dc2626", "#fdecec"),
        STATUS_MISSING: ("#6b7280", "#f3f4f6"),
    }
    return palette.get(status, ("#0ea5e9", "#e0f2fe"))


def build_status_report_html(clients: list[Client], tz: ZoneInfo) -> str:
    now = datetime.now(tz=tz)
    header_date = now.strftime("%d/%m/%Y %H:%M")
    rows: list[str] = []
    for client in clients:
        fg, bg = _status_badge(client.status_label())
        checked_at = (
            client.last_checked_at.strftime("%d/%m/%Y %H:%M")
            if client.last_checked_at
            else "Jamais vérifié"
        )
        subject = client.last_subject or "—"
        note = client.last_note or "—"
        rows.append(
            """
            <tr>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;font-weight:600;color:#111827;">{name}</td>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;">
                    <span style="display:inline-block;padding:6px 10px;border-radius:999px;font-weight:700;color:{fg};background:{bg};border:1px solid {fg}1a;">{status}</span>
                </td>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;font-family:'SFMono-Regular',Consolas,monospace;color:#374151;font-size:13px;">{subject}</td>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;color:#374151;">{checked_at}</td>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;color:#4b5563;">{note}</td>
            </tr>
            """.format(
                name=html.escape(client.name),
                status=html.escape(client.status_label()),
                subject=html.escape(subject),
                checked_at=html.escape(checked_at),
                note=html.escape(note),
                fg=fg,
                bg=bg,
            )
        )

    table_body = "".join(rows) or """
        <tr>
            <td colspan="5" style="padding:16px;text-align:center;color:#6b7280;background:#f9fafb;">
                Aucun client n'a été configuré pour le moment.
            </td>
        </tr>
    """

    return f"""
    <!doctype html>
    <html lang=\"fr\">
    <body style=\"margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Helvetica,Arial,sans-serif;\">
        <div style=\"max-width:760px;margin:24px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;box-shadow:0 6px 24px rgba(15,23,42,0.08);\">
            <div style=\"background:linear-gradient(120deg,#2563eb,#7c3aed);color:#f8fafc;padding:18px 22px;\">
                <div style=\"font-size:14px;opacity:0.9;letter-spacing:0.3px;\">Rapport Veeam</div>
                <div style=\"font-size:22px;font-weight:700;margin-top:4px;\">Statut des notifications</div>
                <div style=\"font-size:13px;opacity:0.85;margin-top:6px;\">Généré le {header_date} ({tz})</div>
            </div>
            <div style=\"padding:20px 22px 10px;\">
                <p style=\"margin:0 0 12px;color:#1f2937;font-size:14px;line-height:1.6;\">
                    Voici un récapitulatif des derniers statuts reçus pour vos clients.
                </p>
            </div>
            <div style=\"padding:0 22px 22px;\">
                <table style=\"width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;\">
                    <thead>
                        <tr style=\"background:#f9fafb;border-bottom:1px solid #e5e7eb;\">
                            <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Client</th>
                            <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Statut</th>
                            <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Dernier sujet</th>
                            <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Vérifié le</th>
                            <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Notes</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_body}
                    </tbody>
                </table>
            </div>
            <div style=\"padding:14px 22px 20px;color:#6b7280;font-size:12px;border-top:1px solid #f3f4f6;background:#fbfbff;\">
                Ce message est généré automatiquement par Veeam Notify. Merci de ne pas y répondre directement.
            </div>
        </div>
    </body>
    </html>
    """


def parse_report_recipients(raw_recipients: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[,;\n]+", raw_recipients)
        if part.strip()
    ]


def send_status_report(app=None) -> tuple[bool, str]:
    app = app or current_app._get_current_object()
    with app.app_context():
        config = EmailConfig.get_singleton()
        tz = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
        recipients = parse_report_recipients(config.report_recipients or "")

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
        html_body = build_status_report_html(clients, tz)

        msg = EmailMessage()
        msg["Subject"] = f"Rapport Veeam - {datetime.now(tz=tz).strftime('%d/%m/%Y %H:%M')}"
        msg["From"] = config.smtp_username
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        msg.add_alternative(html_body, subtype="html")

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
