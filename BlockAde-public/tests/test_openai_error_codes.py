import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from openai_match import OpenAIMatchError, OpenAIMatchProvider, OpenAIRateLimitError, _retry_delay


class ErrorCodeTests(unittest.TestCase):
    def test_429_distinguishes_billing_and_rate_without_leaking_message(self):
        provider = OpenAIMatchProvider(api_key="private-key")
        for code in ("insufficient_quota", "credit_balance_exhausted", "rate_limit_exceeded",
                     "project_spend_limit_exceeded", "organization_spend_limit_exceeded"):
            with self.subTest(code=code):
                body = io.BytesIO(json.dumps({"error": {"code": code, "message": "private-key account content"}}).encode())
                error = HTTPError("https://api.openai.com/v1/responses", 429, "error", {}, body)
                with patch.object(provider, "candidate_windows", return_value=[{}]), patch("openai_match.urlopen", side_effect=error):
                    with self.assertRaises(OpenAIMatchError) as caught:
                        provider.suggest({}, [])
                self.assertEqual(isinstance(caught.exception, OpenAIRateLimitError), code == "rate_limit_exceeded")
                self.assertIn(code, str(caught.exception))
                self.assertNotIn("private-key", str(caught.exception))
                self.assertTrue(body.closed)

    def test_retry_after_is_bounded_by_runner_not_truncated_by_parser(self):
        self.assertEqual(_retry_delay({"Retry-After": "130"}), 130)
        self.assertEqual(_retry_delay({"Retry-After": "invalid"}), 20)
        self.assertEqual(_retry_delay({"Retry-After": "nan"}), 20)
