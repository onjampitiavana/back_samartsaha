"""Fixed option lists used by the forms and their validation."""

CROPS = sorted({
    "almond", "apple", "avocado", "cacao", "citrus", "coffee", "grapewine",
    "mango", "peach", "strawberry", "sugarcane", "vanilla", "walnut",
    "onion", "potato", "carrots", "maize", "celery", "lettuce", "spinach",
    "radish", "eggplant", "tomato", "sweet potato", "cassava",
    "beans green", "beans", "peas", "soybeans", "cotton", "sesame",
    "sunflower", "millet", "sorghum", "rice", "pineapple", "grapes",
    "pistachios",
})
LEGAL_STATUS_CHOICES = ["cooperative", "group", "other"]
RECORD_STATUS_CHOICES = ["active", "not_active", "na"]
GENDER_CHOICES = ["male", "female", "other"]
MARITAL_STATUS_CHOICES = ["single", "married", "divorced", "widowed", "other"]
CONVERSION_STATUS_CHOICES = ["not_started", "in_conversion", "certified"]
CONTAMINATION_RISK_CHOICES = ["low", "medium", "high"]
NON_CONFORMITY_SEVERITY_CHOICES = ["none", "minor", "major", "critical"]
AUDIT_STATUS_CHOICES = ["compliant", "non_compliant", "compliant_with_reservations"]
# Audit Bio (web, migrations/20261122_audits_bio_web.sql)
CROP_CYCLE_CHOICES = ["annual", "perennial", "grassland"]
SEED_CATEGORY_CHOICES = ["organic", "in_conversion", "farm_saved", "derogation", "conventional"]
NC_SEVERITY_CHOICES = ["minor", "major", "critical"]      # severity of one finding
CAMPAIGN_STATUS_CHOICES = ["considered", "banned"]
CAMPAIGN_STATUS_LABELS_FR = {"considered": "Retenu", "banned": "Exclu"}

# Survey form field types (public.formulaires.fields[].type), in the order
# the form builder offers them. The SQL column type of each is decided by
# public.formulaire_column_sql() (migrations/20261025_formulaires.sql).
FORM_FIELD_TYPES = {
    "text_short": "Short text",
    "text_long": "Long text",
    "integer": "Whole number",
    "decimal": "Decimal number",
    "date": "Date",
    "time": "Time",
    "datetime": "Date & time",
    "single_choice": "Single choice",
    "multiple_choice": "Multiple choice",
    "scale": "Scale",
    "geolocation": "Geolocation",
    "geoshape": "Boundary (polygon)",
    "photo": "Photo",
    "signature": "Signature",
}
CHOICE_FIELD_TYPES = ("single_choice", "multiple_choice")
FORM_STATUS_CHOICES = ["draft", "published"]
