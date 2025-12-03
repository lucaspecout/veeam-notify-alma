import imaplib
import smtplib
from functools import wraps

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import db
from .email_service import run_email_checks
from .models import Client, EmailConfig, LogEntry, STATUS_CHOICES, User, add_log


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
        subject = request.form.get("expected_subject", "").strip()
        if not name or not subject:
            flash("Merci de renseigner un nom et un objet attendu.", "error")
        else:
            client = Client(name=name, expected_subject=subject, last_status="Non reçu")
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
        client.expected_subject = request.form.get("expected_subject", "").strip()
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
        db.session.commit()
        add_log(f"Configuration e-mail mise à jour par {g.user.username}.")
        flash("Configuration mise à jour.", "success")
        return redirect(url_for("main.settings"))
    return render_template("settings.html", config=config)


@bp.route("/settings/test-imap", methods=["POST"])
@login_required
def test_imap_connection():
    config = EmailConfig.get_singleton()
    if not config.imap_host or not config.imap_username or not config.imap_password:
        flash("Configuration IMAP incomplète.", "error")
        return redirect(url_for("main.settings"))

    mail = None
    try:
        if config.use_ssl:
            mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
        else:
            mail = imaplib.IMAP4(config.imap_host, config.imap_port)
        mail.login(config.imap_username, config.imap_password)
        mail.select("INBOX")
        flash("Test IMAP réussi.", "success")
        add_log(f"Test IMAP réussi par {g.user.username}.")
    except Exception as exc:  # noqa: BLE001
        flash(f"Test IMAP échoué : {exc}", "error")
        add_log(f"Test IMAP échoué : {exc}", level="error")
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
        flash("Configuration SMTP incomplète.", "error")
        return redirect(url_for("main.settings"))

    server = None
    try:
        if config.use_ssl:
            server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10)
        server.login(config.smtp_username, config.smtp_password)
        server.noop()
        flash("Test SMTP réussi.", "success")
        add_log(f"Test SMTP réussi par {g.user.username}.")
    except Exception as exc:  # noqa: BLE001
        flash(f"Test SMTP échoué : {exc}", "error")
        add_log(f"Test SMTP échoué : {exc}", level="error")
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
