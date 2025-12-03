import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from .scheduler import init_scheduler


db = SQLAlchemy()


def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")

    db.init_app(app)

    with app.app_context():
        from .routes import bp

        db.create_all()
        app.register_blueprint(bp)
        init_scheduler(app)

    return app
