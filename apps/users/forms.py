"""Auth forms for the staff app.

Login uses Django's AuthenticationForm directly (the view keeps a generic error
message + the per-IP throttle, so we don't leak which field/credential failed or
whether an account is inactive). Staff user creation uses StaffUserCreationForm,
a UserCreationForm subclass that adds the optional profile fields and — because
UserCreationForm validates the password against the prospective user — enforces
the password-confirmation match and the similarity-to-username rule that the
previous hand-rolled validate_password(user=None) call silently skipped.
"""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

User = get_user_model()


class StaffUserCreationForm(UserCreationForm):
    """Create a staff user. Brings UserCreationForm's password1/password2
    confirmation and full password validation (incl. similarity to the username),
    plus the optional email / first name / last name profile fields."""

    email = forms.EmailField(required=False)
    first_name = forms.CharField(required=False)
    last_name = forms.CharField(required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email', 'first_name', 'last_name')
