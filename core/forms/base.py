from flask_wtf import FlaskForm
class Form(FlaskForm):
    class Meta:
        locales = ("fr_FR", "fr")
