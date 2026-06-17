"""
TDD (Red phase) tests for the rewritten staff user-creation form.

The user-creation form is being rewritten to subclass Django's
UserCreationForm. That means:
- a two-field password (password1 + password2 confirmation)
- Django's full password validation run WITH the prospective user, so
  similarity-to-username is enforced.

New form fields (REPLACING the old single 'password' field):
    username, email, first_name, last_name, password1, password2

Endpoint: POST '/manager/users/' creates a staff user (auth required).
A GET '/manager/users/' returns 200 with a 'users' list in context.

These tests are written FROM THE SPEC ONLY, before the implementation exists.
"""

import pytest
from django.test import Client
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages

User = get_user_model()


# A strong password that is NOT similar to the usernames used below.
STRONG_PASSWORD = 'Ztx9!quvw3Lm'


def get_messages_list(response):
    """Extract messages from a response."""
    return [str(m) for m in get_messages(response.wsgi_request)]


def make_authenticated_client(username):
    """Create an authenticated user and return a logged-in Client."""
    User.objects.create_user(username=username, password='AuthPass123!xyz')
    client = Client()
    client.login(username=username, password='AuthPass123!xyz')
    return client


@pytest.mark.django_db
class TestSignupFormUserCreation:
    """Tests for the rewritten UserCreationForm-based staff user creation."""

    def test_get_users_page_returns_200_with_users_list(self):
        """GET '/manager/users/' returns 200 and a 'users' list in context."""
        client = make_authenticated_client('signup_get_auth')
        response = client.get('/manager/users/')

        assert response.status_code == 200
        assert 'users' in response.context

    def test_valid_creation_persists_user_and_shows_success(self):
        """Valid POST creates the user, persists profile fields, shows success."""
        client = make_authenticated_client('signup_valid_auth')
        new_username = 'signup_valid_newuser'

        response = client.post('/manager/users/', {
            'username': new_username,
            'email': 'signup_valid_newuser@example.com',
            'first_name': 'Valid',
            'last_name': 'Newuser',
            'password1': STRONG_PASSWORD,
            'password2': STRONG_PASSWORD,
        })

        # Primary assertion: the user IS created.
        assert User.objects.filter(username=new_username).exists()

        created = User.objects.get(username=new_username)
        assert created.email == 'signup_valid_newuser@example.com'
        assert created.first_name == 'Valid'
        assert created.last_name == 'Newuser'

        # A success message is shown.
        messages = get_messages_list(response)
        assert len(messages) >= 1

    def test_duplicate_username_does_not_create_second_user(self):
        """A duplicate username is rejected: exactly one user keeps that name."""
        dup_username = 'signup_dup_user'
        User.objects.create_user(username=dup_username, password='ExistingPass9!q')

        client = make_authenticated_client('signup_dup_auth')
        response = client.post('/manager/users/', {
            'username': dup_username,
            'email': 'signup_dup_new@example.com',
            'first_name': 'Dup',
            'last_name': 'Licate',
            'password1': STRONG_PASSWORD,
            'password2': STRONG_PASSWORD,
        })

        # Still exactly ONE user with that username (not duplicated).
        assert User.objects.filter(username=dup_username).count() == 1

        assert response.status_code == 200
        messages = get_messages_list(response)
        assert len(messages) >= 1

    def test_weak_password_is_rejected(self):
        """A weak password (matching, but '123') is rejected; user NOT created."""
        client = make_authenticated_client('signup_weak_auth')
        new_username = 'signup_weak_newuser'

        response = client.post('/manager/users/', {
            'username': new_username,
            'email': 'signup_weak_newuser@example.com',
            'first_name': 'Weak',
            'last_name': 'Password',
            'password1': '123',
            'password2': '123',
        })

        assert not User.objects.filter(username=new_username).exists()
        assert response.status_code == 200
        messages = get_messages_list(response)
        assert len(messages) >= 1

    def test_password_confirmation_mismatch_is_rejected(self):
        """Two individually-strong but mismatched passwords are rejected."""
        client = make_authenticated_client('signup_mismatch_auth')
        new_username = 'signup_mismatch_newuser'

        response = client.post('/manager/users/', {
            'username': new_username,
            'email': 'signup_mismatch_newuser@example.com',
            'first_name': 'Mis',
            'last_name': 'Match',
            'password1': 'Ztx9!quvw3Lm',
            'password2': 'Different9!xyz',
        })

        assert not User.objects.filter(username=new_username).exists()
        assert response.status_code == 200
        messages = get_messages_list(response)
        assert len(messages) >= 1

    def test_password_too_similar_to_username_is_rejected(self):
        """A password equal to the username is rejected for similarity."""
        client = make_authenticated_client('signup_similar_auth')
        new_username = 'harrypotter22'

        response = client.post('/manager/users/', {
            'username': new_username,
            'email': 'signup_similar_newuser@example.com',
            'first_name': 'Harry',
            'last_name': 'Potter',
            'password1': new_username,
            'password2': new_username,
        })

        assert not User.objects.filter(username=new_username).exists()
        assert response.status_code == 200
        messages = get_messages_list(response)
        assert len(messages) >= 1

    def test_missing_username_is_rejected(self):
        """An empty username is rejected; no user created."""
        client = make_authenticated_client('signup_missing_auth')

        response = client.post('/manager/users/', {
            'username': '',
            'email': 'signup_missing_newuser@example.com',
            'first_name': 'Miss',
            'last_name': 'Ing',
            'password1': STRONG_PASSWORD,
            'password2': STRONG_PASSWORD,
        })

        assert not User.objects.filter(username='').exists()
        assert response.status_code == 200
        messages = get_messages_list(response)
        assert len(messages) >= 1
