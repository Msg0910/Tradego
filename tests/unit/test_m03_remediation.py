"""
Regression tests for M-03 extra_params secret redaction in BrokerCredentialsConfig:
- Sensitive keys in extra_params are recursively redacted to [REDACTED]
- Non-sensitive keys in extra_params are preserved
- __repr__ and __str__ do not leak extra_params secrets
- Audit logging of credentials does not contain secrets
"""

import json
import os
import shutil
import tempfile
import unittest

from gateway.broker_connectivity import BrokerConnectivityManager
from gateway.broker_credentials import BrokerCredentialsConfig
from gateway.recovery import TradingGuard
from gateway.security import Tier1AuditLogger


class TestM03BrokerCredentialsRedaction(unittest.TestCase):
    """M-03 BrokerCredentialsConfig extra_params redaction regression suite."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.temp_dir, "test_audit_m03.jsonl")

    def tearDown(self) -> None:
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_extra_params_sensitive_key_redacted(self) -> None:
        """Sensitive keys in extra_params (and nested dicts) are masked to [REDACTED]."""
        creds = BrokerCredentialsConfig(
            venue_name="DHAN",
            client_id="CLIENT_ABC123",
            access_token="TOP_LEVEL_ACCESS_SECRET",
            api_key="TOP_LEVEL_API_KEY",
            secret_key="TOP_LEVEL_SECRET_KEY",
            extra_params={
                "password": "SUPER_SECRET_PASSWORD",
                "nested_secret": "NESTED_SECRET_VAL",
                "refresh_token": "REFRESH_SECRET_VAL",
                "auth_code": "AUTH_CODE_SECRET_VAL",
                "client_secret": "CLIENT_SECRET_VAL",
                "nested_dict": {
                    "token": "DEEP_SECRET_TOKEN",
                    "secret_sub": "DEEP_SUB_SECRET",
                },
                "nested_list": [
                    {"api_key": "LIST_SECRET_KEY"},
                    {"safe_item": 123},
                ],
            },
        )

        redacted = creds.redacted_dict()

        # Top-level secrets
        self.assertEqual(redacted["access_token"], "[REDACTED]")
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["secret_key"], "[REDACTED]")

        # Extra params secrets
        ep = redacted.get("extra_params", {})
        self.assertEqual(ep.get("password"), "[REDACTED]")
        self.assertEqual(ep.get("nested_secret"), "[REDACTED]")
        self.assertEqual(ep.get("refresh_token"), "[REDACTED]")
        self.assertEqual(ep.get("auth_code"), "[REDACTED]")
        self.assertEqual(ep.get("client_secret"), "[REDACTED]")

        # Nested dict secrets
        nested = ep.get("nested_dict", {})
        self.assertEqual(nested.get("token"), "[REDACTED]")
        self.assertEqual(nested.get("secret_sub"), "[REDACTED]")

        # Nested list secrets
        nested_list = ep.get("nested_list", [])
        self.assertEqual(nested_list[0].get("api_key"), "[REDACTED]")
        self.assertEqual(nested_list[1].get("safe_item"), 123)

        # Confirm zero sensitive values leaked in serialized JSON
        serialized = json.dumps(redacted)
        self.assertNotIn("TOP_LEVEL_ACCESS_SECRET", serialized)
        self.assertNotIn("SUPER_SECRET_PASSWORD", serialized)
        self.assertNotIn("NESTED_SECRET_VAL", serialized)
        self.assertNotIn("REFRESH_SECRET_VAL", serialized)
        self.assertNotIn("AUTH_CODE_SECRET_VAL", serialized)
        self.assertNotIn("CLIENT_SECRET_VAL", serialized)
        self.assertNotIn("DEEP_SECRET_TOKEN", serialized)
        self.assertNotIn("DEEP_SUB_SECRET", serialized)
        self.assertNotIn("LIST_SECRET_KEY", serialized)

    def test_extra_params_non_sensitive_key_preserved(self) -> None:
        """Non-sensitive parameters in extra_params are preserved unmodified."""
        creds = BrokerCredentialsConfig(
            venue_name="ZERODHA",
            client_id="CLIENT_SAFE",
            access_token="ACCESS_VAL",
            extra_params={
                "environment": "sandbox",
                "timeout_seconds": 30,
                "max_retries": 3,
                "use_tls": True,
                "endpoints": {
                    "base_url": "https://api.example.com",
                    "port": 443,
                },
            },
        )

        redacted = creds.redacted_dict()
        ep = redacted.get("extra_params", {})
        self.assertEqual(ep.get("environment"), "sandbox")
        self.assertEqual(ep.get("timeout_seconds"), 30)
        self.assertEqual(ep.get("max_retries"), 3)
        self.assertEqual(ep.get("use_tls"), True)
        self.assertEqual(ep.get("endpoints", {}).get("base_url"), "https://api.example.com")
        self.assertEqual(ep.get("endpoints", {}).get("port"), 443)

    def test_repr_does_not_leak_extra_params_secret(self) -> None:
        """repr() and str() of BrokerCredentialsConfig never expose extra_params secrets."""
        creds = BrokerCredentialsConfig(
            venue_name="DHAN",
            client_id="CLIENT_123456",
            access_token="SECRET_ACCESS_VAL",
            extra_params={
                "secret_key_param": "RAW_LEAK_SECRET_123",
                "nested": {"token": "RAW_LEAK_TOKEN_456"},
            },
        )

        r_repr = repr(creds)
        r_str = str(creds)

        self.assertNotIn("SECRET_ACCESS_VAL", r_repr)
        self.assertNotIn("RAW_LEAK_SECRET_123", r_repr)
        self.assertNotIn("RAW_LEAK_TOKEN_456", r_repr)

        self.assertNotIn("SECRET_ACCESS_VAL", r_str)
        self.assertNotIn("RAW_LEAK_SECRET_123", r_str)
        self.assertNotIn("RAW_LEAK_TOKEN_456", r_str)

        self.assertIn("[REDACTED]", r_repr)
        self.assertIn("[REDACTED]", r_str)

    def test_audit_log_does_not_contain_secrets(self) -> None:
        """Setting credentials on BrokerConnectivityManager does not leak extra_params secrets into audit."""
        guard = TradingGuard()
        audit = Tier1AuditLogger(log_file_path=self.audit_path)
        conn_mgr = BrokerConnectivityManager(
            trading_guard=guard,
            audit_logger=audit,
            active_broker="VENUE_TEST",
        )

        creds = BrokerCredentialsConfig(
            venue_name="VENUE_TEST",
            client_id="CLIENT_AUDIT_01",
            access_token="TOP_SECRET_AUDIT_TOKEN",
            extra_params={
                "password": "SUPER_SECRET_AUDIT_PASSWORD",
                "nested_token": "NESTED_AUDIT_TOKEN_VAL",
                "environment": "staging",
            },
        )

        conn_mgr.set_credentials(creds, operator_id="operator_security")
        audit.flush()
        audit.close()

        # Check in-memory records
        in_memory_records = audit.in_memory_records
        serialized_in_memory = json.dumps(in_memory_records)
        self.assertNotIn("TOP_SECRET_AUDIT_TOKEN", serialized_in_memory)
        self.assertNotIn("SUPER_SECRET_AUDIT_PASSWORD", serialized_in_memory)
        self.assertNotIn("NESTED_AUDIT_TOKEN_VAL", serialized_in_memory)

        # Check on-disk audit log file
        with open(self.audit_path, "r", encoding="utf-8") as f:
            disk_content = f.read()

        self.assertNotIn("TOP_SECRET_AUDIT_TOKEN", disk_content)
        self.assertNotIn("SUPER_SECRET_AUDIT_PASSWORD", disk_content)
        self.assertNotIn("NESTED_AUDIT_TOKEN_VAL", disk_content)
        self.assertIn("BROKER_CREDENTIALS_UPDATED", disk_content)


if __name__ == "__main__":
    unittest.main()
