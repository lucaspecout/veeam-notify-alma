import atexit

from apscheduler.schedulers.background import BackgroundScheduler

from .email_service import run_email_checks, send_status_report

scheduler = BackgroundScheduler(timezone="Europe/Paris")


def configure_jobs(app):
    from .models import EmailConfig

    if not scheduler.running:
        scheduler.start()

    scheduler.add_job(
        lambda: run_email_checks(app),
        "cron",
        hour=9,
        minute=0,
        id="daily-email-check",
        replace_existing=True,
    )

    config = EmailConfig.get_singleton()
    report_job = scheduler.get_job("daily-report-email")
    if config.auto_report_enabled:
        scheduler.add_job(
            lambda: send_status_report(app),
            "cron",
            hour=9,
            minute=30,
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
