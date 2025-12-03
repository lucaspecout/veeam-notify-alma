from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import db
from .email_service import run_email_checks
from .models import Client, EmailConfig, STATUS_CHOICES


bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    clients = Client.query.order_by(Client.name).all()
    return render_template("index.html", clients=clients, statuses=STATUS_CHOICES)


@bp.route("/clients/new", methods=["GET", "POST"])
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
            flash("Client créé avec succès.", "success")
            return redirect(url_for("main.index"))
    return render_template("client_form.html", client=None)


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def edit_client(client_id: int):
    client = Client.query.get_or_404(client_id)
    if request.method == "POST":
        client.name = request.form.get("name", "").strip()
        client.expected_subject = request.form.get("expected_subject", "").strip()
        db.session.commit()
        flash("Client mis à jour.", "success")
        return redirect(url_for("main.index"))
    return render_template("client_form.html", client=client)


@bp.route("/clients/<int:client_id>/delete", methods=["POST"])
def delete_client(client_id: int):
    client = Client.query.get_or_404(client_id)
    db.session.delete(client)
    db.session.commit()
    flash("Client supprimé.", "success")
    return redirect(url_for("main.index"))


@bp.route("/settings", methods=["GET", "POST"])
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
        flash("Configuration mise à jour.", "success")
        return redirect(url_for("main.settings"))
    return render_template("settings.html", config=config)


@bp.route("/run-check", methods=["POST"])
def run_check():
    run_email_checks()
    flash("Vérification lancée.", "success")
    return redirect(url_for("main.index"))
