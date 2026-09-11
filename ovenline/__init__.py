"""应用工厂。"""
import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.path.join(app.instance_path, "ovenline.sqlite"),
        PROBE_GAP_MINUTES=10,          # 启用探头缺报超过该分钟数记为探头中断，
                                       # 且超过阈值的探头不再计为有效探头
        PROBE_DIVERGENCE_C=5.0,        # 同一时刻有效探头校正值极差超过该温度记为温差异常
        STUCK_PROBE_MIN_CONSECUTIVE=5,  # 同一探头连续相同读数达到该点数记为卡值
        MIN_VALID_PROBES=1,            # 判定合格所需的最少有效探头数
        COOLING_GAP_MINUTES=10,        # 冷却测温采样间隔超过该分钟数即截断低温区间，
                                       # 最新读数距基准时刻超过该值视为陈旧不得放行
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
