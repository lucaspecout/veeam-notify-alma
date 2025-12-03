from datetime import datetime

from . import db


STATUS_OK = "OK"
STATUS_MISSING = "Non reçu"
STATUS_FAILED = "Failed"
STATUS_WARNING = "Warning"
STATUS_CHOICES = [STATUS_OK, STATUS_MISSING, STATUS_FAILED, STATUS_WARNING]


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    expected_subject = db.Column(db.String(512), nullable=False)
    last_status = db.Column(db.String(32), default=STATUS_MISSING, nullable=False)
    last_checked_at = db.Column(db.DateTime)
    last_note = db.Column(db.Text)
    last_subject = db.Column(db.String(512))

    def status_label(self) -> str:
        return self.last_status or STATUS_MISSING


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
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get_singleton(cls):
        instance = cls.query.first()
        if not instance:
            instance = cls(imap_port=993, use_ssl=True)
            db.session.add(instance)
            db.session.commit()
        return instance
