import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Generic, List, Mapping, Optional, TypeVar

import shapely
from pyproj import Geod

from core.forms.validators import (
    ADDRESS_MESSAGE, ADDRESS_RE, CIN_MESSAGE, CIN_RE, CROP_MESSAGE, CROP_RE,
    EMAIL_MESSAGE, EMAIL_RE, HOUSEHOLD_RE, MOBILE_MESSAGE, MOBILE_RE,
    ORG_NAME_MESSAGE, ORG_NAME_RE, PLACE_MESSAGE, PLACE_RE, POSITIVE_NUMBER_RE, person_name_problem,
)
from core.models.choices import (
    AUDIT_STATUS_CHOICES, CAMPAIGN_STATUS_CHOICES, CONVERSION_STATUS_CHOICES,
    GENDER_CHOICES, LEGAL_STATUS_CHOICES, MARITAL_STATUS_CHOICES,
    RECORD_STATUS_CHOICES,
)

DEFAULT_LEGAL_STATUS = "cooperative"
DEFAULT_RECORD_STATUS = "active"
MAX_COOPERATIVE_CROPS = 20
MAX_CROP_LENGTH = 60

T = TypeVar("T")

_GEOD = Geod(ellps="WGS84")


@dataclass
class Parsed(Generic[T]):
    """A parsed form: `data` is only meaningful when `errors` is empty."""
    data: Optional[T]
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the form had no errors."""
        return not self.errors


def _text(form: Mapping[str, str], name: str) -> str:
    return form.get(name, "").strip()


def _optional_text(form: Mapping[str, str], name: str) -> Optional[str]:
    return _text(form, name) or None


def _year_in(value: str, low: int, high: int) -> bool:
    return value.isdigit() and low <= int(value) <= high


_ACTIVE_VALUES = {"true", "1", "on", "active", "actif"}
_INACTIVE_VALUES = {"false", "0", "off", "inactive", "inactif", "not_active"}


def parse_active_flag(raw: str) -> Optional[bool]:
    """'true' / '1' / 'active'... -> True, 'false' / '0' / 'inactive'... ->
    False, anything else (empty included) -> None."""
    raw = (raw or "").strip().lower()
    if raw in _ACTIVE_VALUES:
        return True
    if raw in _INACTIVE_VALUES:
        return False
    return None


@dataclass
class CooperativeInput:
    """A validated cooperative registration.

    Crops are free text, any number (`crops`); the first three are also kept
    in the historical main_crop / second_crop / third_crop columns. The
    legal status and the status are no longer asked: None leaves the stored
    value untouched (the database default on insert, see to_row)."""
    name: str
    president_name: str
    email: str
    mobile: Optional[str]
    address: str
    year_of_creation: int
    crops: List[str]
    legal_status: Optional[str] = None
    status: Optional[str] = None
    # Active (True, the default) or deactivated (False). None: unchanged.
    is_active: Optional[bool] = None
    # Optional location of the cooperative (map dashboard filters).
    region: Optional[str] = None
    commune: Optional[str] = None

    @property
    def main_crop(self) -> str:
        return self.crops[0]

    def to_row(self, creating: bool) -> Dict[str, Any]:
        """Column values to write. A new cooperative gets the default legal
        status and status when the form did not set them (the columns are
        NOT NULL)."""
        row: Dict[str, Any] = {
            "name": self.name,
            "president_name": self.president_name,
            "email": self.email,
            "mobile": self.mobile,
            "address": self.address,
            "year_of_creation": self.year_of_creation,
            "crops": list(self.crops),
            "main_crop": self.crops[0],
            "second_crop": self.crops[1] if len(self.crops) > 1 else None,
            "third_crop": self.crops[2] if len(self.crops) > 2 else None,
            "region": self.region,
            "commune": self.commune,
        }
        if self.legal_status is not None or creating:
            row["legal_status"] = self.legal_status or DEFAULT_LEGAL_STATUS
        if self.status is not None or creating:
            row["status"] = self.status or DEFAULT_RECORD_STATUS
        if self.is_active is not None or creating:
            active = True if self.is_active is None else self.is_active
            row["is_active"] = active
            # Historical text column, kept in sync for the mobile app / exports.
            row["status"] = "active" if active else "not_active"
        return row

    @classmethod
    def parse(cls, form: Mapping[str, str]) -> Parsed["CooperativeInput"]:
        """Validate the posted form; errors come back in display order.
        Crops come as repeated `crops` values, or (older forms) as
        main_crop / second_crop / third_crop."""
        name = _text(form, "name")
        president_name = _text(form, "president_name")
        email = _text(form, "email").lower()
        year = _text(form, "year_of_creation")
        legal_status = _text(form, "legal_status") or None
        status = _text(form, "status") or None
        raw_active = _text(form, "is_active").lower()
        is_active = parse_active_flag(raw_active)
        if is_active is None and status is not None:
            is_active = status != "not_active"
        getlist = getattr(form, "getlist", None)
        raw_crops = getlist("crops") if getlist else []
        if not raw_crops:
            raw_crops = [form.get(k, "") for k in ("main_crop", "second_crop", "third_crop")]
        crops: List[str] = []
        for crop in (" ".join(c.split()) for c in raw_crops):
            if crop and crop.lower() not in {c.lower() for c in crops}:
                crops.append(crop)

        address = _text(form, "address")
        mobile = _text(form, "mobile")
        region = " ".join(_text(form, "region").split())
        commune = " ".join(_text(form, "commune").split())

        errors = []
        if not name:
            errors.append("Saisissez le nom de la coopérative.")
        elif not ORG_NAME_RE.match(name):
            errors.append(ORG_NAME_MESSAGE)
        if not president_name:
            errors.append("Saisissez le nom du président.")
        else:
            problem = person_name_problem(president_name, "Le nom du président")
            if problem:
                errors.append(problem)
        if not address:
            errors.append("Saisissez l'adresse de la coopérative.")
        elif not ADDRESS_RE.match(address):
            errors.append(ADDRESS_MESSAGE)
        if region and not PLACE_RE.match(region):
            errors.append(PLACE_MESSAGE.format(label="La région"))
        if commune and not PLACE_RE.match(commune):
            errors.append(PLACE_MESSAGE.format(label="La commune"))
        if not email:
            errors.append("Saisissez l'adresse e-mail de la coopérative.")
        elif not EMAIL_RE.match(email):
            errors.append(EMAIL_MESSAGE)
        if mobile and not MOBILE_RE.match(mobile):
            errors.append(MOBILE_MESSAGE)
        if legal_status is not None and legal_status not in LEGAL_STATUS_CHOICES:
            errors.append("Choisissez un statut juridique valide.")
        if status is not None and status not in RECORD_STATUS_CHOICES:
            errors.append("Choisissez un statut valide.")
        if raw_active and is_active is None:
            errors.append("Choisissez un statut valide : active ou inactive.")
        if not crops:
            errors.append("Saisissez au moins une culture.")
        elif any(len(c) > MAX_CROP_LENGTH for c in crops):
            errors.append(f"Le nom d'une culture ne doit pas dépasser {MAX_CROP_LENGTH} caractères.")
        elif not all(CROP_RE.match(c) for c in crops):
            errors.append(CROP_MESSAGE)
        elif len(crops) > MAX_COOPERATIVE_CROPS:
            errors.append(f"{MAX_COOPERATIVE_CROPS} cultures au maximum.")
        if year.startswith("-"):
            errors.append("L'année de création ne peut pas être négative.")
        elif not _year_in(year, 1900, date.today().year):
            errors.append(f"Saisissez une année de création entre 1900 et {date.today().year}.")
        if errors:
            return Parsed(None, errors)

        return Parsed(cls(
            name=name,
            president_name=president_name,
            email=email,
            mobile=mobile or None,
            address=address,
            year_of_creation=int(year),
            crops=crops,
            legal_status=legal_status,
            status=status,
            is_active=is_active,
            region=region or None,
            commune=commune or None,
        ))


# --------------------------------------------------------------------------
# Farmers
# --------------------------------------------------------------------------
MIN_BIRTH_YEAR = 1900
MAX_BIRTH_YEAR = 2025          # farmers.year_of_birth check constraint
HOUSEHOLD_MESSAGE = "La taille du foyer doit être un nombre entier positif (0 à 99)."


def _farmer_identity_errors(full_name: str, year: str, id_card: str) -> List[str]:
    """Name, birth year and CIN checks shared by creation and edit."""
    errors = []
    if not full_name:
        errors.append("Saisissez le nom complet du producteur.")
    else:
        problem = person_name_problem(full_name)
        if problem:
            errors.append(problem)
    if not _year_in(year, MIN_BIRTH_YEAR, MAX_BIRTH_YEAR):
        errors.append(f"Saisissez une année de naissance entre {MIN_BIRTH_YEAR} et "
                      f"{MAX_BIRTH_YEAR} (4 chiffres).")
    if not id_card:
        errors.append("Saisissez le numéro de CIN du producteur.")
    elif not CIN_RE.match(id_card):
        errors.append(CIN_MESSAGE)
    return errors


@dataclass
class FarmerInput:
    """A validated farmer registration or inline edit."""
    full_name: str
    year_of_birth: int
    gender: str
    id_card: str
    marital_status: str
    household_number: int
    status: str
    cooperative_id: Optional[str] = None     # creation only
    handicap: Optional[bool] = None          # creation only

    @classmethod
    def parse_new(cls, form: Mapping[str, str]) -> Parsed["FarmerInput"]:
        """Registration form (/farmers): every field is checked."""
        cooperative_id = form.get("cooperative_id", "")
        full_name = _text(form, "full_name")
        year = _text(form, "year_of_birth")
        gender = form.get("gender", "")
        id_card = _text(form, "id_card")
        marital_status = form.get("marital_status", "")
        household = _text(form, "household_number")
        # No longer on the web form: the columns keep their defaults.
        status = form.get("status") or DEFAULT_RECORD_STATUS

        errors = []
        if not cooperative_id:
            errors.append("Choisissez la coopérative du producteur.")
        errors.extend(_farmer_identity_errors(full_name, year, id_card))
        if gender not in GENDER_CHOICES:
            errors.append("Choisissez le genre.")
        if marital_status not in MARITAL_STATUS_CHOICES:
            errors.append("Choisissez la situation matrimoniale.")
        if not HOUSEHOLD_RE.match(household):
            errors.append(HOUSEHOLD_MESSAGE)
        if status not in RECORD_STATUS_CHOICES:
            errors.append("Choisissez un statut.")
        if errors:
            return Parsed(None, errors)

        return Parsed(cls(
            cooperative_id=cooperative_id,
            full_name=full_name,
            year_of_birth=int(year),
            gender=gender,
            id_card=id_card,
            handicap=form.get("handicap", "no") == "yes",
            marital_status=marital_status,
            household_number=int(household),
            status=status,
        ))

    @classmethod
    def parse_edit(cls, form: Mapping[str, str]) -> Parsed["FarmerInput"]:
        """Edit: same fields and checks as the registration, minus the
        cooperative and handicap."""
        full_name = _text(form, "full_name")
        year = _text(form, "year_of_birth")
        gender = form.get("gender", "")
        marital_status = form.get("marital_status", "")
        household = _text(form, "household_number")
        status = form.get("status", "")
        id_card = _text(form, "id_card")

        errors = _farmer_identity_errors(full_name, year, id_card)
        if gender not in GENDER_CHOICES:
            errors.append("Choisissez le genre.")
        if marital_status not in MARITAL_STATUS_CHOICES:
            errors.append("Choisissez la situation matrimoniale.")
        if not HOUSEHOLD_RE.match(household):
            errors.append(HOUSEHOLD_MESSAGE)
        if status not in RECORD_STATUS_CHOICES:
            errors.append("Choisissez un statut.")
        if errors:
            return Parsed(None, errors)

        return Parsed(cls(
            full_name=full_name,
            year_of_birth=int(year),
            gender=gender,
            id_card=id_card,
            marital_status=marital_status,
            household_number=int(household),
            status=status,
        ))

    def editable_values(self) -> Dict[str, Any]:
        """Columns updated by the data page's inline edit."""
        return {
            "full_name": self.full_name,
            "year_of_birth": self.year_of_birth,
            "gender": self.gender,
            "id_card": self.id_card,
            "marital_status": self.marital_status,
            "household_number": self.household_number,
            "status": self.status,
        }


# --------------------------------------------------------------------------
# Parcels
# --------------------------------------------------------------------------
PARCEL_FILE_RE = re.compile(r"\.(kml|gpkg|zip)$", re.IGNORECASE)


@dataclass
class ParcelUploadInput:
    """A validated parcel file upload."""
    cooperative_id: str
    farmer_id: str
    upload: Any                                   # werkzeug FileStorage

    @classmethod
    def parse(cls, form: Mapping[str, str], files: Mapping[str, Any]
              ) -> Parsed["ParcelUploadInput"]:
        """Validate the posted form; errors come back in display order."""
        cooperative_id = form.get("cooperative_id", "")
        farmer_id = form.get("farmer_id", "")
        upload = files.get("parcel_file")

        errors = []
        if not cooperative_id:
            errors.append("Choisissez la coopérative.")
        if not farmer_id:
            errors.append("Choisissez le producteur à qui appartient cette parcelle.")
        if not upload or not upload.filename:
            errors.append("Joignez un fichier .kml, .gpkg ou un Shapefile compressé (.zip).")
        elif not PARCEL_FILE_RE.search(upload.filename):
            errors.append("Format de fichier non pris en charge : envoyez un fichier .kml, .gpkg "
                          "ou un .zip contenant un Shapefile.")
        if errors:
            return Parsed(None, errors)
        return Parsed(cls(cooperative_id, farmer_id, upload))


DRAWN_PARCEL_MAX_VERTICES = 5000
PARCEL_NAME_MAX = 120


def drawn_ring(geometry: Any) -> "tuple[Optional[List[List[float]]], Optional[str]]":
    """The exterior ring of a drawn GeoJSON Polygon, closed and without
    repeated consecutive vertices, or (None, the problem in French)."""
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        return None, "Tracez la parcelle sur la carte (un polygone)."
    rings = geometry.get("coordinates")
    if not isinstance(rings, list) or len(rings) != 1 or not isinstance(rings[0], list):
        return None, "Le tracé doit être un seul contour, sans trou."
    ring: List[List[float]] = []
    for point in rings[0]:
        if (not isinstance(point, (list, tuple)) or len(point) < 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for v in point[:2])):
            return None, "Le tracé contient un point illisible."
        lng, lat = float(point[0]), float(point[1])
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            return None, "Le tracé contient un point hors des coordonnées GPS valides."
        if not ring or ring[-1] != [lng, lat]:
            ring.append([lng, lat])
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3:
        return None, "Le tracé doit avoir au moins 3 sommets."
    if len(ring) > DRAWN_PARCEL_MAX_VERTICES:
        return None, f"Le tracé a trop de sommets ({DRAWN_PARCEL_MAX_VERTICES} au maximum)."
    return ring + [ring[0]], None


@dataclass
class ParcelDrawInput:
    """A validated parcel drawn on the map: same cooperative / farmer rules
    as ParcelUploadInput, plus a valid simple polygon (EPSG:4326)."""
    cooperative_id: str
    farmer_id: str
    geometry: Dict[str, Any]            # GeoJSON Polygon, ring closed
    nom: Optional[str]
    area_ha: float                      # geodesic (WGS 84), for the message only

    @classmethod
    def parse(cls, payload: Any) -> Parsed["ParcelDrawInput"]:
        """Validate the JSON body; errors come back in display order."""
        payload = payload if isinstance(payload, dict) else {}
        cooperative_id = str(payload.get("cooperative_id") or "").strip()
        farmer_id = str(payload.get("farmer_id") or "").strip()
        nom = str(payload.get("nom") or "").strip() or None

        errors = []
        if not cooperative_id:
            errors.append("Choisissez la coopérative.")
        if not farmer_id:
            errors.append("Choisissez le producteur à qui appartient cette parcelle.")
        if nom and len(nom) > PARCEL_NAME_MAX:
            errors.append(f"Le nom de la parcelle est trop long ({PARCEL_NAME_MAX} caractères "
                          "au maximum).")
        ring, problem = drawn_ring(payload.get("geometry"))
        area_ha = 0.0
        if problem:
            errors.append(problem)
        else:
            polygon = shapely.Polygon(ring)
            if not polygon.is_valid:
                errors.append("Le contour se croise lui-même : déplacez ou supprimez les "
                              "sommets en cause.")
            else:
                area_m2, _ = _GEOD.geometry_area_perimeter(polygon)
                area_ha = abs(area_m2) / 10000
                if area_ha <= 0:
                    errors.append("La parcelle tracée n'a pas de surface.")
        if errors:
            return Parsed(None, errors)
        return Parsed(cls(cooperative_id, farmer_id,
                          {"type": "Polygon", "coordinates": [ring]}, nom, area_ha))


@dataclass
class ParcelReassignInput:
    """A validated parcel reassignment."""
    cooperative_id: str
    farmer_id: str

    @classmethod
    def parse(cls, form: Mapping[str, str]) -> Parsed["ParcelReassignInput"]:
        """Validate the posted form; errors come back in display order."""
        cooperative_id = form.get("cooperative_id", "")
        farmer_id = form.get("farmer_id", "")
        if not cooperative_id or not farmer_id:
            return Parsed(None, ["Choisissez une coopérative et un producteur."])
        return Parsed(cls(cooperative_id, farmer_id))


@dataclass
class CampaignStatusInput:
    """A validated campaign status change."""
    entity_type: str
    entity_id: str
    new_status: str
    reason: Optional[str]

    @classmethod
    def parse(cls, form: Mapping[str, str]) -> Parsed["CampaignStatusInput"]:
        """Validate the posted form; errors come back in display order."""
        entity_type = form.get("entity_type", "")
        entity_id = form.get("entity_id", "")
        new_status = form.get("new_status", "")
        if (entity_type not in ("farmer", "parcel")
                or new_status not in CAMPAIGN_STATUS_CHOICES or not entity_id):
            return Parsed(None, ["Requête invalide : rechargez la page puis réessayez."])
        return Parsed(cls(entity_type, entity_id, new_status,
                          _optional_text(form, "reason")))


# --------------------------------------------------------------------------
# Bio audits
# --------------------------------------------------------------------------
_AUDIT_TEXT_FIELDS = (
    "inputs_used", "seed_origin", "pest_management_method",
    "neighboring_crops", "bio_nonbio_separation", "storage_handling_notes",
    "non_conformities", "corrective_action_plan", "auditor_signature",
)
_AUDIT_CHECKBOXES = (
    "parcel_register_kept", "inputs_register_kept", "harvest_register_kept",
    "sales_register_kept",
)


@dataclass
class AuditInput:
    """A validated bio audit."""
    values: Dict[str, Any]          # audits_bio columns, owner excluded

    @classmethod
    def parse(cls, form: Mapping[str, str]) -> Parsed["AuditInput"]:
        """Validate the posted form; errors come back in display order."""
        parcel_id = form.get("parcel_id", "")
        auditor_name = _text(form, "auditor_name")
        audit_date = _text(form, "audit_date")
        conversion_status = form.get("conversion_status", "")
        overall_status = form.get("overall_status", "")
        buffer_zone = _text(form, "buffer_zone_distance_m")

        errors = []
        if not parcel_id:
            errors.append("Choisissez la parcelle auditée.")
        if not auditor_name:
            errors.append("Saisissez le nom de l'auditeur.")
        else:
            problem = person_name_problem(auditor_name, "Le nom de l'auditeur")
            if problem:
                errors.append(problem)
        if not audit_date:
            errors.append("Saisissez la date de l'audit.")
        if conversion_status not in CONVERSION_STATUS_CHOICES:
            errors.append("Choisissez le statut de conversion.")
        if overall_status not in AUDIT_STATUS_CHOICES:
            errors.append("Choisissez la conclusion de l'audit.")
        buffer_zone_m: Optional[float] = None
        if buffer_zone:
            if POSITIVE_NUMBER_RE.match(buffer_zone):
                buffer_zone_m = float(buffer_zone.replace(",", "."))
            else:
                errors.append("La distance de zone tampon doit être un nombre positif, en mètres.")
        if errors:
            return Parsed(None, errors)

        values: Dict[str, Any] = {
            "parcel_id": parcel_id,
            "audit_date": audit_date,
            "auditor_name": auditor_name,
            "conversion_start_date": form.get("conversion_start_date") or None,
            "conversion_status": conversion_status,
            "buffer_zone_distance_m": buffer_zone_m,
            "contamination_risk": form.get("contamination_risk", "low"),
            "non_conformity_severity": form.get("non_conformity_severity", "none"),
            "corrective_action_deadline": form.get("corrective_action_deadline") or None,
            "overall_status": overall_status,
        }
        values.update({name: _optional_text(form, name) for name in _AUDIT_TEXT_FIELDS})
        values.update({name: form.get(name) == "on" for name in _AUDIT_CHECKBOXES})
        return Parsed(cls(values))
