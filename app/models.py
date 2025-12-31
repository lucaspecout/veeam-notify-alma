import os
from datetime import datetime
from zoneinfo import ZoneInfo

from werkzeug.security import check_password_hash, generate_password_hash

from . import db


STATUS_OK = "OK"
STATUS_MISSING = "Non reçu"
STATUS_FAILED = "Failed"
STATUS_WARNING = "Warning"
STATUS_CHOICES = [STATUS_OK, STATUS_MISSING, STATUS_FAILED, STATUS_WARNING]


def current_time() -> datetime:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
    return datetime.now(tz=tz)


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    expected_subject = db.Column(db.String(512), nullable=False)
    expected_subject_ok = db.Column(db.String(512))
    expected_subject_warning = db.Column(db.String(512))
    expected_subject_failed = db.Column(db.String(512))
    last_status = db.Column(db.String(32), default=STATUS_MISSING, nullable=False)
    last_checked_at = db.Column(db.DateTime)
    last_note = db.Column(db.Text)
    last_subject = db.Column(db.String(512))
    last_statuses = db.Column(db.Text)
    last_email_count = db.Column(db.Integer, default=0, nullable=False)

    def status_label(self) -> str:
        return self.last_status or STATUS_MISSING

    @property
    def subject_ok(self) -> str:
        return (self.expected_subject_ok or self.expected_subject or "").strip()

    @property
    def subject_warning(self) -> str:
        return (self.expected_subject_warning or "").strip()

    @property
    def subject_failed(self) -> str:
        return (self.expected_subject_failed or "").strip()


class EmailConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    imap_host = db.Column(db.String(256))
    imap_port = db.Column(db.Integer, default=993)
    imap_username = db.Column(db.String(256))
    imap_password = db.Column(db.String(256))
    smtp_host = db.Column(db.String(256))
    smtp_port = db.Column(db.Integer)
    smtp_username = db.Column(db.String(256))
    smtp_password = db.Column(db.String(256))
    use_ssl = db.Column(db.Boolean, default=True)
    report_recipients = db.Column(db.Text)
    auto_report_enabled = db.Column(db.Boolean, default=False, nullable=False)
    check_schedule_hour = db.Column(db.Integer, default=9, nullable=False)
    check_schedule_minute = db.Column(db.Integer, default=0, nullable=False)
    report_schedule_hour = db.Column(db.Integer, default=9, nullable=False)
    report_schedule_minute = db.Column(db.Integer, default=30, nullable=False)
    check_window_start_hour = db.Column(db.Integer, default=16, nullable=False)
    check_window_end_hour = db.Column(db.Integer, default=9, nullable=False)
    updated_at = db.Column(db.DateTime, default=current_time, onupdate=current_time)

    @classmethod
    def get_singleton(cls):
        instance = cls.query.first()
        if not instance:
            instance = cls(
                imap_port=993,
                use_ssl=True,
                check_schedule_hour=9,
                check_schedule_minute=0,
                report_schedule_hour=9,
                report_schedule_minute=30,
                check_window_start_hour=16,
                check_window_end_hour=9,
            )
            db.session.add(instance)
            db.session.commit()
        return instance


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=current_time, nullable=False)

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    @classmethod
    def ensure_default_admin(cls):
        if not cls.query.filter_by(username="admin").first():
            admin = cls(username="admin")
            admin.set_password("admin")
            db.session.add(admin)
            db.session.commit()


class LogEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=current_time, nullable=False)
    level = db.Column(db.String(16), default="INFO", nullable=False)
    message = db.Column(db.Text, nullable=False)


def add_log(message: str, level: str = "INFO") -> None:
    entry = LogEntry(message=message, level=level.upper())
    db.session.add(entry)
    db.session.commit()
