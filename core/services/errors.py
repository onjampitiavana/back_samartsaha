"""Service-layer errors and user-facing messages for raw backend errors.

Every message shown to a user is in French. Raw errors (PostgREST,
Postgres, Supabase Auth) are logged by the services and replaced here by a
plain sentence: users never see SQL, error codes or English."""

import re
from typing import Optional, Tuple


class ServiceError(Exception):
    """An expected failure whose message is safe to show to the user.

    Services raise it (or a subclass) for every failure the user can act on;
    routes catch it and flash str(exc). Unexpected exceptions are logged by
    the service and re-raised as a ServiceError with a generic message."""


# Messages raised by the SQL functions (schema.sql, migrations/*.sql) that
# the user can act on. The functions keep their English text: the mobile app
# calls them too.
_SQL_MESSAGES: Tuple[Tuple[str, str], ...] = (
    (r"nothing to publish", "Aucune modification à publier."),
    (r"a form needs at least one field", "Ajoutez au moins une question avant de publier."),
    (r"a form can have", "Le formulaire contient trop de questions."),
    (r"field keys must be unique", "Plusieurs questions utilisent le même nom de colonne."),
    (r"archived forms", "Un formulaire archivé ne peut pas être modifié."),
    (r"invalid field key", "Un nom de colonne n'est pas valide (lettres a-z, chiffres et « _ »)."),
    (r"reserved field key", "Un nom de colonne est réservé : choisissez-en un autre."),
    (r"needs at least two options", "Une question à choix doit proposer au moins deux options."),
    (r"invalid scale range", "Une échelle a des bornes invalides."),
    (r"unknown field type", "Un type de question est inconnu."),
    (r"form not found", "Ce formulaire n'existe pas ou a été supprimé."),
    (r"farmer not found", "Ce producteur est introuvable dans votre coopérative."),
    (r"parcel not found", "Cette parcelle est introuvable."),
    (r"cooperative not found", "Cette coopérative est introuvable."),
    (r"cooperative is inactive", "Cette coopérative est désactivée : aucun ajout n'y est possible. "
                                 "Réactivez-la d'abord."),
    (r"cooperative must be deactivated", "Désactivez la coopérative avant de la supprimer."),
    (r"cooperative still has linked data", "Cette coopérative ne peut pas être supprimée : des "
                                           "données y sont encore rattachées."),
    (r"parcel id already used", "Cette parcelle a déjà été enregistrée."),
    (r"at least 3 points", "Une parcelle doit avoir au moins 3 points."),
    (r"not attached to a cooperative", "Ce compte n'est rattaché à aucune coopérative."),
    (r"validated audit is locked", "Un audit enregistré ne peut plus être modifié."),
    (r"not authenticated", "Votre session a expiré. Veuillez vous reconnecter."),
    # PostGIS, while storing an imported shape
    (r"z dimension|m dimension", "Les coordonnées du fichier ont une dimension (altitude) que la "
                                 "base n'accepte pas."),
    (r"invalid geojson|unknown geojson|geojson|parse error - invalid geometry",
     "Une forme du fichier a des coordonnées invalides."),
    (r"too few points|at least four points|linearring|closed linestring", "Un contour du fichier a moins de 3 points "
                                                  "ou n'est pas fermé."),
    (r"shape \d+ is not a polygon", "Une forme du fichier n'est pas un polygone de parcelle."),
    (r"no parcel to import", "Le fichier ne contient aucun polygone de parcelle."),
)


def sql_message(raw: Optional[str]) -> Optional[str]:
    """French message for a known SQL-function error, or None."""
    raw_lower = (raw or "").lower()
    return next((fr for pattern, fr in _SQL_MESSAGES if re.search(pattern, raw_lower)), None)


def friendly_db_error(raw: str) -> str:
    """Map a PostgREST / Postgres error text to a user-facing message."""
    known = sql_message(raw)
    if known:
        return known
    raw_lower = raw.lower()
    if "duplicate" in raw_lower or "unique" in raw_lower:
        return "Cet élément existe déjà."
    if "foreign key" in raw_lower:
        return "Cette sélection n'est plus valide. Actualisez la page puis réessayez."
    if "row-level security" in raw_lower or "permission denied" in raw_lower:
        return "Vous n'avez pas l'autorisation d'effectuer cette action."
    if "check constraint" in raw_lower or "violates check" in raw_lower:
        return "Une valeur saisie n'est pas acceptée. Vérifiez le formulaire."
    if "not-null" in raw_lower or "null value" in raw_lower:
        return "Un champ obligatoire est manquant."
    if "timed out" in raw_lower or "timeout" in raw_lower or "connection" in raw_lower:
        return "La base de données est momentanément injoignable. Veuillez réessayer."
    return "L'enregistrement a échoué. Veuillez réessayer."


def friendly_auth_error(raw: str) -> str:
    """Map a Supabase Auth error text to a user-facing message."""
    raw_lower = raw.lower()
    if ("already registered" in raw_lower or "already exists" in raw_lower
            or "already been registered" in raw_lower):
        return "Un compte existe déjà avec cette adresse e-mail. Essayez plutôt de vous connecter."
    if "banned" in raw_lower:
        return "Ce compte a été désactivé. Contactez votre administrateur."
    if "invalid login credentials" in raw_lower:
        return "Adresse e-mail ou mot de passe incorrect."
    if "email not confirmed" in raw_lower:
        return "Confirmez votre adresse e-mail avant de vous connecter."
    if "password" in raw_lower and ("least" in raw_lower or "weak" in raw_lower
                                    or "should contain" in raw_lower):
        return "Le mot de passe ne respecte pas les exigences de sécurité."
    if "rate limit" in raw_lower or "too many" in raw_lower:
        return "Trop de tentatives. Patientez quelques minutes puis réessayez."
    if "invalid" in raw_lower and "email" in raw_lower:
        return "Adresse email introuvable"
    return "Une erreur est survenue. Veuillez réessayer."
