"""Audit Bio report (PDF, reportlab) - same look as the parcel sheets
(core/services/fiche_service.py): forest green titles, light rows."""

import base64
import io
import re
from datetime import date
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from core.services.data_service import ExportFile

FOREST = colors.HexColor("#1B4332")
CANVAS = colors.HexColor("#F6F4EF")
LINE = colors.HexColor("#DDDDDD")

CONVERSION = {"not_started": "Non commencée", "in_conversion": "En conversion",
              "certified": "Certifiée bio"}
OVERALL = {"compliant": "Conforme", "non_compliant": "Non conforme",
           "compliant_with_reservations": "Conforme sous réserve"}
OVERALL_COLORS = {"compliant": ("#E3F1EA", "#1B4332"), "non_compliant": ("#FDECEC", "#B42318"),
                  "compliant_with_reservations": ("#F7EEDD", "#8A6327")}
RISK = {"low": "Faible", "medium": "Moyen", "high": "Élevé"}
SEVERITY = {"none": "Aucune", "minor": "Mineure", "major": "Majeure", "critical": "Critique"}
CROP_CYCLE = {"annual": "Culture annuelle (conversion : 2 ans)",
              "perennial": "Culture pérenne (conversion : 3 ans)",
              "grassland": "Prairie / fourrage (conversion : 2 ans)"}
CONVERSION_YEARS = {"annual": 2, "perennial": 3, "grassland": 2}
SEEDS = {"organic": "Semences / plants biologiques", "in_conversion": "Issus de parcelles en conversion",
         "farm_saved": "Semences de ferme (production bio)",
         "derogation": "Conventionnels non traités (dérogation)",
         "conventional": "Conventionnels sans dérogation"}

SIGNATURE_PREFIX = "data:image/png;base64,"

# Crop keys of the cooperatives (core/models/choices.py CROPS) in French,
# as nuxt/app/utils/labels.ts shows them; free-text crops stay as typed.
CROPS_FR = {
    "almond": "Amande", "apple": "Pomme", "avocado": "Avocat", "beans": "Haricots",
    "beans green": "Haricots verts", "cacao": "Cacao", "carrots": "Carottes",
    "cassava": "Manioc", "celery": "Céleri", "citrus": "Agrumes", "coffee": "Café",
    "cotton": "Coton", "eggplant": "Aubergine", "grapes": "Raisins", "grapewine": "Vigne",
    "lettuce": "Laitue", "maize": "Maïs", "mango": "Mangue", "millet": "Millet",
    "onion": "Oignon", "peach": "Pêche", "peas": "Pois", "pineapple": "Ananas",
    "pistachios": "Pistaches", "potato": "Pomme de terre", "radish": "Radis", "rice": "Riz",
    "sesame": "Sésame", "sorghum": "Sorgho", "soybeans": "Soja", "spinach": "Épinards",
    "strawberry": "Fraise", "sugarcane": "Canne à sucre", "sunflower": "Tournesol",
    "sweet potato": "Patate douce", "tomato": "Tomate", "vanilla": "Vanille", "walnut": "Noix",
}


def signature_bytes(value: Optional[str]) -> Optional[bytes]:
    """PNG bytes of a data-URL signature, or None."""
    if not value or not value.startswith(SIGNATURE_PREFIX):
        return None
    try:
        return base64.b64decode(value[len(SIGNATURE_PREFIX):])
    except ValueError:
        return None


def _esc(value: Any) -> str:
    text = "—" if value is None or value == "" else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _date(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(value)


def _yes(value: Any) -> str:
    return "Oui" if value else "Non"


def _number(value: Any, decimals: int = 2) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):,.{decimals}f}".replace(",", " ").replace(".", ",")


def _months_between(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day))


def conversion_summary(audit: Dict[str, Any]) -> Dict[str, str]:
    """Elapsed time and expected end of the conversion period."""
    try:
        start = date.fromisoformat(str(audit.get("conversion_start_date"))[:10])
        at = date.fromisoformat(str(audit.get("audit_date"))[:10])
    except ValueError:
        return {"elapsed": "—", "expected_end": "—"}
    months = _months_between(start, at)
    years, rest = divmod(months, 12)
    elapsed = " ".join(p for p in (f"{years} an{'s' if years > 1 else ''}" if years else "",
                                   f"{rest} mois" if rest or not years else "") if p)
    expected = "—"
    span = CONVERSION_YEARS.get(audit.get("crop_cycle") or "")
    if span:
        try:
            end = start.replace(year=start.year + span)
        except ValueError:                       # 29 February
            end = start.replace(year=start.year + span, day=28)
        expected = end.strftime("%d/%m/%Y")
    return {"elapsed": elapsed, "expected_end": expected}


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=18, leading=22,
                                textColor=FOREST, alignment=TA_LEFT, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9, textColor=colors.grey),
        "section": ParagraphStyle("h", parent=base["Heading3"], fontSize=11.5,
                                  textColor=FOREST, spaceBefore=10, spaceAfter=4),
        "label": ParagraphStyle("l", parent=base["Normal"], fontSize=9,
                                textColor=colors.HexColor("#444444")),
        "value": ParagraphStyle("v", parent=base["Normal"], fontSize=9.5, leading=12),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontSize=8.5, leading=11),
        "head": ParagraphStyle("hd", parent=base["Normal"], fontSize=8.5, leading=11,
                               textColor=colors.white),
        "status": ParagraphStyle("st", parent=base["Normal"], fontSize=13, leading=16),
        "warn": ParagraphStyle("w", parent=base["Normal"], fontSize=9.5, leading=12,
                               textColor=colors.HexColor("#B42318")),
    }


def _rows_table(st, rows: List[tuple]) -> Table:
    table = Table([[Paragraph(_esc(label), st["label"]),
                    Paragraph(_esc(value), st["value"])
                    if value is None or isinstance(value, (str, int, float)) else value]
                   for label, value in rows], colWidths=[62 * mm, None])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CANVAS]),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _section(st, title: str, rows: List[tuple]) -> KeepTogether:
    return KeepTogether([Paragraph(_esc(title), st["section"]), _rows_table(st, rows)])


def _prohibited(st, used: bool) -> Paragraph:
    return Paragraph("<b>CONSTATÉ</b>" if used else "Non constaté", st["warn"] if used else st["value"])


def build(audit: Dict[str, Any], context: Dict[str, Any],
          signature_png: Optional[bytes]) -> ExportFile:
    """The report of `audit`, with the identification of `context`
    (audit_bio_parcel_context)."""
    st = _styles()
    parcel = context.get("parcel") or {}
    farmer = context.get("farmer") or {}
    cooperative = context.get("cooperative") or {}
    conversion = conversion_summary(audit)
    status = audit.get("overall_status") or ""
    reference = str(audit.get("uuid", ""))[:8].upper()
    parcel_name = parcel.get("nom") or "Parcelle"

    story: List[Any] = [
        Paragraph("Rapport d'audit bio", st["title"]),
        Paragraph(_esc(f"{parcel_name} · Campagne {audit.get('campaign') or '—'} · "
                       f"Règlement (UE) 2018/848 · Réf. {reference} · "
                       f"édité le {date.today().strftime('%d/%m/%Y')}"), st["sub"]),
        Spacer(1, 4 * mm),
    ]

    bg, fg = OVERALL_COLORS.get(status, ("#EEEEEE", "#333333"))
    banner = Table([[Paragraph(f'<font color="{fg}"><b>Conclusion : '
                               f'{_esc(OVERALL.get(status, status))}</b></font>', st["status"]),
                     Paragraph(_esc(f"Audit du {_date(audit.get('audit_date'))} · "
                                    f"Prochain audit avant le {_date(audit.get('next_audit_due'))}"),
                               st["small"])]],
                   colWidths=[100 * mm, None])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                ("TOPPADDING", (0, 0), (-1, -1), 7),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    story.append(banner)

    crops = ", ".join(CROPS_FR.get(c.lower(), c) for c in audit.get("crops") or []) or "—"
    coords = (f"{parcel['lat']:.6f}, {parcel['lng']:.6f}"
              if parcel.get("lat") is not None and parcel.get("lng") is not None else "—")
    story.append(_section(st, "1. Identification", [
        ("Coopérative", cooperative.get("name")),
        ("Producteur", farmer.get("full_name")),
        ("N° CIN du producteur", farmer.get("id_card")),
        ("Parcelle", parcel_name),
        ("Superficie auditée (PostGIS)", f"{_number(audit.get('area_ha_audited'), 4)} ha"),
        ("Position (centre)", coords),
        ("Culture(s) principale(s)", crops),
        ("Type de culture", CROP_CYCLE.get(audit.get("crop_cycle") or "", "—")),
        ("Date de l'audit", _date(audit.get("audit_date"))),
        ("Auditeur", audit.get("auditor_name")),
    ]))

    story.append(_section(st, "2. Historique de conversion", [
        ("Début de conversion", _date(audit.get("conversion_start_date"))),
        ("Durée écoulée à la date de l'audit", conversion["elapsed"]),
        ("Fin de conversion réglementaire prévue", conversion["expected_end"]),
        ("Statut actuel", CONVERSION.get(audit.get("conversion_status") or "", "—")),
    ]))

    story.append(_section(st, "3. Pratiques agricoles", [
        ("Intrants utilisés", audit.get("inputs_used")),
        ("Semences / plants", SEEDS.get(audit.get("seed_category") or "", "—")),
        ("Origine des semences / plants", audit.get("seed_origin")),
        ("Méthodes de fertilisation", audit.get("fertilization_methods")),
        ("Protection des cultures", audit.get("pest_management_method")),
        ("Engrais chimiques de synthèse", _prohibited(st, audit.get("synthetic_fertilizers_used"))),
        ("Pesticides chimiques de synthèse", _prohibited(st, audit.get("synthetic_pesticides_used"))),
        ("OGM ou produits issus d'OGM", _prohibited(st, audit.get("gmo_used"))),
    ]))

    buffer = audit.get("buffer_zone_distance_m")
    story.append(_section(st, "4. Prévention des contaminations", [
        ("Cultures voisines", audit.get("neighboring_crops")),
        ("Voisinage en agriculture conventionnelle", _yes(audit.get("neighbors_conventional"))),
        ("Zone tampon présente", _yes(audit.get("buffer_zone_present"))),
        ("Distance de sécurité", f"{_number(buffer, 1)} m" if buffer is not None else "—"),
        ("Haies / barrières physiques", _yes(audit.get("hedges_present"))),
        ("Gestion des zones tampons", audit.get("buffer_zone_management")),
        ("Risque de contamination", RISK.get(audit.get("contamination_risk") or "", "—")),
    ]))

    story.append(_section(st, "5. Traçabilité documentaire", [
        ("Registre parcellaire tenu", _yes(audit.get("parcel_register_kept"))),
        ("Registre des intrants tenu", _yes(audit.get("inputs_register_kept"))),
        ("Registre des récoltes tenu", _yes(audit.get("harvest_register_kept"))),
        ("Registre des ventes tenu", _yes(audit.get("sales_register_kept"))),
        ("Flux bio / non-bio séparés", _yes(audit.get("flows_separated"))),
        ("Modalités de séparation", audit.get("bio_nonbio_separation")),
    ]))

    story.append(_section(st, "6. Stockage et manutention post-récolte", [
        ("Lieu de stockage dédié au bio", _yes(audit.get("storage_dedicated"))),
        ("Stockage propre, sans produit interdit", _yes(audit.get("storage_clean"))),
        ("Contenants et lots identifiés", _yes(audit.get("containers_labelled"))),
        ("Observations", audit.get("storage_handling_notes")),
    ]))

    items = audit.get("non_conformity_items") or []
    block: List[Any] = [Paragraph("7. Constat de conformité", st["section"])]
    if not items:
        block.append(Paragraph("Aucune non-conformité relevée.", st["value"]))
    else:
        rows = [[Paragraph(h, st["head"]) for h in
                 ("N°", "Non-conformité", "Gravité", "Action corrective", "Délai", "Suivi")]]
        for n, item in enumerate(items, start=1):
            done = item.get("resolved")
            rows.append([Paragraph(str(n), st["small"]),
                         Paragraph(_esc(item.get("description")), st["small"]),
                         Paragraph(_esc(SEVERITY.get(item.get("severity") or "", "—")), st["small"]),
                         Paragraph(_esc(item.get("corrective_action")), st["small"]),
                         Paragraph(_date(item.get("deadline")), st["small"]),
                         Paragraph(f"Levée le {_date(item.get('resolved_at'))}" if done else "En cours",
                                   st["small"])])
        table = Table(rows, colWidths=[8 * mm, 52 * mm, 18 * mm, 52 * mm, 20 * mm, None],
                      repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), FOREST),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, CANVAS]),
        ]))
        block.append(table)
    story.append(KeepTogether(block))

    signature: Any = "—"
    if signature_png:
        try:
            signature = Image(io.BytesIO(signature_png), width=55 * mm, height=22 * mm,
                              kind="proportional")
        except Exception:
            signature = "Signature enregistrée (image illisible)"
    story.append(_section(st, "8. Conclusion de l'audit", [
        ("Statut global", OVERALL.get(status, status)),
        ("Observations", audit.get("conclusion_notes")),
        ("Date de l'audit", _date(audit.get("audit_date"))),
        ("Auditeur / gestionnaire", audit.get("auditor_name")),
        ("Identifiant de l'auditeur", str(audit.get("owner") or "—")),
        ("Enregistré et validé le", _date(audit.get("created_at"))),
        ("Échéance de renouvellement", _date(audit.get("next_audit_due"))),
        ("Signature", signature),
    ]))

    out = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(18 * mm, 10 * mm, f"Smart Saha - rapport d'audit bio · réf. {reference}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"Rapport d'audit bio - {parcel_name}", author="Smart Saha")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    slug = re.sub(r"[^a-z0-9]+", "-", parcel_name.lower()).strip("-") or "parcelle"
    filename = f"audit-bio-{slug}-{audit.get('audit_date') or 'date'}.pdf"
    return ExportFile(content=out.getvalue(), filename=filename, mimetype="application/pdf")
