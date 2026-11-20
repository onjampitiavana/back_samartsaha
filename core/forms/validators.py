"""Validation rules shared by the WTForms forms and the plain-form routes.

The Nuxt app applies the same expressions (nuxt/app/utils/validation.ts) to
guide the input; these are the ones that decide."""

import re
from typing import Optional

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.]{3,30}$")

# Malagasy mobile numbers: 032 / 033 / 034 / 037 / 038, or +261 without the
# leading 0, then 7 digits, spaces allowed between the groups
# (034 12 345 67, +261 34 12 345 67, 0341234567).
MOBILE_RE = re.compile(
    r"^(032|033|034|037|038|\+261\s?(32|33|34|37|38))\s?\d{2}\s?\d{3}\s?\d{2}$")
MOBILE_MESSAGE = ("Saisissez un numéro de mobile malgache valide (ex. 034 12 345 67 ou "
                  "+261 34 12 345 67).")

# A standard address: local part, @, domain with a 2+ letter extension.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$")
EMAIL_MESSAGE = "Saisissez une adresse e-mail valide (ex. prenom.nom@exemple.org)."

# Malagasy national identity card (CIN): exactly 12 digits.
CIN_RE = re.compile(r"^\d{12}$")
CIN_MESSAGE = "Le numéro de CIN doit contenir exactement 12 chiffres, sans espace ni lettre."

# Person names: letters (accents included), separated by spaces, apostrophes,
# hyphens or dots; 2 to 120 characters. No digits, no other symbols.
PERSON_NAME_RE = re.compile(r"^(?=.{2,120}$)[^\W\d_]+(?:[\s'’.-]+[^\W\d_]+)*\.?$")
PERSON_NAME_MESSAGE = ("{label} : 2 à 120 caractères, lettres uniquement (espaces, « ' », "
                       "« - » et « . » acceptés).")

# Names of organisations (cooperatives): letters, digits, spaces and
# ' ’ . - & ( ) ; 2 to 120 characters, starting with a letter or a digit.
ORG_NAME_RE = re.compile(r"^(?=.{2,120}$)[^\W_][\w\s'’.&()-]*$")
ORG_NAME_MESSAGE = ("Le nom de la coopérative : 2 à 120 caractères (lettres, chiffres, espaces, "
                    "« ' », « - », « . », « & », parenthèses).")

# Postal addresses: letters, digits, spaces and , ' ’ . - / # ° ( ) ; 3 to 200.
# Region / commune names: letters, digits, spaces and ' ’ . - ( ), 2 to 100.
PLACE_RE = re.compile(r"^(?=.{2,100}$)[\w\s'’.()-]+$")
PLACE_MESSAGE = "{label} : 2 à 100 caractères (lettres, chiffres, espaces et « ' . - ( ) »)."
ADDRESS_RE = re.compile(r"^(?=.{3,200}$)[\w\s,'’./#°()-]+$")
ADDRESS_MESSAGE = ("L'adresse : 3 à 200 caractères (lettres, chiffres, espaces et "
                   "« , ' . - / # ° »).")

# Crop names: letters, spaces, apostrophes and hyphens; 2 to 60 characters.
CROP_RE = re.compile(r"^(?=.{2,60}$)[^\W\d_]+(?:[\s'’-]+[^\W\d_]+)*$")
CROP_MESSAGE = "Une culture : 2 à 60 caractères, lettres uniquement (espaces, « ' » et « - » acceptés)."

# Positive whole numbers (household size): 0 to 99.
HOUSEHOLD_RE = re.compile(r"^\d{1,2}$")
# Positive decimal number (distances, areas), dot or comma as separator.
POSITIVE_NUMBER_RE = re.compile(r"^\d{1,9}(?:[.,]\d{1,4})?$")

MIN_PASSWORD_LENGTH = 8


def matches(pattern: "re.Pattern[str]", value: Optional[str]) -> bool:
    """True if the whole `value` (stripped) matches `pattern`."""
    return bool(pattern.match((value or "").strip()))


def person_name_problem(value: str, label: str = "Le nom complet") -> Optional[str]:
    """Why `value` is not a valid person name (in French), or None."""
    return None if matches(PERSON_NAME_RE, value) else PERSON_NAME_MESSAGE.format(label=label)


# --------------------------------------------------------------------------
# WTForms validators
# --------------------------------------------------------------------------
class PersonName:
    """WTForms validator: PERSON_NAME_RE, with `label` in the message."""

    def __init__(self, label: str = "Le nom complet") -> None:
        self.label = label

    def __call__(self, form, field) -> None:
        from wtforms.validators import ValidationError
        problem = person_name_problem(field.data or "", self.label)
        if problem:
            raise ValidationError(problem)


class StrongPassword:
    """WTForms validator: password_service.password_problem (8 characters
    with a lower-case letter, an upper-case letter, a digit and a symbol).
    Empty values are left to DataRequired / Optional."""

    def __call__(self, form, field) -> None:
        from wtforms.validators import ValidationError
        from core.services.password_service import password_problem
        if field.data:
            problem = password_problem(field.data)
            if problem:
                raise ValidationError(problem)
