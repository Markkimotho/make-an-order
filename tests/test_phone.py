import pytest

from services.phone import InvalidPhone, normalize_ke_phone


@pytest.mark.parametrize("raw,expected", [
    ("0712345678", "254712345678"),
    ("+254712345678", "254712345678"),
    ("254712345678", "254712345678"),
    ("712345678", "254712345678"),
    ("0112345678", "254112345678"),
    ("112345678", "254112345678"),
    ("+254 712 345 678", "254712345678"),
    ("0110 123 456", "254110123456"),
    ("0712-345-678", "254712345678"),
    ("(+254) 722 000 111", "254722000111"),
])
def test_valid(raw, expected):
    assert normalize_ke_phone(raw) == expected


@pytest.mark.parametrize("bad", [
    "",
    None,
    "12345",
    "+25756098389",     # Burundi
    "0812345678",       # 08 is not a Kenyan mobile prefix
    "25471234567",      # one digit short
    "2547123456789",    # one digit long
    "abcdefghij",
])
def test_invalid(bad):
    with pytest.raises(InvalidPhone):
        normalize_ke_phone(bad)


def test_invalid_phone_is_a_value_error():
    assert issubclass(InvalidPhone, ValueError)


def test_mpesa_format_phone_uses_the_same_rules():
    from services.mpesa_service import format_phone
    assert format_phone("0712 345 678") == "254712345678"
    with pytest.raises(ValueError):
        format_phone("0812345678")
