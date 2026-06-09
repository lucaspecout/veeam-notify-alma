import base64
import email
import html
import imaplib
import json
import os
import re
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from binascii import Error as BinasciiError
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from flask import current_app

from . import db
from .models import (
    Client,
    EmailConfig,
    MONITOR_TYPE_SYNOLOGY,
    MONITOR_TYPE_VEEAM,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_WARNING,
    add_log,
)

DEFAULT_WINDOW_START_HOUR = 16
DEFAULT_WINDOW_END_HOUR = 9
OAUTH_EXPIRY_SAFETY_MARGIN_SECONDS = 600


MICROSOFT_SCOPE = "https://outlook.office365.com/.default"
MICROSOFT_USER_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send"


def is_microsoft_oauth_enabled(config: EmailConfig) -> bool:
    return (config.auth_mode or "password") == "microsoft_oauth2"


def _request_microsoft_token(config: EmailConfig, token_data: dict[str, str]) -> dict:
    if not config.ms_tenant_id or not config.ms_client_id:
        raise ValueError("Configuration OAuth Microsoft incomplète.")

    payload = {
        "client_id": config.ms_client_id,
        **token_data,
    }
    if config.ms_client_secret:
        payload["client_secret"] = config.ms_client_secret

    token_url = f"https://login.microsoftonline.com/{config.ms_tenant_id}/oauth2/v2.0/token"
    encoded_payload = urllib.parse.urlencode(payload).encode("utf-8")
    token_request = urllib.request.Request(token_url, data=encoded_payload, method="POST")
    token_request.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(token_request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise ValueError(f"Token Microsoft refusé ({exc.code}): {details}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Impossible de joindre Microsoft Identity: {exc.reason}") from exc

    data = json.loads(body)
    if not data.get("access_token"):
        raise ValueError("Réponse Microsoft sans access_token.")
    return data


def _store_oauth_token(config: EmailConfig, token_response: dict) -> None:
    config.ms_access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    if refresh_token:
        config.ms_refresh_token = refresh_token
    expires_in = int(token_response.get("expires_in") or 3600)
    valid_seconds = max(0, expires_in - OAUTH_EXPIRY_SAFETY_MARGIN_SECONDS)
    config.ms_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=valid_seconds)
    db.session.commit()


def _get_delegated_token(config: EmailConfig, force_refresh: bool = False) -> str | None:
    if not force_refresh and config.ms_access_token and config.ms_token_expires_at:
        expires_at = config.ms_token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc):
            return config.ms_access_token

    if config.ms_refresh_token:
        token_data = _request_microsoft_token(
            config,
            {
                "grant_type": "refresh_token",
                "refresh_token": config.ms_refresh_token,
                "scope": MICROSOFT_USER_SCOPE,
            },
        )
        _store_oauth_token(config, token_data)
        return config.ms_access_token

    return None




def _decode_jwt_payload(access_token: str) -> dict:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except (ValueError, json.JSONDecodeError, BinasciiError):
        return {}


def get_microsoft_token_diagnostics(config: EmailConfig) -> dict[str, str]:
    token = None
    mode = "none"

    delegated_token = _get_delegated_token(config)
    if delegated_token:
        token = delegated_token
        mode = "delegated"
    elif config.ms_client_secret:
        token_data = _request_microsoft_token(
            config,
            {
                "grant_type": "client_credentials",
                "scope": MICROSOFT_SCOPE,
            },
        )
        token = token_data.get("access_token")
        mode = "application"

    if not token:
        return {"mode": mode, "aud": "", "scp": "", "roles": "", "identity": ""}

    claims = _decode_jwt_payload(token)
    roles = claims.get("roles") or []
    if isinstance(roles, list):
        roles_str = ",".join(str(role) for role in roles)
    else:
        roles_str = str(roles)

    return {
        "mode": mode,
        "aud": str(claims.get("aud") or ""),
        "scp": str(claims.get("scp") or ""),
        "roles": roles_str,
        "identity": str(
            claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("appid")
            or ""
        ),
    }

def get_microsoft_access_token(config: EmailConfig, force_refresh: bool = False) -> str:
    delegated_token = _get_delegated_token(config, force_refresh=force_refresh)
    if delegated_token:
        return delegated_token

    if not config.ms_client_secret:
        raise ValueError(
            "Aucun token utilisateur Microsoft disponible. Cliquez sur « Se connecter avec Microsoft 365 » dans les paramètres."
        )

    token_data = _request_microsoft_token(
        config,
        {
            "grant_type": "client_credentials",
            "scope": MICROSOFT_SCOPE,
        },
    )
    return token_data["access_token"]


def build_xoauth2_payload(username: str, access_token: str) -> str:
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01"


def imap_authenticate(
    mail: imaplib.IMAP4, config: EmailConfig, force_refresh: bool = False
) -> None:
    if is_microsoft_oauth_enabled(config):
        access_token = get_microsoft_access_token(config, force_refresh=force_refresh)
        payload = build_xoauth2_payload(config.imap_username or "", access_token)
        mail.authenticate("XOAUTH2", lambda _: payload)
        return

    mail.login(config.imap_username, config.imap_password)


def _open_imap_mailbox(
    config: EmailConfig, mailbox: str = "INBOX", force_token_refresh: bool = False
) -> imaplib.IMAP4:
    if config.use_ssl:
        mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
    else:
        mail = imaplib.IMAP4(config.imap_host, config.imap_port)
    try:
        imap_authenticate(mail, config, force_refresh=force_token_refresh)
    except Exception as exc:  # noqa: BLE001
        if (
            force_token_refresh
            or not is_microsoft_oauth_enabled(config)
            or not _is_access_token_expired_error(exc)
        ):
            _logout_imap(mail)
            raise
        _logout_imap(mail)
        return _open_imap_mailbox(config, mailbox=mailbox, force_token_refresh=True)
    status, _ = mail.select(mailbox)
    if status != "OK":
        raise RuntimeError(f"Impossible d'ouvrir la boîte {mailbox}.")
    return mail


def _logout_imap(mail: imaplib.IMAP4 | None) -> None:
    if not mail:
        return
    try:
        mail.logout()
    except Exception:  # noqa: BLE001
        pass


def _is_access_token_expired_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "accesstokenexpired" in text or "access token expired" in text


def smtp_authenticate(server: smtplib.SMTP, config: EmailConfig) -> None:
    if is_microsoft_oauth_enabled(config):
        access_token = get_microsoft_access_token(config)
        payload = build_xoauth2_payload(config.smtp_username or "", access_token)
        auth_string = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        code, response = server.docmd("AUTH", f"XOAUTH2 {auth_string}")
        if code not in (235, 250):
            raise smtplib.SMTPAuthenticationError(code, response)
        return

    server.login(config.smtp_username, config.smtp_password)


def format_smtp_auth_error(config: EmailConfig, exc: Exception) -> str:
    """Build a human-friendly authentication error for SMTP failures."""
    raw_error = str(exc)
    response_text = ""
    smtp_code = ""

    if isinstance(exc, smtplib.SMTPAuthenticationError):
        smtp_code = str(exc.smtp_code)
        try:
            response_text = exc.smtp_error.decode("utf-8", errors="ignore")
        except AttributeError:
            response_text = str(exc.smtp_error)

    lowered = f"{raw_error} {response_text}".lower()
    is_auth_error = (
        isinstance(exc, smtplib.SMTPAuthenticationError)
        or "5.7.3" in lowered
        or "authentication unsuccessful" in lowered
        or smtp_code == "535"
    )

    if not is_auth_error:
        return raw_error

    if is_microsoft_oauth_enabled(config):
        token_info = get_microsoft_token_diagnostics(config)
        identity = token_info.get("identity") or "inconnue"
        smtp_user = config.smtp_username or "<vide>"
        return (
            "Authentification SMTP Microsoft refusée (535/5.7.3). "
            "Vérifiez que la boîte SMTP est autorisée pour SMTP AUTH et que l'adresse "
            f"SMTP configurée ({smtp_user}) correspond au compte Microsoft connecté ({identity}). "
            "Si besoin, cliquez sur « Se déconnecter », reconnectez le compte Microsoft 365, "
            "puis relancez le test SMTP."
        )

    return (
        "Authentification SMTP refusée (535/5.7.3). "
        "Vérifiez l'adresse SMTP, le mot de passe ou mot de passe d'application, "
        "et que SMTP AUTH est activé sur la boîte."
    )


def _sanitize_hour(value: int | None, default: int) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(23, hour))


def get_window_hours(config: EmailConfig) -> tuple[int, int]:
    start_hour = _sanitize_hour(config.check_window_start_hour, DEFAULT_WINDOW_START_HOUR)
    end_hour = _sanitize_hour(config.check_window_end_hour, DEFAULT_WINDOW_END_HOUR)
    return start_hour, end_hour


def format_window_label(config: EmailConfig) -> str:
    start_hour, end_hour = get_window_hours(config)
    return f"{start_hour:02d}h-{end_hour:02d}h"


def decode_subject(raw_subject: str) -> str:
    decoded_parts = decode_header(raw_subject)
    subject = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            subject += part.decode(encoding or "utf-8", errors="ignore")
        else:
            subject += part or ""
    return subject


def _split_expected_patterns(raw_patterns: str) -> list[str]:
    return [
        pattern.strip().lower()
        for pattern in re.split(r"[;\n|]+", raw_patterns or "")
        if pattern.strip()
    ]


def extract_status_from_subject(subject: str, client: Client) -> str | None:
    subject_lower = subject.lower().strip()
    expected_pairs = [
        (STATUS_FAILED, client.subject_failed),
        (STATUS_WARNING, client.subject_warning),
        (STATUS_OK, client.subject_ok),
    ]

    for status, expected in expected_pairs:
        for expected_lower in _split_expected_patterns(expected):
            if (client.monitor_type or MONITOR_TYPE_VEEAM) == MONITOR_TYPE_SYNOLOGY:
                if expected_lower in subject_lower:
                    return status
            elif subject_lower.startswith(expected_lower):
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


def _fetch_message_headers_in_window(
    mail: imaplib.IMAP4,
    start_time: datetime,
    end_time: datetime,
    tz: ZoneInfo,
    message_ids: list[bytes],
) -> tuple[list[tuple[str, datetime]], str | None]:
    messages: list[tuple[str, datetime]] = []
    note = None
    for msg_id in reversed(message_ids):
        status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)])")
        if status != "OK" or not msg_data:
            note = "Impossible de récupérer le message." if not note else note
            continue

        raw_headers = next(
            (
                item[1]
                for item in msg_data
                if isinstance(item, tuple) and isinstance(item[1], bytes)
            ),
            None,
        )
        if not raw_headers:
            note = "Impossible de lire les en-têtes du message." if not note else note
            continue

        message = email.message_from_bytes(raw_headers)
        received_at = parse_email_date(message.get("Date"), tz)
        if not received_at:
            note = note or "Date du message introuvable."
            continue
        if received_at < start_time or received_at > end_time:
            continue

        subject = decode_subject(message.get("Subject", ""))
        messages.append((subject, received_at))

    return messages, note


def find_matching_subject(
    messages: list[tuple[str, datetime]],
    client: Client,
    note: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, int]:
    matched_subject = None
    matched_status = None
    matched_statuses_summary = None
    email_count = 0
    status_counts: dict[str, int] = {}
    for subject, _received_at in messages:
        matched_status = extract_status_from_subject(subject, client)
        if matched_status:
            if matched_subject is None:
                matched_subject = subject
            status_counts[matched_status] = status_counts.get(matched_status, 0) + 1

    if status_counts:
        email_count = sum(status_counts.values())
        status_order = [STATUS_FAILED, STATUS_WARNING, STATUS_OK]
        matched_status = next(
            (status for status in status_order if status_counts.get(status)), None
        )
        parts = [
            f"{status} ×{status_counts[status]}" if status_counts[status] > 1 else status
            for status in status_order
            if status_counts.get(status)
        ]
        matched_statuses_summary = ", ".join(parts)

    return matched_subject, note, matched_status, matched_statuses_summary, email_count


def run_email_checks(app=None, monitor_type: str | None = None):
    app = app or current_app._get_current_object()
    with app.app_context():
        query = Client.query
        if monitor_type:
            query = query.filter_by(monitor_type=monitor_type)
        clients = query.all()
        config = EmailConfig.get_singleton()
        tz = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
        now = datetime.now(tz=tz)
        start_hour, end_hour = get_window_hours(config)
        start_time = (now - timedelta(days=1)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
        end_time_target = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        end_time = end_time_target if end_time_target < now else now

        missing_imap_password = (not is_microsoft_oauth_enabled(config)) and (not config.imap_password)
        if not config.imap_host or not config.imap_username or missing_imap_password:
            for client in clients:
                client.last_status = STATUS_MISSING
                client.last_checked_at = now
                client.last_note = "Configuration IMAP incomplète."
                client.last_email_count = 0
                client.last_statuses = None
            db.session.commit()
            add_log("Vérification impossible : configuration IMAP incomplète.", level="warning")
            return

        mail = None
        try:
            mail = _open_imap_mailbox(config)
            date_filter = start_time.strftime("%d-%b-%Y")
            status, search_data = mail.search(None, f'(SINCE "{date_filter}")')
            if status != "OK":
                raise RuntimeError("Impossible de parcourir la boîte mail.")
            message_ids = search_data[0].split()

            messages = []
            fetch_note = None
            for attempt in range(2):
                try:
                    messages, fetch_note = _fetch_message_headers_in_window(
                        mail, start_time, end_time, tz, message_ids
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    if not (
                        attempt == 0
                        and is_microsoft_oauth_enabled(config)
                        and _is_access_token_expired_error(exc)
                    ):
                        raise

                    add_log(
                        "Session IMAP Microsoft expirée pendant la lecture initiale. "
                        "Rafraîchissement du token OAuth et reprise de la vérification.",
                        level="warning",
                    )
                    _logout_imap(mail)
                    mail = _open_imap_mailbox(config, force_token_refresh=True)
                    status, search_data = mail.search(None, f'(SINCE "{date_filter}")')
                    if status != "OK":
                        raise RuntimeError("Impossible de reparcourir la boîte mail après reconnexion.")
                    message_ids = search_data[0].split()

            for client in clients:
                (
                    matched_subject,
                    note,
                    matched_status,
                    matched_statuses,
                    email_count,
                ) = find_matching_subject(messages, client, fetch_note)
                client.last_email_count = email_count
                client.last_statuses = matched_statuses
                if matched_subject:
                    client.last_status = matched_status or STATUS_OK
                    client.last_subject = matched_subject
                    client.last_note = None
                    if not matched_statuses:
                        client.last_statuses = matched_status
                        client.last_email_count = 1
                else:
                    client.last_status = STATUS_MISSING
                    client.last_subject = None
                    client.last_statuses = None
                    client.last_email_count = 0
                    client.last_note = (
                        note
                        or f"Aucun message reçu entre {start_time.strftime('%d/%m %H:%M')} et {end_time.strftime('%d/%m %H:%M')} ({tz}) ne correspond au début d'objet attendu."
                    )
                client.last_checked_at = now

            db.session.commit()
            scope = f" ({monitor_type})" if monitor_type else ""
            add_log(f"Vérification des emails effectuée pour {len(clients)} élément(s){scope}.")
        except Exception as exc:  # noqa: BLE001
            for client in clients:
                client.last_status = STATUS_MISSING
                client.last_checked_at = now
                client.last_note = f"Erreur IMAP: {exc}"
            db.session.commit()
            add_log(f"Erreur lors de la vérification des emails: {exc}", level="error")
        finally:
            _logout_imap(mail)


def build_status_report(
    clients: list[Client],
    tz: ZoneInfo,
    window_label: str,
    title: str = "Rapport de statut Veeam",
    extra_sections: list[tuple[str, str, list[Client]]] | None = None,
) -> str:
    header = [title, "=" * len(title), ""]
    lines = header
    now = datetime.now(tz=tz)
    lines.append(f"Généré le {now.strftime('%d/%m/%Y %H:%M')} ({tz})")
    lines.append("")

    def add_client_lines(section_clients: list[Client]) -> None:
        for client in section_clients:
            checked_at = (
                client.last_checked_at.strftime("%d/%m/%Y %H:%M")
                if client.last_checked_at
                else "Jamais vérifié"
            )
            lines.append(f"- {client.name}: {client.status_label()}")
            lines.append(f"  Dernier sujet : {client.last_subject or '—'}")
            lines.append(
                f"  Statuts reçus ({window_label}) : "
                f"{client.last_statuses or '—'} ({client.last_email_count or 0} mail(s))"
            )
            lines.append(f"  Dernière vérification : {checked_at}")
            if client.last_note:
                lines.append(f"  Note : {client.last_note}")
            lines.append("")

    add_client_lines(clients)

    for section_title, _item_label, section_clients in extra_sections or []:
        lines.append(section_title)
        lines.append("=" * len(section_title))
        lines.append("")
        add_client_lines(section_clients)

    return "\n".join(lines)


def _build_status_table_html(
    clients: list[Client],
    window_label: str,
    item_label: str,
) -> str:
    rows: list[str] = []
    for client in clients:
        checked_at = (
            client.last_checked_at.strftime("%d/%m/%Y %H:%M")
            if client.last_checked_at
            else "Jamais vérifié"
        )
        fg, bg = _status_badge(client.status_label())
        subject = client.last_subject or "—"
        note = client.last_note or "—"
        statuses = client.last_statuses or "—"
        email_count = client.last_email_count or 0
        rows.append(
            """
            <tr>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;font-weight:600;color:#111827;">{name}</td>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;">
                    <span style="display:inline-block;padding:6px 10px;border-radius:999px;font-weight:700;color:{fg};background:{bg};border:1px solid {fg}1a;">{status}</span>
                </td>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;color:#374151;">{statuses}</td>
                <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;color:#111827;font-weight:600;">{email_count}</td>
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
                statuses=html.escape(statuses),
                email_count=email_count,
                fg=fg,
                bg=bg,
            )
        )

    table_body = "".join(rows) or """
        <tr>
            <td colspan="7" style="padding:16px;text-align:center;color:#6b7280;background:#f9fafb;">
                Aucun élément n'a été configuré pour le moment.
            </td>
        </tr>
    """

    return f"""
        <table style=\"width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;\">
            <thead>
                <tr style=\"background:#f9fafb;border-bottom:1px solid #e5e7eb;\">
                    <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">{html.escape(item_label)}</th>
                    <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Statut</th>
                    <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Statuts ({html.escape(window_label)})</th>
                    <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Mails reçus</th>
                    <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Dernier sujet</th>
                    <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Vérifié le</th>
                    <th style=\"padding:12px 14px;text-align:left;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;\">Notes</th>
                </tr>
            </thead>
            <tbody>
                {table_body}
            </tbody>
        </table>
    """


def _status_badge(status: str) -> tuple[str, str]:
    palette = {
        STATUS_OK: ("#16a34a", "#e7f7ec"),
        STATUS_WARNING: ("#f59e0b", "#fff7e6"),
        STATUS_FAILED: ("#dc2626", "#fdecec"),
        STATUS_MISSING: ("#6b7280", "#f3f4f6"),
    }
    return palette.get(status, ("#0ea5e9", "#e0f2fe"))


def build_status_report_html(
    clients: list[Client],
    tz: ZoneInfo,
    window_label: str,
    title: str = "Rapport Veeam",
    subtitle: str = "Statut des notifications",
    item_label: str = "Client",
    extra_sections: list[tuple[str, str, list[Client]]] | None = None,
) -> str:
    now = datetime.now(tz=tz)
    header_date = now.strftime("%d/%m/%Y %H:%M")
    main_table = _build_status_table_html(clients, window_label, item_label)
    extra_sections_html = ""
    for section_title, section_item_label, section_clients in extra_sections or []:
        extra_sections_html += f"""
            <div style=\"padding:0 22px 10px;\">
                <h2 style=\"margin:4px 0 12px;color:#111827;font-size:18px;\">{html.escape(section_title)}</h2>
                {_build_status_table_html(section_clients, window_label, section_item_label)}
            </div>
        """

    return f"""
    <!doctype html>
    <html lang=\"fr\">
    <body style=\"margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Helvetica,Arial,sans-serif;\">
        <div style=\"max-width:760px;margin:24px auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;box-shadow:0 6px 24px rgba(15,23,42,0.08);\"> 
            <div style=\"background:linear-gradient(120deg,#e5e7eb,#f8fafc);color:#0f172a;padding:18px 22px;\">
                <div style=\"font-size:14px;opacity:0.9;letter-spacing:0.3px;\">{html.escape(title)}</div>
                <div style=\"font-size:22px;font-weight:700;margin-top:4px;\">{html.escape(subtitle)}</div>
                <div style=\"font-size:13px;opacity:0.85;margin-top:6px;\">Généré le {header_date} ({tz})</div>
            </div>
            <div style=\"padding:20px 22px 10px;\">
                <p style=\"margin:0 0 12px;color:#1f2937;font-size:14px;line-height:1.6;\">
                    Voici un récapitulatif des derniers statuts reçus.
                </p>
            </div>
            <div style=\"padding:0 22px 22px;\">
                {main_table}
            </div>
            {extra_sections_html}
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


def send_status_report(
    app=None,
    monitor_type: str = MONITOR_TYPE_VEEAM,
    report_title: str = "Rapport de statut Veeam",
    mail_subject_prefix: str = "Rapport Veeam",
    html_title: str = "Rapport Veeam",
    item_label: str = "Client",
    include_synology_section: bool | None = None,
) -> tuple[bool, str]:
    app = app or current_app._get_current_object()
    with app.app_context():
        config = EmailConfig.get_singleton()
        tz = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
        recipients = parse_report_recipients(config.report_recipients or "")

        if not recipients:
            message = "Aucun destinataire configuré pour le rapport."
            add_log(message, level="warning")
            return False, message

        missing_smtp_password = (not is_microsoft_oauth_enabled(config)) and (not config.smtp_password)
        missing_smtp = not (
            config.smtp_host and config.smtp_port and config.smtp_username
        ) or missing_smtp_password
        if missing_smtp:
            message = "Configuration SMTP incomplète pour l'envoi du rapport."
            add_log(message, level="error")
            return False, message

        clients = Client.query.filter_by(monitor_type=monitor_type).order_by(Client.name).all()
        if include_synology_section is None:
            include_synology_section = monitor_type == MONITOR_TYPE_VEEAM

        extra_sections: list[tuple[str, str, list[Client]]] = []
        if include_synology_section:
            synology_clients = (
                Client.query.filter_by(monitor_type=MONITOR_TYPE_SYNOLOGY)
                .order_by(Client.name)
                .all()
            )
            if synology_clients:
                extra_sections.append(
                    ("Rapport Synology Hyper Backup", "NAS", synology_clients)
                )

        window_label = format_window_label(config)
        body = build_status_report(
            clients,
            tz,
            window_label,
            report_title,
            extra_sections=extra_sections,
        )
        html_body = build_status_report_html(
            clients,
            tz,
            window_label,
            title=html_title,
            subtitle="Statut des notifications",
            item_label=item_label,
            extra_sections=extra_sections,
        )

        msg = EmailMessage()
        msg["Subject"] = f"{mail_subject_prefix} - {datetime.now(tz=tz).strftime('%d/%m/%Y %H:%M')}"
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
            smtp_authenticate(server, config)
            server.send_message(msg)
            add_log(f"Rapport envoyé à {len(recipients)} destinataire(s).")
            return True, "Rapport envoyé avec succès."
        except Exception as exc:  # noqa: BLE001
            error_detail = format_smtp_auth_error(config, exc)
            message = f"Échec de l'envoi du rapport : {error_detail}"
            add_log(message, level="error")
            return False, message
        finally:
            if server:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
