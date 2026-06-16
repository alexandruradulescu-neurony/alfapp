from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """The single application user. The manager/agent role split was removed —
    there is one trusted user type (a manager) and access is gated only by
    authentication. Kept as a custom model so future fields have a home."""

    class Meta(AbstractUser.Meta):
        # Deterministic default order for any unordered User queryset (e.g. a
        # paginated admin/list view) — avoids undefined DB ordering.
        ordering = ('username',)

    def __str__(self):
        return self.username
