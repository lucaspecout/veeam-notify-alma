import atexit

from apscheduler.schedulers.background import BackgroundScheduler

from .email_service import run_email_checks

scheduler = BackgroundScheduler(timezone="Europe/Paris")


def init_scheduler(app):
    if scheduler.running:
        return

    scheduler.add_job(lambda: run_email_checks(app), "cron", hour=9, minute=0, id="daily-email-check", replace_existing=True)
    scheduler.start()

    atexit.register(lambda: scheduler.shutdown(wait=False))
