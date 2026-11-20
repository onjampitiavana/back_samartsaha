"""Audit Bio (web) - validation of the JSON body sent by the Nuxt form.

The checklist follows Regulation (EU) 2018/848: conversion, agricultural
practices (no synthetic inputs, no GMO), prevention of contamination,
traceability, storage, findings and conclusion. Two modes:

  * draft  (final=False): only the parcel is required; every value given
    must still be well formed. Stored as-is in audits_bio_drafts.data.
  * final  (final=True):  the audit is complete, consistent and signed.
    Becomes one row of audits_bio, locked once recorded.
"""

import base64
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional

from core.forms.record_forms import Parsed
from core.forms.validators import CROP_MESSAGE, CROP_RE, POSITIVE_NUMBER_RE, person_name_problem
from core.models.choices import (
    AUDIT_STATUS_CHOICES, CONTAMINATION_RISK_CHOICES, CONVERSION_STATUS_CHOICES,
    CROP_CYCLE_CHOICES, NC_SEVERITY_CHOICES, NON_CONFORMITY_SEVERITY_CHOICES,
    SEED_CATEGORY_CHOICES,
)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CAMPAIGN_RE = re.compile(r"^\d{4}(?:[-/]\d{2,4})?$")
SIGNATURE_PREFIX = "data:image/png;base64,"

MAX_TEXT = 2000
MAX_CROPS = 20
MAX_NON_CONFORMITIES = 30
MAX_SIGNATURE_BYTES = 400_000

TEXT_FIELDS = {
    "inputs_used": "Les intrants utilisés",
    "seed_origin": "L'origine des semences",
    "fertilization_methods": "Les méthodes de fertilisation",
    "pest_management_method": "La protection des cultures",
    "neighboring_crops": "Les cultures voisines",
    "buffer_zone_management": "La gestion des zones tampons",
    "bio_nonbio_separation": "La séparation des flux",
    "storage_handling_notes": "Le stockage et la manutention",
    "conclusion_notes": "Les observations",
}

BOOL_FIELDS = (
    "synthetic_fertilizers_used", "synthetic_pesticides_used", "gmo_used",
    "neighbors_conventional", "buffer_zone_present", "hedges_present",
    "parcel_register_kept", "inputs_register_kept", "harvest_register_kept",
    "sales_register_kept", "flows_separated",
    "storage_dedicated", "storage_clean", "containers_labelled",
)

CHOICE_FIELDS = {
    "conversion_status": (CONVERSION_STATUS_CHOICES, "Choisissez le statut de conversion."),
    "crop_cycle": (CROP_CYCLE_CHOICES, "Choisissez le type de culture."),
    "seed_category": (SEED_CATEGORY_CHOICES, "Choisissez la catégorie des semences / plants."),
    "contamination_risk": (CONTAMINATION_RISK_CHOICES, "Choisissez le niveau de risque de contamination."),
    "overall_status": (AUDIT_STATUS_CHOICES, "Choisissez la conclusion de l'audit."),
}

_SEVERITY_RANK = {s: i for i, s in enumerate(NON_CONFORMITY_SEVERITY_CHOICES)}


def _str(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    return value.strip() if isinstance(value, str) else ""


def _bool(value: Any) -> bool:
    return value is True or value in ("on", "true", "1", 1)


def _date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _signature_problem(value: str) -> Optional[str]:
    if not value.startswith(SIGNATURE_PREFIX):
        return "La signature est illisible : signez à nouveau."
    try:
        raw = base64.b64decode(value[len(SIGNATURE_PREFIX):], validate=True)
    except ValueError:
        return "La signature est illisible : signez à nouveau."
    if len(raw) > MAX_SIGNATURE_BYTES:
        return "La signature est trop volumineuse : effacez-la et signez à nouveau."
    if not raw.startswith(b"\x89PNG"):
        return "La signature est illisible : signez à nouveau."
    return None


@dataclass
class AuditBioInput:
    """A validated audit: `values` are audits_bio columns (owner excluded)."""
    values: Dict[str, Any]

    @property
    def parcel_id(self) -> str:
        return self.values["parcel_id"]

    @classmethod
    def parse(cls, payload: Any, final: bool) -> Parsed["AuditBioInput"]:
        """Validate the JSON body; errors come back in display order."""
        if not isinstance(payload, dict):
            return Parsed(None, ["Requête invalide : rechargez la page puis réessayez."])
        errors: List[str] = []
        values: Dict[str, Any] = {}

        def required(message: str) -> None:
            if final:
                errors.append(message)

        # -- Identification -------------------------------------------------
        parcel_id = _str(payload, "parcel_id").lower()
        if not UUID_RE.match(parcel_id):
            errors.append("Choisissez la parcelle auditée.")
        values["parcel_id"] = parcel_id

        audit_date = _date(payload.get("audit_date"))
        if payload.get("audit_date") and audit_date is None:
            errors.append("La date de l'audit n'est pas valide.")
        elif audit_date is None:
            required("Saisissez la date de l'audit.")
        elif audit_date > date.today():
            errors.append("La date de l'audit ne peut pas être dans le futur.")
        values["audit_date"] = _iso(audit_date)

        campaign = _str(payload, "campaign")
        if campaign and not CAMPAIGN_RE.match(campaign):
            errors.append("La campagne s'écrit sous la forme 2026 ou 2025-2026.")
        values["campaign"] = campaign or (str(audit_date.year) if audit_date else None)

        auditor_name = _str(payload, "auditor_name")
        if not auditor_name:
            required("Saisissez le nom de l'auditeur.")
        else:
            problem = person_name_problem(auditor_name, "Le nom de l'auditeur")
            if problem:
                errors.append(problem)
        values["auditor_name"] = auditor_name or None

        crops_in = payload.get("crops") or []
        crops: List[str] = []
        if not isinstance(crops_in, list):
            crops_in = []
        for crop in crops_in:
            text = str(crop).strip()
            if not text or text.lower() in (c.lower() for c in crops):
                continue
            if not CROP_RE.match(text):
                errors.append(CROP_MESSAGE)
                break
            crops.append(text)
        if len(crops) > MAX_CROPS:
            errors.append(f"{MAX_CROPS} cultures au plus.")
        if not crops:
            required("Indiquez au moins une culture principale.")
        values["crops"] = crops[:MAX_CROPS]

        # -- Choices, texts, check boxes ------------------------------------
        for name, (choices, message) in CHOICE_FIELDS.items():
            value = _str(payload, name)
            if value and value not in choices:
                errors.append(message)
                value = ""
            values[name] = value or None
        for name in ("conversion_status", "overall_status"):
            if not values[name]:
                required(CHOICE_FIELDS[name][1])
        values["contamination_risk"] = values["contamination_risk"] or "low"

        for name, label in TEXT_FIELDS.items():
            text = _str(payload, name)
            if len(text) > MAX_TEXT:
                errors.append(f"{label} : {MAX_TEXT} caractères au plus.")
            values[name] = text[:MAX_TEXT] or None

        for name in BOOL_FIELDS:
            values[name] = _bool(payload.get(name))

        # -- Conversion -----------------------------------------------------
        start = _date(payload.get("conversion_start_date"))
        if payload.get("conversion_start_date") and start is None:
            errors.append("La date de début de conversion n'est pas valide.")
        elif start and audit_date and start > audit_date:
            errors.append("La conversion ne peut pas commencer après la date de l'audit.")
        elif not start and values["conversion_status"] in ("in_conversion", "certified"):
            required("Saisissez la date de début de conversion.")
        values["conversion_start_date"] = _iso(start)

        # -- Buffer zone ----------------------------------------------------
        buffer_text = str(payload.get("buffer_zone_distance_m") or "").strip()
        buffer_m: Optional[float] = None
        if buffer_text:
            if POSITIVE_NUMBER_RE.match(buffer_text):
                buffer_m = float(buffer_text.replace(",", "."))
            else:
                errors.append("La distance de la zone tampon doit être un nombre positif, en mètres.")
        values["buffer_zone_distance_m"] = buffer_m

        # -- Non-conformities -----------------------------------------------
        items_in = payload.get("non_conformity_items") or []
        if not isinstance(items_in, list):
            items_in = []
        if len(items_in) > MAX_NON_CONFORMITIES:
            errors.append(f"{MAX_NON_CONFORMITIES} non-conformités au plus par audit.")
        items: List[Dict[str, Any]] = []
        for n, raw in enumerate(items_in[:MAX_NON_CONFORMITIES], start=1):
            if not isinstance(raw, dict):
                continue
            description = _str(raw, "description")[:MAX_TEXT]
            action = _str(raw, "corrective_action")[:MAX_TEXT]
            severity = _str(raw, "severity")
            deadline = _date(raw.get("deadline"))
            if not (description or action or raw.get("deadline")):
                continue                                  # empty row of the form
            if not description:
                required(f"Non-conformité n° {n} : décrivez l'écart constaté.")
            if severity not in NC_SEVERITY_CHOICES:
                if severity or final:
                    errors.append(f"Non-conformité n° {n} : choisissez le niveau de gravité.")
                severity = ""
            if not action:
                required(f"Non-conformité n° {n} : précisez l'action corrective.")
            if raw.get("deadline") and deadline is None:
                errors.append(f"Non-conformité n° {n} : le délai n'est pas une date valide.")
            elif deadline is None:
                required(f"Non-conformité n° {n} : fixez le délai de mise en conformité.")
            elif audit_date and deadline < audit_date:
                errors.append(f"Non-conformité n° {n} : le délai doit suivre la date de l'audit.")
            items.append({"description": description, "severity": severity or None,
                          "corrective_action": action or None, "deadline": _iso(deadline),
                          "resolved": False, "resolved_at": None})
        values["non_conformity_items"] = items

        # -- Conclusion -----------------------------------------------------
        next_due = _date(payload.get("next_audit_due"))
        if payload.get("next_audit_due") and next_due is None:
            errors.append("La date du prochain audit n'est pas valide.")
        elif next_due and audit_date and next_due <= audit_date:
            errors.append("Le prochain audit doit être postérieur à la date de l'audit.")
        values["next_audit_due"] = _iso(next_due)

        signature = _str(payload, "auditor_signature")
        if signature:
            problem = _signature_problem(signature)
            if problem:
                errors.append(problem)
                signature = ""
        elif final:
            errors.append("Signez l'audit pour le valider.")
        values["auditor_signature"] = signature or None

        if final and not errors:
            errors.extend(_consistency_problems(values))
        if errors:
            return Parsed(None, errors)

        values.update(_legacy_summary(items))
        return Parsed(cls(values))


def _consistency_problems(values: Dict[str, Any]) -> List[str]:
    """Rules of Regulation (EU) 2018/848 the conclusion must respect."""
    problems = []
    status = values["overall_status"]
    open_severities = {i["severity"] for i in values["non_conformity_items"]}
    prohibited = [label for name, label in (
        ("synthetic_fertilizers_used", "engrais de synthèse"),
        ("synthetic_pesticides_used", "pesticides de synthèse"),
        ("gmo_used", "OGM")) if values[name]]
    if prohibited and status != "non_compliant":
        problems.append("Usage constaté d'intrants interdits (" + ", ".join(prohibited) + ") : "
                        "l'audit ne peut conclure qu'à « Non conforme » (art. 9, 11 et 24 et "
                        "annexe II du règlement (UE) 2018/848).")
    if prohibited and not values["non_conformity_items"]:
        problems.append("Consignez l'usage d'intrants interdits comme non-conformité, avec son "
                        "plan d'action.")
    if "critical" in open_severities and status != "non_compliant":
        problems.append("Une non-conformité critique impose la conclusion « Non conforme ».")
    if "major" in open_severities and status == "compliant":
        problems.append("Une non-conformité majeure est incompatible avec la conclusion "
                        "« Conforme » : choisissez « Conforme sous réserve » ou « Non conforme ».")
    if status in ("non_compliant", "compliant_with_reservations") and not values["non_conformity_items"]:
        problems.append("Décrivez les non-conformités qui justifient cette conclusion.")
    if values["conversion_status"] == "certified" and values["seed_category"] == "conventional":
        problems.append("Des semences conventionnelles sans dérogation ne permettent pas le statut "
                        "« Certifiée bio » : consignez une non-conformité ou corrigez la catégorie.")
    return problems


def _legacy_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The historical one-value columns (read by the mobile app, the map
    dashboard and the parcel sheets), summarised from the findings."""
    if not items:
        return {"non_conformities": None, "non_conformity_severity": "none",
                "corrective_action_plan": None, "corrective_action_deadline": None}
    worst = max((i["severity"] or "minor" for i in items), key=lambda s: _SEVERITY_RANK.get(s, 0))
    deadlines = sorted(i["deadline"] for i in items if i["deadline"])
    return {
        "non_conformities": "\n".join(f"- {i['description']}" for i in items if i["description"]) or None,
        "non_conformity_severity": worst,
        "corrective_action_plan": "\n".join(f"- {i['corrective_action']}" for i in items
                                            if i["corrective_action"]) or None,
        "corrective_action_deadline": deadlines[0] if deadlines else None,
    }
