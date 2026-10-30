from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from personal_agent.cloudbase_identity import CloudBaseIdentity, _NoRedirect


FAKE_CREDENTIALS = {
    'TENCENTCLOUD_SECRETID': 'FAKE_SECRET_ID_OFFLINE_ONLY',
    'TENCENTCLOUD_SECRETKEY': 'FAKE_SECRET_KEY_OFFLINE_ONLY',
    'TENCENTCLOUD_SESSIONTOKEN': 'FAKE_SESSION_TOKEN_OFFLINE_ONLY',
}
FAKE_TOKEN = 'FAKE_ACCESS_TOKEN_OFFLINE_ONLY'
ENDPOINT = ('https://fake-env-offline.api.tcloudbasegateway.com'
            '/auth/v1/token/clientCredential')


class FakeResponse:
    def __init__(self, raw, status=200, url=ENDPOINT):
        self.raw, self.status, self.url = raw, status, url
        self.limits = []
        self.closed = False

    def geturl(self):
        return self.url

    def read(self, limit):
        self.limits.append(limit)
        return self.raw[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class FakeOpener:
    def __init__(self):
        self.payload = {'token_type': 'Bearer', 'access_token': FAKE_TOKEN, 'expires_in': 7200}
        self.raw = None
        self.status = 200
        self.url = ENDPOINT
        self.error = None
        self.calls = []
        self.responses = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        raw = self.raw if self.raw is not None else json.dumps(self.payload).encode('utf-8')
        response = FakeResponse(raw, self.status, self.url)
        self.responses.append(response)
        return response


class CloudBaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.credentials = dict(FAKE_CREDENTIALS)
        self.reader = Mock(side_effect=lambda: dict(self.credentials))
        # The official CLI subtracts one second before signing.
        self.now = 1735689601
        self.opener = FakeOpener()
        self.identity = CloudBaseIdentity('fake-env-offline', self.reader,
                                         opener=self.opener, clock=lambda: self.now)

    def assertUnavailable(self, identity=None):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            (identity or self.identity).bearer()
        self.assertEqual(str(caught.exception), '云端身份暂不可用，请稍后重试。')
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        return caught.exception

    def test_exact_official_signature_nodejs_222_offline_vector(self):
        # Produced by the public official @cloudbase/signature-nodejs 2.2.2,
        # with isCloudApi=true, withSignedParams=false; no real key was used.
        self.assertEqual(self.identity.bearer(), FAKE_TOKEN)
        request, timeout = self.opener.calls[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.data, b'{"grant_type":"client_credentials"}')
        self.assertEqual(request.get_header('Host'), 'fake-env-offline.api.tcloudbasegateway.com')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(request.get_header('Authorization'),
                         'TC3-HMAC-SHA256 Credential=FAKE_SECRET_ID_OFFLINE_ONLY/'
                         '2025-01-01/tcb/tc3_request, SignedHeaders=content-type;host, '
                         'Signature=1d9d265f2ad6e3144d03488d9a00eab567a61c692519429b01a686a7833a8dd5, '
                         'Timestamp=1735689600, Token=FAKE_SESSION_TOKEN_OFFLINE_ONLY')
        self.assertEqual(timeout, 30)

    def test_constructor_does_not_read_environment_or_identity(self):
        self.reader.assert_not_called()
        self.assertEqual(self.opener.calls, [])

    def test_cache_reads_current_credentials_every_call_and_caps_at_600_seconds(self):
        self.identity.bearer()
        self.now += 599
        self.assertEqual(self.identity.bearer(), FAKE_TOKEN)
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(len(self.opener.calls), 1)
        self.now += 1
        self.identity.bearer()
        self.assertEqual(len(self.opener.calls), 2)

    def test_cache_expires_sixty_seconds_before_server_expiry(self):
        self.opener.payload['expires_in'] = 120
        self.identity.bearer()
        self.now += 59
        self.identity.bearer()
        self.assertEqual(len(self.opener.calls), 1)
        self.now += 1
        self.identity.bearer()
        self.assertEqual(len(self.opener.calls), 2)

    def test_each_credential_rotation_forces_new_signature_and_token(self):
        self.identity.bearer()
        for index, key in enumerate(FAKE_CREDENTIALS, 1):
            self.credentials[key] += '_ROTATED'
            expected = FAKE_TOKEN + str(index)
            self.opener.payload['access_token'] = expected
            self.assertEqual(self.identity.bearer(), expected)
            self.assertEqual(len(self.opener.calls), index + 1)
        self.assertIn('Token=FAKE_SESSION_TOKEN_OFFLINE_ONLY_ROTATED',
                      self.opener.calls[-1][0].get_header('Authorization'))

    def test_missing_or_permanent_credentials_invalidate_previous_cache(self):
        self.identity.bearer()
        for key in FAKE_CREDENTIALS:
            original = self.credentials.pop(key)
            self.assertUnavailable()
            self.assertEqual(len(self.opener.calls), 1)
            self.credentials[key] = original
        self.identity.bearer()
        self.assertEqual(len(self.opener.calls), 2)

    def test_reader_failure_clears_cache_and_never_reads_files(self):
        self.identity.bearer()
        self.reader.side_effect = RuntimeError('FAKE_PRIVATE_DETAILS')
        with patch('builtins.open', side_effect=AssertionError('local file read')):
            self.assertUnavailable()
        self.reader.side_effect = lambda: dict(self.credentials)
        self.identity.bearer()
        self.assertEqual(len(self.opener.calls), 2)

    def test_expired_runtime_credential_rejection_never_reuses_old_token(self):
        self.identity.bearer()
        self.credentials['TENCENTCLOUD_SESSIONTOKEN'] += '_EXPIRED'
        body = io.BytesIO(b'FAKE_SECRET_PROVIDER_BODY')
        self.opener.error = HTTPError(ENDPOINT, 401, 'FAKE_PRIVATE_DETAILS', {}, body)
        self.assertUnavailable()
        self.assertTrue(body.closed)
        self.assertEqual(len(self.opener.calls), 2)
        self.assertUnavailable()
        self.assertEqual(len(self.opener.calls), 3)

    def test_network_and_unexpected_errors_are_sanitized_without_retries(self):
        for error in (URLError('FAKE_PRIVATE_DETAILS'), TimeoutError('FAKE_PRIVATE_DETAILS'),
                      OSError('FAKE_PRIVATE_DETAILS'), RuntimeError('FAKE_PRIVATE_DETAILS')):
            with self.subTest(kind=type(error).__name__):
                self.opener.error = error
                before = len(self.opener.calls)
                self.assertUnavailable()
                self.assertEqual(len(self.opener.calls), before + 1)

    def test_error_cleanup_failure_is_also_sanitized(self):
        error = HTTPError(ENDPOINT, 401, 'FAKE_PRIVATE_DETAILS', {}, io.BytesIO(b'private'))
        self.opener.error = error
        with patch.object(error, 'close', side_effect=RuntimeError('FAKE_PRIVATE_DETAILS')):
            self.assertUnavailable()
        error.close()

    def test_redirect_handler_rejects_all_redirects_and_response_origin_changes(self):
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(_NoRedirect().redirect_request(None, None, code, '', {},
                                                            'https://attacker.invalid/'))
        self.opener.url = 'https://attacker.invalid/'
        self.assertUnavailable()
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.opener.responses[0].limits, [])
        self.assertTrue(self.opener.responses[0].closed)

    def test_http_redirect_and_other_status_codes_are_not_success(self):
        for status in (201, 302, 403, 500):
            with self.subTest(status=status):
                self.opener.status = status
                self.assertUnavailable()
                self.assertEqual(self.opener.responses[-1].limits, [])

    def test_json_is_size_limited_and_invalid_payloads_fail_closed(self):
        for raw in (b'x' * (64 * 1024 + 1), b'not-json', b'\xff', b'[]', b'null'):
            with self.subTest(raw_size=len(raw)):
                self.opener.raw = raw
                self.assertUnavailable()
                self.assertEqual(self.opener.responses[-1].limits, [64 * 1024 + 1])

    def test_short_invalid_and_noninteger_expiries_fail_closed(self):
        for expiry in (None, True, False, -1, 0, 60, 59, 7200.0, '7200', float('nan')):
            with self.subTest(expiry=expiry):
                self.opener.payload['expires_in'] = expiry
                self.assertUnavailable()

    def test_invalid_token_and_type_fail_closed(self):
        for token in (None, '', 'short', 'x' * 8193, FAKE_TOKEN + '\r\nInjected: secret',
                      '秘密' + FAKE_TOKEN):
            with self.subTest(token_type=type(token).__name__):
                self.opener.payload['access_token'] = token
                self.assertUnavailable()
        self.opener.payload['access_token'] = FAKE_TOKEN
        self.opener.payload['token_type'] = 'Basic'
        self.assertUnavailable()

    def test_invalid_credentials_fail_before_network(self):
        for key in FAKE_CREDENTIALS:
            for value in (None, '', 'bad\r\nheader', '秘密', 'x' * 16385):
                with self.subTest(key=key, value_type=type(value).__name__):
                    self.credentials = dict(FAKE_CREDENTIALS)
                    self.credentials[key] = value
                    self.assertUnavailable()
        self.assertEqual(self.opener.calls, [])

    def test_long_request_does_not_extend_expiry_and_clock_regression_clears_cache(self):
        self.opener.payload['expires_in'] = 120
        instants = iter((1735689601, 1735689661))
        identity = CloudBaseIdentity('fake-env-offline', self.reader, opener=self.opener,
                                     clock=lambda: next(instants))
        self.assertUnavailable(identity)
        self.opener.payload['expires_in'] = 7200
        self.identity.bearer()
        self.now -= 10
        self.identity.bearer()
        self.assertEqual(len(self.opener.calls), 3)

    def test_concurrent_calls_share_one_token_exchange_but_each_reads_credentials(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            tokens = list(executor.map(lambda _: self.identity.bearer(), range(16)))
        self.assertEqual(tokens, [FAKE_TOKEN] * 16)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.reader.call_count, 16)

    def test_default_reader_reads_only_current_runtime_environment_when_called(self):
        with patch.dict('os.environ', FAKE_CREDENTIALS, clear=True):
            identity = CloudBaseIdentity('fake-env-offline', opener=self.opener,
                                         clock=lambda: self.now)
            self.assertEqual(identity.bearer(), FAKE_TOKEN)
        with patch.dict('os.environ', {}, clear=True):
            self.assertUnavailable(identity)
        self.assertEqual(len(self.opener.calls), 1)

    def test_invalid_constructor_parameters_do_not_request_identity(self):
        for env in ('', 'https://host', 'wrong/env', 'fake\r\nheader', 'X', None):
            with self.assertRaises(ValueError):
                CloudBaseIdentity(env, self.reader, opener=self.opener)
        for timeout in (True, 0, -1, float('inf'), '30'):
            with self.assertRaises(ValueError):
                CloudBaseIdentity('fake-env-offline', self.reader, timeout=timeout)
        self.reader.assert_not_called()
        self.assertEqual(self.opener.calls, [])


if __name__ == '__main__':
    unittest.main()
