import csv
import imaplib
import io
import smtplib
from functools import wraps

from flask import (
    Blueprint,
    current_app,
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
from .email_service import parse_report_recipients, run_email_checks, send_status_report
from .models import Client, EmailConfig, LogEntry, STATUS_CHOICES, STATUS_MISSING, User, add_log
from .scheduler import configure_jobs


bp = Blueprint("main", __name__)


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
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session["user_id"] = user.id
            flash("Connexion réussie.", "success")
            next_page = request.args.get("next") or url_for("main.index")
            add_log(f"Utilisateur {username} connecté.")
            return redirect(next_page)
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
    return render_template("index.html", clients=clients, statuses=STATUS_CHOICES)


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


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    config = EmailConfig.get_singleton()
    if request.method == "POST":
        config.imap_host = request.form.get("imap_host") or None
        config.imap_port = int(request.form.get("imap_port") or 993)
        config.imap_username = request.form.get("imap_username") or None
        config.imap_password = request.form.get("imap_password") or None
        config.smtp_host = request.form.get("smtp_host") or None
        config.smtp_port = int(request.form.get("smtp_port") or 0) or None
        config.smtp_username = request.form.get("smtp_username") or None
        config.smtp_password = request.form.get("smtp_password") or None
        config.use_ssl = request.form.get("use_ssl") == "on"
        raw_recipients = request.form.get("report_recipients", "")
        recipients = parse_report_recipients(raw_recipients)
        config.report_recipients = ", ".join(recipients) if recipients else None
        config.auto_report_enabled = request.form.get("auto_report_enabled") == "on"
        db.session.commit()
        configure_jobs(current_app._get_current_object())
        add_log(f"Configuration e-mail mise à jour par {g.user.username}.")
        flash("Configuration mise à jour.", "success")
        return redirect(url_for("main.settings"))
    return render_template("settings.html", config=config)


@bp.route("/settings/test-imap", methods=["POST"])
@login_required
def test_imap_connection():
    config = EmailConfig.get_singleton()
    if not config.imap_host or not config.imap_username or not config.imap_password:
        message = "Configuration IMAP incomplète."
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": False, "message": message}), 400
        flash(message, "error")
        return redirect(url_for("main.settings"))

    mail = None
    try:
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=10)
        else:
            mail = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=10)
        mail.login(config.imap_username, config.imap_password)
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
    if (
        not config.smtp_host
        or not config.smtp_port
        or not config.smtp_username
        or not config.smtp_password
    ):
        message = "Configuration SMTP incomplète."
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": False, "message": message}), 400
        flash(message, "error")
        return redirect(url_for("main.settings"))

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
        server.noop()
        message = "Test SMTP réussi."
        add_log(f"{message} par {g.user.username}.")
        if request.accept_mimetypes.accept_json:
            return jsonify({"success": True, "message": message})
        flash(message, "success")
    except Exception as exc:  # noqa: BLE001
        message = f"Test SMTP échoué : {exc}"
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
    entries = LogEntry.query.order_by(LogEntry.created_at.desc()).limit(200).all()
    return render_template("logs.html", entries=entries)
