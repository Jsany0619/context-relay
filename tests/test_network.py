"""Private-network diagnostics report evidence without leaking account metadata."""
import json
import subprocess
import unittest
from unittest.mock import patch
from relay.network import diagnostics, summarize_status


class NetworkTests(unittest.TestCase):
    def test_connected_status_does_not_leak_identifiers_or_claim_phone_reachable(self):
        result = summarize_status({"BackendState": "Running", "TailscaleIPs": ["100.80.1.2", "fd7a::1", "8.8.8.8"],
            "AuthURL": "private-login-url", "User": {"private-account": {}},
            "Peer": {"private-host": {"Online": True}, "offline-host": {"Online": False}}})
        self.assertEqual(result["local_ipv4"], ["100.80.1.2"])
        self.assertEqual(result["online_peers"], 1)
        self.assertNotIn("private-", json.dumps(result))
        self.assertIn("不证明", result["detail"])

    def test_stopped_network_does_not_offer_stale_ip(self):
        result = summarize_status({"BackendState": "NeedsLogin", "TailscaleIPs": ["100.80.1.2"]})
        self.assertEqual(result["state"], "not_connected")
        self.assertEqual(result["local_ipv4"], [])

    def test_missing_client_never_installs_or_runs_anything(self):
        with patch("relay.network.Path.is_file", return_value=False), patch("relay.network.subprocess.run") as run:
            self.assertEqual(diagnostics()["state"], "not_installed")
            run.assert_not_called()

    def test_timeout_returns_unknown_without_raw_error(self):
        with patch("relay.network.Path.is_file", return_value=True), patch("relay.network.subprocess.run",
                side_effect=subprocess.TimeoutExpired("status", 5, stderr=b"private-login-url")):
            result = diagnostics()
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("private-login", json.dumps(result))
