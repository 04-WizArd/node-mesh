from django.contrib.auth.forms import UserCreationForm


class RegistrationForm(UserCreationForm):
    """Username + password registration (Django's validators apply)."""
