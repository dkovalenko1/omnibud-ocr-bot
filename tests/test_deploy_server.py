import hashlib
import hmac
import unittest


class DeployServerSecurityTests(unittest.TestCase):
    def test_missing_webhook_secret_rejects_requests(self):
        import deploy_server

        deploy_server.WEBHOOK_SECRET = ""

        self.assertFalse(deploy_server._verify_signature(b"{}", ""))

    def test_valid_signature_is_accepted(self):
        import deploy_server

        body = b'{"ref":"refs/heads/main"}'
        deploy_server.WEBHOOK_SECRET = "test-secret"
        signature = "sha256=" + hmac.new(
            b"test-secret",
            body,
            hashlib.sha256,
        ).hexdigest()

        self.assertTrue(deploy_server._verify_signature(body, signature))


if __name__ == "__main__":
    unittest.main()
