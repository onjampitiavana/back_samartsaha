"""Flask blueprints, one per functional area, registered by app.py."""

from core.routes.admin_bp import admin_bp
from core.routes.api_bp import api_bp
from core.routes import audit_bio_api, avenant_api, dashboard_api, fire_api  # noqa: F401  (add /api routes)
from core.routes.auth_bp import auth_bp
from core.routes.data_bp import data_bp
from core.routes.formulaire_bp import formulaire_bp
from core.routes.main_bp import main_bp
from core.routes.manager_bp import manager_bp
from core.routes.superadmin_bp import superadmin_bp

ALL_BLUEPRINTS = (auth_bp, main_bp, data_bp, superadmin_bp, admin_bp, manager_bp,
                  formulaire_bp, api_bp)

__all__ = ["ALL_BLUEPRINTS", "admin_bp", "api_bp", "auth_bp", "data_bp", "formulaire_bp",
           "main_bp", "manager_bp", "superadmin_bp"]
