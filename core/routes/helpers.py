
import io
from typing import Iterable

from flask import flash, send_file
from flask_wtf import FlaskForm

from core.services.data_service import ExportFile


def flash_errors(messages: Iterable[str]) -> None:
    """Flash each message as an error."""
    for message in messages:
        flash(message, "error")


def flash_form_errors(form: FlaskForm) -> None:
    """Flash every validation error of a Flask-WTF form."""
    for errors in form.errors.values():
        flash_errors(errors)


def send_export(export: ExportFile):
    """Serve an in-memory export as a download."""
    return send_file(io.BytesIO(export.content), as_attachment=True,
                     download_name=export.filename, mimetype=export.mimetype)
