from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom User model with role-based access control."""

    ROLE_CHOICES = [
        ('MANAGER', 'Manager'),
        ('AGENT', 'Agent'),
    ]

    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default='AGENT',
    )

    def __str__(self):
        return f"{self.username} ({self.role})"

    def is_manager(self):
        return self.role == 'MANAGER'

    def is_agent(self):
        return self.role == 'AGENT'
