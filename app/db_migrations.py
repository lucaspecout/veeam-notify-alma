from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def run_migrations(engine: Engine) -> None:
    ensure_client_subject_columns(engine)
    ensure_email_config_report_columns(engine)


def ensure_client_subject_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if "client" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("client")}
    columns_added: list[str] = []

    with engine.begin() as connection:
        if "expected_subject_ok" not in columns:
            connection.execute(
                text("ALTER TABLE client ADD COLUMN expected_subject_ok VARCHAR(512)")
            )
            columns_added.append("expected_subject_ok")

        if "expected_subject_warning" not in columns:
            connection.execute(
                text("ALTER TABLE client ADD COLUMN expected_subject_warning VARCHAR(512)")
            )
            columns_added.append("expected_subject_warning")

        if "expected_subject_failed" not in columns:
            connection.execute(
                text("ALTER TABLE client ADD COLUMN expected_subject_failed VARCHAR(512)")
            )
            columns_added.append("expected_subject_failed")

        if "last_statuses" not in columns:
            connection.execute(text("ALTER TABLE client ADD COLUMN last_statuses TEXT"))

        if "last_email_count" not in columns:
            connection.execute(
                text("ALTER TABLE client ADD COLUMN last_email_count INTEGER DEFAULT 0 NOT NULL")
            )

        if columns_added:
            connection.execute(
                text(
                    """
                    UPDATE client
                    SET
                        expected_subject_ok = COALESCE(expected_subject_ok, expected_subject),
                        expected_subject_warning = COALESCE(expected_subject_warning, ''),
                        expected_subject_failed = COALESCE(expected_subject_failed, '')
                    """
                )
            )


def ensure_email_config_report_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if "email_config" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("email_config")}

    with engine.begin() as connection:
        if "report_recipients" not in columns:
            connection.execute(
                text("ALTER TABLE email_config ADD COLUMN report_recipients TEXT")
            )

        if "auto_report_enabled" not in columns:
            connection.execute(
                text("ALTER TABLE email_config ADD COLUMN auto_report_enabled BOOLEAN DEFAULT 0 NOT NULL")
            )
