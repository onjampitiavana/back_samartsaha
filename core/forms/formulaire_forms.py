import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional

import shapely
from werkzeug.datastructures import FileStorage, MultiDict

from core.forms.record_forms import Parsed, drawn_ring
from core.models.choices import CHOICE_FIELD_TYPES, FORM_FIELD_TYPES

FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
RESERVED_KEYS = frozenset({"id", "enqueteur_id", "farmer_id", "parcel_id",
                           "submitted_at"})
MAX_FIELDS = 150
MAX_OPTIONS = 50
MAX_SCALE_SPAN = 100
MAX_SHORT_TEXT = 500
MAX_LONG_TEXT = 10_000
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_SIGNATURE_BYTES = 1024 * 1024
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
SIGNATURE_PREFIX = "data:image/png;base64,"


def slugify_key(label: str) -> str:
    """A column-safe key from a field label ('Âge du chef' -> 'age_du_chef')."""
    ascii_label = (unicodedata.normalize("NFKD", label)
                   .encode("ascii", "ignore").decode())
    key = re.sub(r"[^a-z0-9]+", "_", ascii_label.lower()).strip("_")[:40]
    if not key or not key[0].isalpha():
        key = f"q_{key}".rstrip("_")
    return key

def _clean_field(raw: Any, position: int, errors: List[str]) -> Optional[Dict[str, Any]]:
    """One builder field, normalised, or None after appending its errors."""
    where = f"Question {position}"
    if not isinstance(raw, dict):
        errors.append(f"{where} : la question est mal formée.")
        return None

    label = str(raw.get("label") or "").strip()[:200]
    ftype = raw.get("type")
    if not label:
        errors.append(f"{where} : saisissez l'intitulé de la question.")
    if ftype not in FORM_FIELD_TYPES:
        errors.append(f"{where} : type de question inconnu.")
        return None

    key = str(raw.get("key") or "").strip() or slugify_key(label)
    if not FIELD_KEY_RE.match(key):
        errors.append(f"{where} : le nom de colonne « {key} » doit commencer par une lettre "
                      "et ne contenir que a-z, 0-9 et « _ » (48 caractères au maximum).")
    elif key in RESERVED_KEYS:
        errors.append(f"{where} : « {key} » est un nom de colonne réservé.")

    cleaned: Dict[str, Any] = {
        "key": key,
        "label": label,
        "type": ftype,
        "required": bool(raw.get("required")),
        "help": str(raw.get("help") or "").strip()[:300],
    }

    if ftype in CHOICE_FIELD_TYPES:
        options = raw.get("options") or []
        if not isinstance(options, list):
            options = []
        seen, unique = set(), []
        for o in options:
            o = str(o).strip()[:120]
            if o and o not in seen:
                seen.add(o)
                unique.append(o)
        if len(unique) < 2:
            errors.append(f"{where} : ajoutez au moins deux options différentes.")
        elif len(unique) > MAX_OPTIONS:
            errors.append(f"{where} : {MAX_OPTIONS} options au maximum.")
        cleaned["options"] = unique

    if ftype == "scale":
        try:
            low, high = int(raw.get("min", 1)), int(raw.get("max", 5))
        except (TypeError, ValueError):
            low, high = 0, 0
        if low >= high or high - low > MAX_SCALE_SPAN:
            errors.append(f"{where} : la valeur minimale de l'échelle doit être inférieure à "
                          f"la maximale ({MAX_SCALE_SPAN} niveaux au maximum).")
        cleaned.update(min=low, max=high,
                       min_label=str(raw.get("min_label") or "").strip()[:60],
                       max_label=str(raw.get("max_label") or "").strip()[:60])
    return cleaned


@dataclass
class FormulaireInput:
    """A validated form definition from the builder."""
    title: str
    description: Optional[str]
    cooperative_id: str
    fields: List[Dict[str, Any]]

    @classmethod
    def parse(cls, form: Mapping[str, str]) -> Parsed["FormulaireInput"]:
        """Validate the posted builder; errors come back in display order."""
        title = form.get("title", "").strip()
        description = form.get("description", "").strip() or None
        cooperative_id = form.get("cooperative_id", "").strip()

        errors: List[str] = []
        if len(title) < 2:
            errors.append("Donnez un titre au formulaire.")
        elif len(title) > 160:
            errors.append("Le titre ne doit pas dépasser 160 caractères.")
        if description and len(description) > 2000:
            errors.append("La description ne doit pas dépasser 2000 caractères.")
        if not cooperative_id:
            errors.append("Choisissez la coopérative.")

        try:
            raw_fields = json.loads(form.get("fields_json") or "[]")
        except ValueError:
            raw_fields = None
        if not isinstance(raw_fields, list):
            errors.append("Les questions n'ont pas pu être lues. Veuillez réessayer.")
            raw_fields = []
        if len(raw_fields) > MAX_FIELDS:
            errors.append(f"Un formulaire peut contenir {MAX_FIELDS} questions au maximum.")
            raw_fields = raw_fields[:MAX_FIELDS]

        fields = []
        for i, raw in enumerate(raw_fields, start=1):
            cleaned = _clean_field(raw, i, errors)
            if cleaned:
                fields.append(cleaned)

        keys = [f["key"] for f in fields]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            errors.append("Plusieurs questions utilisent le même nom de colonne : " + ", "
                          "".join(duplicates) + ".")

        return Parsed(cls(title, description, cooperative_id, fields), errors)

@dataclass
class PendingUpload:
    """A photo or signature to store before the response is inserted."""
    key: str
    content: bytes
    content_type: str
    extension: str


@dataclass
class ResponseInput:
    """A validated response: database values, plus files still to upload
    (their storage paths are added to `values` by the service)."""
    farmer_id: Optional[str]
    parcel_id: Optional[str]
    values: Dict[str, Any] = field(default_factory=dict)
    uploads: List[PendingUpload] = field(default_factory=list)

    @classmethod
    def parse(cls, fields: List[Dict[str, Any]], form: MultiDict,
              files: Mapping[str, FileStorage]) -> Parsed["ResponseInput"]:
        """Check every answer against `fields` (one published version)."""
        data = cls(farmer_id=form.get("farmer_id", "").strip() or None,
                   parcel_id=form.get("parcel_id", "").strip() or None)
        errors: List[str] = []
        tz = _client_timezone(form.get("_tz_offset", ""))

        for f in fields:
            try:
                _parse_answer(f, form, files, tz, data)
            except _AnswerError as exc:
                errors.append(f"{f['label']}: {exc}")
        return Parsed(data, errors)


class _AnswerError(ValueError):
    """An answer that doesn't fit its question."""


def _client_timezone(offset: str) -> timezone:
    """The browser's timezone (JS getTimezoneOffset(), minutes), default UTC."""
    try:
        minutes = int(offset)
    except ValueError:
        return timezone.utc
    if abs(minutes) > 14 * 60:
        return timezone.utc
    return timezone(-timedelta(minutes=minutes))


def _parse_answer(f: Dict[str, Any], form: MultiDict,
                  files: Mapping[str, FileStorage], tz: timezone,
                  data: ResponseInput) -> None:
    key, ftype, required = f["key"], f["type"], f.get("required", False)
    name = f"f_{key}"

    if ftype == "multiple_choice":
        picked = [v for v in form.getlist(name) if v]
        if not picked:
            if required:
                raise _AnswerError("choisissez au moins une option.")
            return
        if not set(picked) <= set(f.get("options", [])):
            raise _AnswerError("choisissez parmi les options proposées.")
        data.values[key] = picked
        return

    if ftype == "geolocation":
        lat, lng = form.get(f"{name}__lat", "").strip(), form.get(f"{name}__lng", "").strip()
        if not lat and not lng:
            if required:
                raise _AnswerError("enregistrez la position.")
            return
        try:
            lat_f, lng_f = float(lat), float(lng)
        except ValueError:
            raise _AnswerError("saisissez une latitude et une longitude valides.") from None
        if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
            raise _AnswerError("les coordonnées sont hors limites.")
        data.values[key] = f"SRID=4326;POINT({lng_f:.7f} {lat_f:.7f})"
        return

    if ftype == "geoshape":
        raw = form.get(name, "").strip()
        if not raw:
            if required:
                raise _AnswerError("tracez la délimitation sur la carte.")
            return
        try:
            geometry = json.loads(raw)
        except ValueError:
            raise _AnswerError("la délimitation n'a pas pu être lue.") from None
        ring, problem = drawn_ring(geometry)
        if problem:
            raise _AnswerError(problem[:1].lower() + problem[1:])
        if not shapely.Polygon(ring).is_valid:
            raise _AnswerError("le contour se croise lui-même.")
        data.values[key] = "SRID=4326;POLYGON((" + ", ".join(
            f"{lng:.7f} {lat:.7f}" for lng, lat in ring) + "))"
        return

    if ftype == "photo":
        upload = files.get(name)
        if not upload or not upload.filename:
            if required:
                raise _AnswerError("ajoutez une photo.")
            return
        extension = PHOTO_TYPES.get(upload.mimetype)
        if extension is None:
            raise _AnswerError("la photo doit être une image JPEG, PNG ou WebP.")
        content = upload.read(MAX_PHOTO_BYTES + 1)
        if len(content) > MAX_PHOTO_BYTES:
            raise _AnswerError(f"la photo dépasse {MAX_PHOTO_BYTES // (1024 * 1024)} Mo.")
        data.uploads.append(PendingUpload(key, content, upload.mimetype, extension))
        return

    if ftype == "signature":
        raw = form.get(name, "")
        if not raw:
            if required:
                raise _AnswerError("veuillez signer.")
            return
        if not raw.startswith(SIGNATURE_PREFIX):
            raise _AnswerError("la signature n'a pas pu être lue.")
        try:
            content = base64.b64decode(raw[len(SIGNATURE_PREFIX):], validate=True)
        except (binascii.Error, ValueError):
            raise _AnswerError("la signature n'a pas pu être lue.") from None
        if len(content) > MAX_SIGNATURE_BYTES:
            raise _AnswerError("l'image de la signature est trop lourde.")
        data.uploads.append(PendingUpload(key, content, "image/png", "png"))
        return

    value = form.get(name, "").strip()
    if not value:
        if required:
            raise _AnswerError("cette question est obligatoire.")
        return

    if ftype in ("text_short", "text_long"):
        limit = MAX_SHORT_TEXT if ftype == "text_short" else MAX_LONG_TEXT
        if len(value) > limit:
            raise _AnswerError(f"{limit} caractères au maximum.")
        data.values[key] = value
    elif ftype == "integer":
        if not re.fullmatch(r"-?\d{1,18}", value):
            raise _AnswerError("saisissez un nombre entier.")
        data.values[key] = int(value)
    elif ftype == "decimal":
        try:
            number = Decimal(value.replace(",", "."))
        except InvalidOperation:
            raise _AnswerError("saisissez un nombre.") from None
        if not number.is_finite():
            raise _AnswerError("saisissez un nombre.")
        data.values[key] = str(number)          # exact, cast to numeric in SQL
    elif ftype == "date":
        try:
            data.values[key] = date.fromisoformat(value).isoformat()
        except ValueError:
            raise _AnswerError("saisissez une date valide.") from None
    elif ftype == "time":
        try:
            data.values[key] = time.fromisoformat(value).isoformat()
        except ValueError:
            raise _AnswerError("saisissez une heure valide.") from None
    elif ftype == "datetime":
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise _AnswerError("saisissez une date et une heure valides.") from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        data.values[key] = moment.isoformat()
    elif ftype == "single_choice":
        if value not in f.get("options", []):
            raise _AnswerError("choisissez parmi les options proposées.")
        data.values[key] = value
    elif ftype == "scale":
        if not re.fullmatch(r"-?\d+", value) or not (
                f["min"] <= int(value) <= f["max"]):
            raise _AnswerError(f"choisissez une valeur entre {f['min']} et {f['max']}.")
        data.values[key] = int(value)
