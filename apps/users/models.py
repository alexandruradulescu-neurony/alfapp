from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """The single application user. The manager/agent role split was removed —
    there is one trusted user type (a manager) and access is gated only by
    authentication. Kept as a custom model so future fields have a home."""

    def __str__(self):
        return self.username
