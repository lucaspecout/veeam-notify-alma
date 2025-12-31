import atexit

from apscheduler.schedulers.background import BackgroundScheduler

from .email_service import run_email_checks, send_status_report

scheduler = BackgroundScheduler(timezone="Europe/Paris")


def _sanitize_hour(value: int | None, default: int) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(23, hour))


def _sanitize_minute(value: int | None, default: int) -> int:
    try:
        minute = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(59, minute))


def configure_jobs(app):
    from .models import EmailConfig

    if not scheduler.running:
        scheduler.start()

    config = EmailConfig.get_singleton()
    check_hour = _sanitize_hour(config.check_schedule_hour, 9)
    check_minute = _sanitize_minute(config.check_schedule_minute, 0)
    scheduler.add_job(
        lambda: run_email_checks(app),
        "cron",
        hour=check_hour,
        minute=check_minute,
        id="daily-email-check",
        replace_existing=True,
    )

    report_hour = _sanitize_hour(config.report_schedule_hour, 9)
    report_minute = _sanitize_minute(config.report_schedule_minute, 30)
    report_job = scheduler.get_job("daily-report-email")
    if config.auto_report_enabled:
        scheduler.add_job(
            lambda: send_status_report(app),
            "cron",
            hour=report_hour,
            minute=report_minute,
            id="daily-report-email",
            replace_existing=True,
        )
    elif report_job:
        scheduler.remove_job(report_job.id)


def init_scheduler(app):
    if scheduler.running:
        configure_jobs(app)
        return

    configure_jobs(app)
    atexit.register(lambda: scheduler.shutdown(wait=False))
