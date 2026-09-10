"""应用工厂。"""
import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.path.join(app.instance_path, "ovenline.sqlite"),
        PROBE_GAP_MINUTES=10,  # 相邻测温点间隔超过该分钟数记为探头中断
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    app.json.ensure_ascii = False

    from . import db
    db.init_app(app)
    with app.app_context():
        db.init_db()

    from . import views
    app.register_blueprint(views.bp, url_prefix="/api")

    @app.get("/")
    def index():
        return {
            "service": "粉末喷涂烘炉固化排产 API",
            "api_prefix": "/api",
            "docs": "见 README.md",
        }

    return app
