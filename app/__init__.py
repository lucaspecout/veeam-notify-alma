import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


def create_app():
    app = Flask(
        __name__, template_folder="../templates", static_folder="../static"
    )
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")

    db.init_app(app)

    with app.app_context():
        from .routes import bp
        from .scheduler import init_scheduler
        from .models import User
        from .db_migrations import run_migrations

        db.create_all()
        run_migrations(db.engine)
        User.ensure_default_admin()
        app.register_blueprint(bp)
        init_scheduler(app)

    return app
