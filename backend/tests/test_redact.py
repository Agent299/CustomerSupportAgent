import pytest

from app.privacy.redact import Profile, redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "mongodb+srv://alice:s3cr3t@cluster0.example.net/db",
            "mongodb+srv://alice:<PASSWORD_MASKED>@cluster0.example.net/db",
        ),
        (
            "mongodb://myUser:D1fficultP%40ssw0rd@mongodb0.example.com:27017",
            "mongodb://myUser:<PASSWORD_MASKED>@mongodb0.example.com:27017",
        ),
        # Placeholders in docs are left alone.
        ("mongodb://<username>:<password>@host", "mongodb://<username>:<password>@host"),
        ("mongodb://localhost:27017", "mongodb://localhost:27017"),
    ],
)
def test_connection_string_passwords(text: str, expected: str) -> None:
    assert redact(text, Profile.SECRETS) == expected


def test_api_keys_masked() -> None:
    text = "key sk-proj-abcdefghijklmnopqrstuvwxyz123456 and AKIAABCDEFGHIJKLMNOP"

    assert redact(text, Profile.SECRETS) == "key <API_KEY_MASKED> and <API_KEY_MASKED>"


def test_only_luhn_valid_card_numbers_are_masked() -> None:
    text = "card 4111 1111 1111 1111, order 1234567890123"

    assert redact(text, Profile.SECRETS) == "card <CARD_MASKED>, order 1234567890123"


def test_secrets_profile_keeps_emails_full_profile_masks_them() -> None:
    text = "contact catelyn@gameofthron.es or +1 415-555-0100"

    assert redact(text, Profile.SECRETS) == text
    assert redact(text, Profile.FULL) == "contact <EMAIL_MASKED> or <PHONE_MASKED>"


def test_version_numbers_and_ports_are_not_phones() -> None:
    text = "Upgrade from 8.0.12 to 9.0 on port 27017"

    assert redact(text, Profile.FULL) == text
