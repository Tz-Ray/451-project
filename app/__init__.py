from flask import Flask
from config import Config
from app.db import init_app as init_db_app


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    init_db_app(app)

    from app.controller.main import main_bp
    from app.controller.parking import parking_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(parking_bp)

    return app
