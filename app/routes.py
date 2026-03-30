import os
import csv
import email
import imaplib
import io
import smtplib
import urllib.parse
from functools import wraps

from flask import (
    Blueprint,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import db
from .email_service import (
    decode_subject,
    format_smtp_auth_error,
    format_window_label,
    get_microsoft_access_token,
    get_microsoft_token_diagnostics,
    imap_authenticate,
    MICROSOFT_USER_SCOPE,
    is_microsoft_oauth_enabled,
    run_email_checks,
    send_status_report,
    smtp_authenticate,
    _store_oauth_token,
    _request_microsoft_token,
)
from .models import Client, EmailConfig, LogEntry, STATUS_CHOICES, STATUS_MISSING, User, add_log


bp = Blueprint("main", __name__)


def _request_debug_context() -> str:
    remote_addr = request.headers.get("X-Forwarded-For", request.remote_addr or "inconnue")
    user_agent = request.user_agent.string or "inconnu"
    return f"ip={remote_addr} ua='{user_agent[:120]}'"


def _safe_identity(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "<vide>"
    if len(cleaned) <= 3:
        return "***"
    return f"{cleaned[:2]}***{cleaned[-1]}"


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if not g.user:
            return redirect(url_for("main.login", next=request.path))
        return view(**kwargs)

    return wrapped_view


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = User.query.get(user_id) if user_id else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        add_log(
            "Tentative de connexion application "
            f"username={_safe_identity(username)} {_request_debug_context()}",
            level="warning",
        )
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session["user_id"] = user.id
            flash("Connexion réussie.", "success")
            next_page = request.args.get("next") or url_for("main.index")
            add_log(
                f"Utilisateur {username} connecté. Redirection vers '{next_page}'. "
                f"{_request_debug_context()}"
            )
            return redirect(next_page)
        add_log(
            "Échec de connexion application "
            f"username={_safe_identity(username)} raison='identifiants invalides' "
            f"{_request_debug_context()}",
            level="error",
        )
        flash("Identifiants invalides.", "error")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    if g.user:
        username = g.user.username
        add_log(f"Utilisateur {username} déconnecté.")
    session.clear()
    return redirect(url_for("main.login"))


@bp.route("/")
@login_required
def index():
    clients = Client.query.order_by(Client.name).all()
    config = EmailConfig.get_singleton()
    window_label = format_window_label(config)
    return render_template(
        "index.html", clients=clients, statuses=STATUS_CHOICES, window_label=window_label
    )


@bp.route("/clients/new", methods=["GET", "POST"])
@login_required
def new_client():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        subject_ok = request.form.get("expected_subject_ok", "").strip()
        subject_warning = request.form.get("expected_subject_warning", "").strip()
        subject_failed = request.form.get("expected_subject_failed", "").strip()

        if not name or not subject_ok or not subject_warning or not subject_failed:
            flash("Merci de renseigner un nom et les trois objets attendus.", "error")
        else:
            client = Client(
                name=name,
                expected_subject=subject_ok,
                expected_subject_ok=subject_ok,
                expected_subject_warning=subject_warning,
                expected_subject_failed=subject_failed,
                last_status=STATUS_MISSING,
            )
            db.session.add(client)
            db.session.commit()
            add_log(f"Client '{name}' créé par {g.user.username}.")
            flash("Client créé avec succès.", "success")
            return redirect(url_for("main.index"))
    return render_template("client_form.html", client=None)


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def edit_client(client_id: int):
    client = Client.query.get_or_404(client_id)
    if request.method == "POST":
        client.name = request.form.get("name", "").strip()
        subject_ok = request.form.get("expected_subject_ok", "").strip()
        subject_warning = request.form.get("expected_subject_warning", "").strip()
        subject_failed = request.form.get("expected_subject_failed", "").strip()

        client.expected_subject = subject_ok
        client.expected_subject_ok = subject_ok
        client.expected_subject_warning = subject_warning
        client.expected_subject_failed = subject_failed
        db.session.commit()
        add_log(f"Client '{client.name}' mis à jour par {g.user.username}.")
        flash("Client mis à jour.", "success")
        return redirect(url_for("main.index"))
    return render_template("client_form.html", client=client)


@bp.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def delete_client(client_id: int):
    client = Client.query.get_or_404(client_id)
    db.session.delete(client)
    db.session.commit()
    add_log(f"Client '{client.name}' supprimé par {g.user.username}.")
    flash("Client supprimé.", "success")
    return redirect(url_for("main.index"))


@bp.route("/clients/export", methods=["GET"])
@login_required
def export_clients():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "name",
        "expected_subject_ok",
        "expected_subject_warning",
        "expected_subject_failed",
    ])
    for client in Client.query.order_by(Client.name).all():
        writer.writerow([
            client.name,
            client.expected_subject_ok or "",
            client.expected_subject_warning or "",
            client.expected_subject_failed or "",
        ])

    response = current_app.response_class(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=clients.csv"},
    )
    add_log(f"Export des clients effectué par {g.user.username}.")
    return response


@bp.route("/clients/import", methods=["POST"])
@login_required
def import_clients():
    uploaded = request.files.get("file")
    if not uploaded or uploaded.filename == "":
        flash("Merci de sélectionner un fichier CSV.", "error")
        return redirect(url_for("main.index"))

    try:
        stream = io.StringIO(uploaded.stream.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        flash("Impossible de lire le fichier fourni.", "error")
        return redirect(url_for("main.index"))

    reader = csv.DictReader(stream)
    existing_names = {client.name.lower() for client in Client.query.all()}
    created = 0
    skipped = 0

    for row in reader:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        if name.lower() in existing_names:
            skipped += 1
            continue

        client = Client(
            name=name,
            expected_subject_ok=(row.get("expected_subject_ok") or "").strip(),
            expected_subject_warning=(row.get("expected_subject_warning") or "").strip(),
            expected_subject_failed=(row.get("expected_subject_failed") or "").strip(),
            expected_subject=(row.get("expected_subject_ok") or "").strip(),
            last_status=STATUS_MISSING,
        )
        db.session.add(client)
        existing_names.add(name.lower())
        created += 1

    db.session.commit()
    add_log(
        f"Import de clients réalisé par {g.user.username}: {created} ajoutés, {skipped} ignorés."
    )
    flash(f"Import terminé : {created} ajouté(s), {skipped} ignoré(s).", "success")
    return redirect(url_for("main.index"))


@bp.route("/settings/microsoft/connect", methods=["POST"])
@login_required
def microsoft_connect():
    config = EmailConfig.get_singleton()
    add_log(
        "Démarrage connexion Microsoft 365 "
        f"tenant={_safe_identity(config.ms_tenant_id)} "
        f"client_id={_safe_identity(config.ms_client_id)} "
        f"user={g.user.username if g.user else 'inconnu'}"
    )
    if not config.ms_tenant_id or not config.ms_client_id:
        flash("Renseignez Tenant ID et Client ID avant de vous connecter à Microsoft 365.", "error")
        add_log(
            "Connexion Microsoft refusée: configuration manquante "
            f"tenant={bool(config.ms_tenant_id)} client_id={bool(config.ms_client_id)}",
            level="error",
        )
        return redirect(url_for("main.settings"))

    redirect_uri = url_for("main.microsoft_callback", _external=True, _scheme="https")
    state = session.get("ms_oauth_state") or os.urandom(24).hex()
    session["ms_oauth_state"] = state
    auth_params = {
        "client_id": config.ms_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": MICROSOFT_USER_SCOPE,
        "state": state,
        "prompt": "select_account",
    }
    authorize_url = (
        f"https://login.microsoftonline.com/{config.ms_tenant_id}/oauth2/v2.0/authorize?"
        f"{urllib.parse.urlencode(auth_params)}"
    )
    return redirect(authorize_url)


@bp.route("/settings/microsoft/callback", methods=["GET"])
@login_required
def microsoft_callback():
    config = EmailConfig.get_singleton()
    expected_state = session.pop("ms_oauth_state", None)
    received_state = request.args.get("state")
    add_log(
        "Callback Microsoft reçu "
        f"user={g.user.username if g.user else 'inconnu'} "
        f"state_present={bool(received_state)} code_present={bool(request.args.get('code'))} "
        f"error_present={bool(request.args.get('error'))}"
    )
    if not expected_state or expected_state != received_state:
        add_log(
            "Connexion Microsoft refusée: état OAuth invalide "
            f"expected_state_present={bool(expected_state)} received_state_present={bool(received_state)}",
            level="error",
        )
        flash("Connexion Microsoft refusée: état OAuth invalide.", "error")
        return redirect(url_for("main.settings"))

    error = request.args.get("error")
    if error:
        description = request.args.get("error_description", error)
        add_log(f"Connexion Microsoft annulée: {description}", level="error")
        flash(f"Connexion Microsoft annulée: {description}", "error")
        return redirect(url_for("main.settings"))

    code = request.args.get("code")
    if not code:
        add_log("Connexion Microsoft échouée: code d'autorisation manquant.", level="error")
        flash("Connexion Microsoft échouée: code d'autorisation manquant.", "error")
        return redirect(url_for("main.settings"))

    try:
        add_log("Échange du code OAuth Microsoft contre un token en cours.")
        token_data = _request_microsoft_token(
            config,
            {
                "grant_type": "authorization_code",
                "code": code,
                "scope": MICROSOFT_USER_SCOPE,
                "redirect_uri": url_for("main.microsoft_callback", _external=True, _scheme="https"),
            },
        )
        _store_oauth_token(config, token_data)
        add_log(f"Connexion Microsoft 365 validée par {g.user.username}.")
        flash("Compte Microsoft 365 connecté. Les autorisations OAuth sont validées.", "success")
    except Exception as exc:  # noqa: BLE001
        add_log(f"Connexion Microsoft 365 échouée: {exc}", level="error")
        flash(f"Connexion Microsoft échouée: {exc}", "error")

    return redirect(url_for("main.settings"))


@bp.route("/settings/microsoft/check", methods=["POST"])
@login_required
def microsoft_check_connection():
    config = EmailConfig.get_singleton()
    try:
        get_microsoft_access_token(config)
        message = "Connexion Microsoft 365 valide."
        add_log(f"{message} Vérifiée par {g.user.username}.")
        flash(message, "success")
    except Exception as exc:  # noqa: BLE001
        message = f"Connexion Microsoft 365 invalide : {exc}"
        add_log(message, level="error")
        flash(message, "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/microsoft/test-read-latest", methods=["POST"])
@login_required
def microsoft_test_read_latest_email():
    config = EmailConfig.get_singleton()
    missing_imap_password = (not is_microsoft_oauth_enabled(config)) and (not config.imap_password)
    if not config.imap_host or not config.imap_username or missing_imap_password:
        flash("Configuration IMAP incomplète.", "error")
        return redirect(url_for("main.settings"))

    mail = None
    try:
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=10)
        else:
            mail = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=10)

        imap_authenticate(mail, config)
        status, _ = mail.select("INBOX")
        if status != "OK":
            raise RuntimeError("Impossible d'ouvrir la boîte INBOX.")

        status, data = mail.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("Impossible de lister les e-mails de la boîte.")

        message_ids = data[0].split() if data and data[0] else []
        if not message_ids:
            message = "Connexion IMAP réussie, mais aucun e-mail trouvé dans la boîte."
            add_log(f"{message} Vérifiée par {g.user.username}.")
            flash(message, "success")
            return redirect(url_for("main.settings"))

        latest_id = message_ids[-1]
        status, msg_data = mail.fetch(latest_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError("Connexion IMAP réussie, mais impossible de lire le dernier e-mail.")

        raw_email = msg_data[0][1]
        parsed = email.message_from_bytes(raw_email)
        subject = decode_subject(parsed.get("Subject", "(sans objet)"))
        sender = parsed.get("From", "inconnu")
        received_at = parsed.get("Date", "date inconnue")
        message = (
            "Connexion IMAP valide. Dernier e-mail lu avec succès : "
            f"Objet '{subject}', De '{sender}', Date '{received_at}'."
        )
        add_log(f"Test lecture dernier e-mail réussi par {g.user.username}: {subject}")
        flash(message, "success")
    except Exception as exc:  # noqa: BLE001
        message = f"Échec lecture du dernier e-mail : {exc}"
        add_log(message, level="error")
        flash(message, "error")
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:  # noqa: BLE001
                pass

    return redirect(url_for("main.settings"))


@bp.route("/settings/microsoft/disconnect", methods=["POST"])
@login_required
def microsoft_disconnect():
    config = EmailConfig.get_singleton()
    config.ms_access_token = None
    config.ms_refresh_token = None
    config.ms_token_expires_at = None
    db.session.commit()
    add_log(f"Compte Microsoft 365 déconnecté par {g.user.username}.")
    flash("Compte Microsoft 365 déconnecté.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    config = EmailConfig.get_singleton()
    if request.method == "POST":
        def _parse_port(value: str | None, default: int) -> int:
            try:
                return int((value or "").strip() or default)
            except ValueError:
                return default

        def _parse_bounded_int(
            value: str | None, default: int, minimum: int, maximum: int
        ) -> int:
            try:
                parsed = int((value or "").strip() or default)
            except ValueError:
                return default
            return min(max(parsed, minimum), maximum)

        config.auth_mode = "microsoft_oauth2"
        config.ms_tenant_id = request.form.get("ms_tenant_id") or None
        config.ms_client_id = request.form.get("ms_client_id") or None
        config.ms_client_secret = request.form.get("ms_client_secret") or None
        config.imap_host = (request.form.get("imap_host") or "").strip() or None
        config.imap_port = _parse_port(request.form.get("imap_port"), 993)
        config.imap_username = (request.form.get("imap_username") or "").strip() or None
        config.smtp_host = (request.form.get("smtp_host") or "").strip() or None
        config.smtp_port = _parse_port(request.form.get("smtp_port"), 587)
        config.smtp_username = (request.form.get("smtp_username") or "").strip() or None
        config.use_ssl = request.form.get("use_ssl") == "on"
        config.report_recipients = (request.form.get("report_recipients") or "").strip() or None
        config.check_schedule_hour = _parse_bounded_int(
            request.form.get("check_schedule_hour"), 9, 0, 23
        )
        config.check_schedule_minute = _parse_bounded_int(
            request.form.get("check_schedule_minute"), 0, 0, 59
        )
        config.report_schedule_hour = _parse_bounded_int(
            request.form.get("report_schedule_hour"), 9, 0, 23
        )
        config.report_schedule_minute = _parse_bounded_int(
            request.form.get("report_schedule_minute"), 30, 0, 59
        )
        db.session.commit()
        add_log(f"Configuration Microsoft OAuth + boîtes IMAP/SMTP mise à jour par {g.user.username}.")
        flash("Configuration Microsoft 365 et boîtes IMAP/SMTP mise à jour.", "success")
        return redirect(url_for("main.settings"))
    return render_template(
        "settings.html",
        config=config,
        microsoft_connected=bool(config.ms_refresh_token or config.ms_access_token),
        imap_connected_mail=(config.imap_username or "").strip() or None,
    )


@bp.route("/settings/test-imap", methods=["POST"])
@login_required
def test_imap_connection():
    config = EmailConfig.get_singleton()
    missing_imap_password = (not is_microsoft_oauth_enabled(config)) and (not config.imap_password)
    if not config.imap_host or not config.imap_username or missing_imap_password:
        message = "Configuration IMAP incomplète."
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": False, "message": message}), 400
        flash(message, "error")
        return redirect(url_for("main.settings"))

    mail = None
    try:
        add_log(
            "Test IMAP démarré "
            f"host={config.imap_host or '<vide>'} port={config.imap_port} "
            f"use_ssl={config.use_ssl} auth_mode={config.auth_mode or 'password'} "
            f"user={g.user.username if g.user else 'inconnu'}"
        )
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=10)
        else:
            mail = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=10)
        add_log("Connexion socket IMAP établie, tentative d'authentification en cours.")
        imap_authenticate(mail, config)
        add_log("Authentification IMAP réussie, ouverture INBOX.")
        mail.select("INBOX")
        message = "Test IMAP réussi."
        add_log(f"{message} par {g.user.username}.")
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": True, "message": message})
        flash(message, "success")
    except Exception as exc:  # noqa: BLE001
        message = f"Test IMAP échoué : {exc}"
        add_log(message, level="error")
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": False, "message": message}), 500
        flash(message, "error")
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:  # noqa: BLE001
                pass

    return redirect(url_for("main.settings"))


@bp.route("/settings/test-smtp", methods=["POST"])
@login_required
def test_smtp_connection():
    config = EmailConfig.get_singleton()
    missing_smtp_password = (not is_microsoft_oauth_enabled(config)) and (not config.smtp_password)
    if (
        not config.smtp_host
        or not config.smtp_port
        or not config.smtp_username
        or missing_smtp_password
    ):
        message = "Configuration SMTP incomplète."
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": False, "message": message}), 400
        flash(message, "error")
        return redirect(url_for("main.settings"))

    server = None
    try:
        add_log(
            "Test SMTP démarré "
            f"host={config.smtp_host or '<vide>'} port={config.smtp_port} "
            f"use_ssl={config.use_ssl} auth_mode={config.auth_mode or 'password'} "
            f"user={g.user.username if g.user else 'inconnu'}"
        )
        use_ssl_direct = config.use_ssl and config.smtp_port == 465
        if use_ssl_direct:
            server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10)
            if config.use_ssl:
                server.ehlo()
                server.starttls()
                server.ehlo()
        add_log("Connexion SMTP établie, tentative d'authentification en cours.")
        smtp_authenticate(server, config)
        add_log("Authentification SMTP réussie, envoi commande NOOP.")
        server.noop()
        message = "Test SMTP réussi."
        add_log(f"{message} par {g.user.username}.")
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": True, "message": message})
        flash(message, "success")
    except Exception as exc:  # noqa: BLE001
        error_detail = format_smtp_auth_error(config, exc)
        message = f"Test SMTP échoué : {error_detail}"
        add_log(message, level="error")
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": False, "message": message}), 500
        flash(message, "error")
    finally:
        if server:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass

    return redirect(url_for("main.settings"))


@bp.route("/run-check", methods=["POST"])
@login_required
def run_check():
    run_email_checks()
    flash("Vérification lancée.", "success")
    return redirect(url_for("main.index"))


@bp.route("/send-report", methods=["POST"])
@login_required
def send_report():
    success, message = send_status_report()
    flash(message, "success" if success else "error")
    return redirect(url_for("main.index"))


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not g.user.check_password(current_password):
            flash("Mot de passe actuel incorrect.", "error")
        elif not new_password:
            flash("Le nouveau mot de passe ne peut pas être vide.", "error")
        elif new_password != confirm_password:
            flash("La confirmation ne correspond pas.", "error")
        else:
            g.user.set_password(new_password)
            db.session.commit()
            add_log(f"Mot de passe mis à jour pour l'utilisateur {g.user.username}.")
            flash("Mot de passe mis à jour.", "success")
            return redirect(url_for("main.index"))
    return render_template("change_password.html")


@bp.route("/logs")
@login_required
def logs():
    entries = LogEntry.query.order_by(LogEntry.created_at.desc()).limit(1000).all()
    level_counts = {"INFO": 0, "WARNING": 0, "ERROR": 0}
    for entry in entries:
        level = (entry.level or "INFO").upper()
        level_counts[level] = level_counts.get(level, 0) + 1
    return render_template("logs.html", entries=entries, level_counts=level_counts)
