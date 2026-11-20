"""
Parcel sheets ("fiches parcellaires", Avenant n°1 lot E) - managers.

A template (public.fiche_modeles) is an ordered list of sections, each
taken from one source - the parcel itself, its latest bio audit, or its
latest answer to a survey form - with the fields to show, in order, under
the label the manager chose (the column names in the database never
change). The PDF is built here (reportlab), for one parcel or a batch,
from fiche_parcel_data(): one SQL call for the whole batch.
"""

import io
import json
import logging
import re
import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from reportlab.graphics.shapes import Drawing, Polygon as RLPolygon
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from core.models.choices import CAMPAIGN_STATUS_LABELS_FR
from core.services.clients import service_db
from core.services.data_service import ExportFile
from core.services.errors import ServiceError

log = logging.getLogger(__name__)

MAX_SECTIONS = 20
MAX_FIELDS = 80
MAX_BATCH = 200
_SOURCE_RE = re.compile(r"^(parcel|audit|form:[0-9a-f-]{36})$")

# What a sheet can show from the parcel and from its latest audit:
# technical key -> default label (renamed freely in the template).
PARCEL_FIELDS = {
    "nom": "Nom de la parcelle",
    "farmer_name": "Producteur",
    "farmer_id_card": "N° CIN du producteur",
    "farmer_gender": "Genre du producteur",
    "farmer_year_of_birth": "Année de naissance",
    "cooperative_name": "Coopérative",
    "area_ha": "Superficie (ha)",
    "bio_campaign_status": "Statut campagne bio",
    "center": "Position (centre)",
    "created_at": "Date d'enregistrement",
    "uuid": "Identifiant",
}
AUDIT_FIELDS = {
    "audit_date": "Date de l'audit",
    "auditor_name": "Auditeur",
    "conversion_start_date": "Début de conversion",
    "conversion_status": "Statut de conversion",
    "inputs_used": "Intrants utilisés",
    "seed_origin": "Origine des semences",
    "pest_management_method": "Gestion des ravageurs",
    "neighboring_crops": "Cultures voisines",
    "buffer_zone_distance_m": "Zone tampon (m)",
    "contamination_risk": "Risque de contamination",
    "parcel_register_kept": "Registre parcellaire tenu",
    "inputs_register_kept": "Registre des intrants tenu",
    "harvest_register_kept": "Registre des récoltes tenu",
    "sales_register_kept": "Registre des ventes tenu",
    "bio_nonbio_separation": "Séparation bio / non bio",
    "storage_handling_notes": "Stockage et manutention",
    "non_conformities": "Non-conformités",
    "non_conformity_severity": "Gravité des non-conformités",
    "corrective_action_plan": "Plan d'actions correctives",
    "corrective_action_deadline": "Échéance des actions",
    "overall_status": "Conclusion",
}
_VALUE_LABELS = {
    "conversion_status": {"not_started": "Non commencée", "in_conversion": "En conversion",
                          "certified": "Certifiée"},
    "contamination_risk": {"low": "Faible", "medium": "Moyen", "high": "Élevé"},
    "non_conformity_severity": {"none": "Aucune", "minor": "Mineure", "major": "Majeure",
                                "critical": "Critique"},
    "overall_status": {"compliant": "Conforme", "non_compliant": "Non conforme",
                       "compliant_with_reservations": "Conforme avec réserves"},
    "farmer_gender": {"male": "Homme", "female": "Femme", "other": "Autre"},
    "bio_campaign_status": CAMPAIGN_STATUS_LABELS_FR,
}


# --------------------------------------------------------------------------
# Sources and templates
# --------------------------------------------------------------------------
def _owned_cooperatives(uid: str) -> List[Dict[str, Any]]:
    return (service_db().table("cooperative").select("uuid, name").eq("owner", uid)
            .order("name").execute().data or [])


def _forms_of(uid: str) -> List[Dict[str, Any]]:
    """Published survey forms of the manager's cooperatives, with every
    field ever published (all versions, latest label wins)."""
    coop_ids = [c["uuid"] for c in _owned_cooperatives(uid)]
    if not coop_ids:
        return []
    db = service_db()
    forms = (db.table("formulaires").select("id, title, cooperative_id, current_version")
             .in_("cooperative_id", coop_ids).not_.is_("current_version", "null")
             .order("title").execute().data or [])
    if not forms:
        return []
    versions = (db.table("formulaire_versions").select("formulaire_id, version, fields")
                .in_("formulaire_id", [f["id"] for f in forms]).order("version").execute().data or [])
    fields: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for v in versions:
        for fl in v.get("fields") or []:
            if fl.get("type") in ("photo", "signature"):
                continue                           # images: not printable here
            fields.setdefault(v["formulaire_id"], {})[fl["key"]] = {
                "key": fl["key"], "label": fl.get("label") or fl["key"], "type": fl.get("type")}
    return [{"id": f["id"], "title": f["title"],
             "fields": [{"key": "submitted_at", "label": "Date de la réponse", "type": "datetime"},
                        *fields.get(f["id"], {}).values()]}
            for f in forms]


def sources(uid: str) -> Dict[str, Any]:
    return {
        "parcel": [{"key": k, "label": v} for k, v in PARCEL_FIELDS.items()],
        "audit": [{"key": k, "label": v} for k, v in AUDIT_FIELDS.items()],
        "forms": _forms_of(uid),
    }


def _clean_config(raw: Any) -> Dict[str, Any]:
    sections = (raw or {}).get("sections") if isinstance(raw, dict) else None
    if not isinstance(sections, list) or not sections:
        raise ServiceError("Ajoutez au moins une section à la fiche.")
    if len(sections) > MAX_SECTIONS:
        raise ServiceError(f"Une fiche compte au plus {MAX_SECTIONS} sections.")
    clean = []
    for s in sections:
        if not isinstance(s, dict) or not _SOURCE_RE.match(str(s.get("source") or "")):
            raise ServiceError("Une section de la fiche a une source inconnue.")
        fields = s.get("fields") or []
        if not isinstance(fields, list) or not fields:
            raise ServiceError("Chaque section doit contenir au moins un champ.")
        if len(fields) > MAX_FIELDS:
            raise ServiceError(f"Une section compte au plus {MAX_FIELDS} champs.")
        clean_fields = []
        for fl in fields:
            key = str((fl or {}).get("key") or "")
            label = str((fl or {}).get("label") or "").strip()[:80]
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key):
                raise ServiceError("Un champ de la fiche est invalide.")
            clean_fields.append({"key": key, "label": label or key})
        clean.append({"source": s["source"], "title": str(s.get("title") or "").strip()[:120],
                      "fields": clean_fields})
    return {"sections": clean}


def list_templates(uid: str) -> List[Dict[str, Any]]:
    return (service_db().table("fiche_modeles").select("id, name, config, updated_at")
            .eq("owner", uid).order("name").execute().data or [])


def save_template(uid: str, template_id: Optional[str], name: str, config: Any) -> Dict[str, Any]:
    name = (name or "").strip()
    if not 2 <= len(name) <= 120:
        raise ServiceError("Donnez un nom au modèle (2 à 120 caractères).")
    row = {"name": name, "config": _clean_config(config),
           "updated_at": datetime.now(timezone.utc).isoformat()}
    table = service_db().table("fiche_modeles")
    if template_id:
        saved = table.update(row).eq("id", template_id).eq("owner", uid).execute().data
        if not saved:
            raise ServiceError("Ce modèle est introuvable.")
    else:
        saved = table.insert({**row, "owner": uid}).execute().data
    return saved[0]


def delete_template(uid: str, template_id: str) -> None:
    deleted = (service_db().table("fiche_modeles").delete().eq("id", template_id)
               .eq("owner", uid).execute().data)
    if not deleted:
        raise ServiceError("Ce modèle est introuvable.")


def template(uid: str, template_id: str) -> Dict[str, Any]:
    rows = (service_db().table("fiche_modeles").select("id, name, config").eq("id", template_id)
            .eq("owner", uid).limit(1).execute().data)
    if not rows:
        raise ServiceError("Ce modèle est introuvable.")
    return rows[0]


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def _format(key: str, value: Any) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if key in _VALUE_LABELS and isinstance(value, str):
        return _VALUE_LABELS[key].get(value, value)
    if isinstance(value, bool):
        return "Oui" if value else "Non"
    if isinstance(value, dict) and "coordinates" in value:
        kind, coords = value.get("type"), value.get("coordinates")
        if kind == "Point":
            return f"{coords[1]:.6f}, {coords[0]:.6f}"
        if kind == "Polygon":
            return f"Polygone ({max(len(coords[0]) - 1, 0)} sommets)"
        return kind or "Géométrie"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",")
    text = str(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text[8:10]}/{text[5:7]}/{text[:4]}"
    if key == "area_ha":
        try:
            return f"{float(text):.4f}".replace(".", ",")
        except ValueError:
            return text
    return text


def _source_values(source: str, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if source == "parcel":
        parcel = dict(item.get("parcel") or {})
        if parcel.get("center_lat") is not None:
            parcel["center"] = f"{parcel['center_lat']:.6f}, {parcel['center_lng']:.6f}"
        return parcel
    if source == "audit":
        return item.get("audit")
    return (item.get("forms") or {}).get(source.split(":", 1)[1])


def _outline(geometry: Optional[Dict[str, Any]], size: float = 42 * mm) -> Optional[Drawing]:
    """The parcel outline, drawn to scale (longitude compressed by cos lat)."""
    if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    rings = ([geometry["coordinates"][0]] if geometry["type"] == "Polygon"
             else [p[0] for p in geometry["coordinates"]])
    pts = [pt for ring in rings for pt in ring]
    if len(pts) < 3:
        return None
    lat0 = sum(p[1] for p in pts) / len(pts)
    k = math.cos(math.radians(lat0))
    xs = [p[0] * k for p in pts]
    ys = [p[1] for p in pts]
    w, h = max(max(xs) - min(xs), 1e-12), max(max(ys) - min(ys), 1e-12)
    scale = (size - 8) / max(w, h)
    drawing = Drawing(size, size)
    for ring in rings:
        flat = []
        for lng, lat in ring:
            flat += [4 + (lng * k - min(xs)) * scale + (size - 8 - w * scale) / 2,
                     4 + (lat - min(ys)) * scale + (size - 8 - h * scale) / 2]
        drawing.add(RLPolygon(flat, fillColor=colors.HexColor("#E9D8B4"),
                              strokeColor=colors.HexColor("#1B4332"), strokeWidth=1.2))
    return drawing


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=17, leading=21,
                                textColor=colors.HexColor("#1B4332"), alignment=TA_LEFT,
                                spaceAfter=2),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9, textColor=colors.grey),
        "section": ParagraphStyle("h", parent=base["Heading3"], fontSize=11.5,
                                  textColor=colors.HexColor("#1B4332"), spaceBefore=10, spaceAfter=4),
        "label": ParagraphStyle("l", parent=base["Normal"], fontSize=9, textColor=colors.HexColor("#444444")),
        "value": ParagraphStyle("v", parent=base["Normal"], fontSize=9.5, leading=12),
        "empty": ParagraphStyle("e", parent=base["Normal"], fontSize=9, textColor=colors.grey),
    }


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


_DEFAULT_TITLES = {"parcel": "Parcelle", "audit": "Dernier audit bio"}


def build_pdf(uid: str, template_name: str, config: Any, parcel_ids: List[str]) -> ExportFile:
    cfg = _clean_config(config)
    ids = list(dict.fromkeys(str(p) for p in parcel_ids or []))
    if not ids:
        raise ServiceError("Choisissez au moins une parcelle.")
    if len(ids) > MAX_BATCH:
        raise ServiceError(f"{MAX_BATCH} parcelles au plus par document.")
    form_ids = [s["source"].split(":", 1)[1] for s in cfg["sections"] if s["source"].startswith("form:")]
    form_titles = {f["id"]: f["title"] for f in _forms_of(uid)} if form_ids else {}
    try:
        items = service_db().rpc("fiche_parcel_data", {
            "p_uid": uid, "p_parcel_ids": ids, "p_form_ids": form_ids}).execute().data or []
    except Exception as exc:
        if "PGRST202" in str(exc) or "fiche_parcel_data" in str(exc):
            raise ServiceError("Les fiches parcellaires ne sont pas encore installées sur le serveur "
                               "(migration 20261120_avenant1.sql).") from exc
        log.warning("fiche_parcel_data failed: %s", exc)
        raise ServiceError("Les données des parcelles n'ont pas pu être lues.") from exc
    if not items:
        raise ServiceError("Aucune de ces parcelles n'est accessible.")
    order = {pid: i for i, pid in enumerate(ids)}
    items.sort(key=lambda it: order.get(it["uuid"], 0))

    st = _styles()
    story: List[Any] = []
    today = date.today().strftime("%d/%m/%Y")
    for n, item in enumerate(items):
        if n:
            story.append(PageBreak())
        parcel = item.get("parcel") or {}
        head = [Paragraph(_esc(parcel.get("nom") or "Parcelle"), st["title"]),
                Paragraph(_esc(f"{template_name} · {parcel.get('farmer_name') or ''} · "
                               f"{parcel.get('cooperative_name') or ''} · édité le {today}"), st["sub"])]
        drawing = _outline(item.get("geometry"))
        if drawing:
            header = Table([[head, drawing]], colWidths=[None, 46 * mm])
            header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                        ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
            story.append(header)
        else:
            story.extend(head)
        story.append(Spacer(1, 4 * mm))

        for s in cfg["sections"]:
            source = s["source"]
            title = s["title"] or _DEFAULT_TITLES.get(source) or form_titles.get(
                source.split(":", 1)[-1], "Formulaire")
            values = _source_values(source, item)
            block: List[Any] = [Paragraph(_esc(title), st["section"])]
            if not values:
                block.append(Paragraph("Aucune donnée pour cette parcelle.", st["empty"]))
            else:
                rows = [[Paragraph(_esc(f["label"]), st["label"]),
                         Paragraph(_esc(_format(f["key"], values.get(f["key"]))), st["value"])]
                        for f in s["fields"]]
                table = Table(rows, colWidths=[58 * mm, None], repeatRows=0)
                table.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#F6F4EF")]),
                    ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                block.append(table)
            # Short sections stay on one page; long ones may break.
            if len(s["fields"]) <= 25:
                story.append(KeepTogether(block))
            else:
                story.extend(block)

    buffer = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(18 * mm, 10 * mm, "Smart Saha - fiche parcellaire")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=template_name, author="Smart Saha")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    slug = re.sub(r"[^a-z0-9]+", "-", template_name.lower()).strip("-") or "fiche"
    name = (f"fiche-{slug}-{re.sub(r'[^a-z0-9]+', '-', (items[0]['parcel'].get('nom') or 'parcelle').lower()).strip('-')}.pdf"
            if len(items) == 1 else f"fiches-{slug}-{len(items)}-parcelles.pdf")
    return ExportFile(content=buffer.getvalue(), filename=name, mimetype="application/pdf")


def parse_ids(value: Any) -> List[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [v for v in value.split(",") if v]
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if re.fullmatch(r"[0-9a-f-]{36}", str(v))]
