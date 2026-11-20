import logging
import os

import click

from flask import Flask, jsonify, request, session
from werkzeug.exceptions import HTTPException

from core.config import Config
from core.middleware.superadmin import setup_superadmin
from core.routes import ALL_BLUEPRINTS
from core.services import fire_scheduler, fire_service, kobo_service

app = Flask(__name__)  # templates/ next to this file
app.config.from_object(Config)
os.makedirs(app.config["EXPORT_TMP_DIR"], exist_ok=True)

for blueprint in ALL_BLUEPRINTS:
    app.register_blueprint(blueprint)


@app.context_processor
def inject_current_role():
    """Role name for showing / hiding navigation links. Display only: access
    is enforced by role_required(), which re-reads the role from the DB."""
    return {"current_role": session.get("role")}
_HTTP_MESSAGES = {
    400: "La requête est invalide. Rechargez la page puis réessayez.",
    401: "Connectez-vous pour continuer.",
    403: "Vous n'avez pas accès à cette page.",
    404: "Cette page n'existe pas ou a été déplacée.",
    405: "Cette action n'est pas possible ici.",
    413: f"Le fichier est trop volumineux ({Config.MAX_CONTENT_LENGTH // (1024 * 1024)} Mo au maximum).",
    429: "Trop de demandes. Patientez un instant puis réessayez.",
}
_UNEXPECTED = "Une erreur inattendue est survenue. Veuillez réessayer."


def _error_response(message: str, status: int):
    if request.path.startswith("/api/"):
        return jsonify({"message": message, "errors": [message]}), status
    return message, status, {"Content-Type": "text/plain; charset=utf-8"}


@app.errorhandler(HTTPException)
def _http_error(exc: HTTPException):
    return _error_response(_HTTP_MESSAGES.get(exc.code or 500, _UNEXPECTED), exc.code or 500)


@app.errorhandler(Exception)
def _unexpected_error(exc: Exception):
    logging.getLogger(__name__).exception("Unhandled error on %s", request.path)
    return _error_response(_UNEXPECTED, 500)

if app.config["SUPERADMIN_BOOTSTRAP"]:
    setup_superadmin(app)


@app.cli.command("fires-refresh")
@click.option("--force", is_flag=True, help="Ignore the refresh interval.")
def fires_refresh(force: bool):
    """Fetch NASA FIRMS hotspots now (for cron when FIRE_SCHEDULER=0)."""
    result = fire_service.refresh(force=force)
    click.echo(result.skipped and f"Skipped: {result.skipped}" or
               f"{result.received} rows, {result.inserted} new hotspots, "
               f"{result.alerts} alerts, {result.emailed} e-mails")


@app.cli.command("geo-import")
@click.argument("kind", type=click.Choice(["region", "commune"]))
@click.argument("path", type=click.Path(exists=True))
@click.option("--name-field", help="Colonne du nom (défaut : ADM1_FR/ADM1_EN ou ADM3_FR/ADM3_EN).")
@click.option("--code-field", help="Colonne du code unique (défaut : ADM1_PCODE / ADM3_PCODE).")
@click.option("--region-field", help="Communes : colonne du code de la région (défaut : ADM1_PCODE).")
@click.option("--district-field", help="Communes : colonne du district (défaut : ADM2_FR/ADM2_EN).")
def geo_import(kind, path, name_field, code_field, region_field, district_field):
    """Import administrative boundaries (régions first, then communes)."""
    from core.services import admin_geo_service
    n = admin_geo_service.import_boundaries(kind, path, name_field, code_field,
                                            region_field, district_field)
    click.echo(f"{n} {kind}(s) importée(s)")


@app.cli.command("kobo-sync")
def kobo_sync():
    """Import the new KoboToolbox submissions of every imported form (cron)."""
    click.echo(f"{kobo_service.sync_all()} form(s) synchronised")


# Open the HTTPS connections to Supabase now, not during the first sign-in.
import threading  # noqa: E402
from core import config as _config  # noqa: E402
threading.Thread(target=_config.warm_up, name="supabase-warm-up", daemon=True).start()

fire_scheduler.start()
kobo_service.start_scheduler()
