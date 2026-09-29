import os
import unittest
from unittest.mock import patch

import openai_settings
from openai_match import OpenAIMatchProvider
import test_backend


class OpenAISettingsTests(unittest.TestCase):
    request = test_backend.BackendApiSmokeTests.request

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        openai_settings.clear()

    def tearDown(self):
        openai_settings.clear()
        self.env.stop()

    def test_session_key_provider_and_redacted_status(self):
        key = 'sk-test-fixture-only'
        code, _, data, _ = self.request('POST', '/api/settings/openai',
                                       {'api_key': key, 'model': 'test-model'})
        self.assertEqual(code, 200)
        self.assertEqual(data, {'configured': True, 'source': 'session', 'model': 'test-model'})
        self.assertNotIn(key, str(data))
        self.assertEqual(OpenAIMatchProvider().api_key, key)
        self.request('POST', '/api/settings/openai', {'api_key': '', 'model': 'another-model'})
        self.assertEqual(OpenAIMatchProvider().api_key, key)
        self.assertEqual(OpenAIMatchProvider().model, 'another-model')
        self.request('DELETE', '/api/settings/openai')
        self.assertFalse(openai_settings.status()['configured'])

    def test_environment_fallback_and_validation(self):
        os.environ['OPENAI_API_KEY'] = 'env-test-only'
        openai_settings.configure({'api_key': 'session-test-only'})
        self.assertEqual(openai_settings.clear()['source'], 'environment')
        for payload in ({'api_key': []}, {'api_key': 'bad\nkey'}, {'model': '<script>'}):
            code, _, _, _ = self.request('POST', '/api/settings/openai', payload)
            self.assertEqual(code, 400)
        self.assertEqual(OpenAIMatchProvider().api_key, 'env-test-only')

    def test_cross_origin_cannot_change_credentials(self):
        code, _, _, _ = self.request('POST', '/api/settings/openai',
                                     {'api_key': 'test-secret'}, {'Origin': 'https://example.com'})
        self.assertEqual(code, 403)
        self.assertFalse(openai_settings.status()['configured'])
