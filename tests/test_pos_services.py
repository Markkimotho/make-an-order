"""Unit tests for POS service helpers (pure, no DB)."""
import pytest

from services.mpesa_service import format_phone, parse_stk_callback
from services.otp_service import generate_otp, verify_otp, peek_otp, secrets_compare
from services.receipt_service import _two_col, _center, _line, WIDTH


# --------------------------------------------------------------------------
# format_phone
# --------------------------------------------------------------------------

class TestFormatPhone:
    @pytest.mark.parametrize("raw,expected", [
        ("0712345678",   "254712345678"),
        ("+254712345678","254712345678"),
        ("254712345678", "254712345678"),
        ("712345678",    "254712345678"),
        ("0112345678",   "254112345678"),
        ("112345678",    "254112345678"),
        ("+254 712 345 678", "254712345678"),
    ])
    def test_valid(self, raw, expected):
        assert format_phone(raw) == expected

    @pytest.mark.parametrize("bad", ["", "12345", "abcdefghij"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            format_phone(bad)


# --------------------------------------------------------------------------
# parse_stk_callback
# --------------------------------------------------------------------------

class TestParseStkCallback:
    def test_success(self):
        body = {"Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_1",
            "ResultCode": 0,
            "ResultDesc": "Success",
            "CallbackMetadata": {"Item": [
                {"Name": "Amount", "Value": 100.0},
                {"Name": "MpesaReceiptNumber", "Value": "QABCXYZ123"},
                {"Name": "PhoneNumber", "Value": 254712345678},
                {"Name": "TransactionDate", "Value": 20260502123456},
            ]},
        }}}
        out = parse_stk_callback(body)
        assert out["success"] is True
        assert out["checkout_request_id"] == "ws_CO_1"
        assert out["result_code"] == 0
        assert out["amount"] == 100.0
        assert out["mpesa_receipt"] == "QABCXYZ123"
        assert out["phone_number"] == "254712345678"
        assert out["transaction_date"] == "20260502123456"

    def test_failure(self):
        body = {"Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_2",
            "ResultCode": 1032,
            "ResultDesc": "Cancelled by user",
        }}}
        out = parse_stk_callback(body)
        assert out["success"] is False
        assert out["result_code"] == 1032
        assert out["amount"] is None
        assert out["mpesa_receipt"] is None

    def test_empty_body(self):
        out = parse_stk_callback({})
        assert out["success"] is False
        assert out["checkout_request_id"] is None


# --------------------------------------------------------------------------
# OTP service (uses in-memory fallback when Redis is absent)
# --------------------------------------------------------------------------

class TestOtpService:
    def test_generate_returns_numeric(self):
        otp = generate_otp("test:1")
        assert otp.isdigit()
        assert len(otp) == 6

    def test_generate_custom_length(self):
        otp = generate_otp("test:2", length=8)
        assert len(otp) == 8

    def test_generate_invalid_length(self):
        with pytest.raises(ValueError):
            generate_otp("test:3", length=2)

    def test_verify_success_consumes(self):
        otp = generate_otp("test:consume")
        assert verify_otp("test:consume", otp) is True
        # Second attempt should fail (already consumed)
        assert verify_otp("test:consume", otp) is False

    def test_verify_no_consume(self):
        otp = generate_otp("test:peek")
        assert verify_otp("test:peek", otp, consume=False) is True
        assert verify_otp("test:peek", otp, consume=False) is True

    def test_verify_wrong_code(self):
        generate_otp("test:wrong")
        assert verify_otp("test:wrong", "000000") is False

    def test_verify_unknown_scope(self):
        assert verify_otp("test:nonexistent", "123456") is False

    def test_verify_empty_candidate(self):
        generate_otp("test:empty")
        assert verify_otp("test:empty", "") is False

    def test_peek_does_not_consume(self):
        otp = generate_otp("test:peeking")
        assert peek_otp("test:peeking") == otp
        assert peek_otp("test:peeking") == otp  # still there
        assert verify_otp("test:peeking", otp) is True

    def test_secrets_compare(self):
        assert secrets_compare("abc123", "abc123") is True
        assert secrets_compare("abc123", "abc124") is False


# --------------------------------------------------------------------------
# Receipt formatting helpers
# --------------------------------------------------------------------------

class TestReceiptHelpers:
    def test_line_width(self):
        assert len(_line()) == WIDTH
        assert len(_line("=")) == WIDTH

    def test_center(self):
        out = _center("Hello")
        assert "Hello" in out
        # left-padded so total length up to right edge of text equals WIDTH
        assert len(out) <= WIDTH

    def test_two_col_width(self):
        out = _two_col("Subtotal", "100.00")
        assert len(out) == WIDTH
        assert out.startswith("Subtotal")
        assert out.endswith("100.00")

    def test_two_col_truncates_long_left(self):
        out = _two_col("X" * 100, "10")
        assert len(out) == WIDTH
