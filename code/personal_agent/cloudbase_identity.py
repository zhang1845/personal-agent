"""Optional CloudBase runtime identity; never reads CLI credentials or local files.

The request follows the official CLI's rawFetchAccessToken flow and was checked
against @cloudbase/signature-nodejs 2.2.2 with offline, invented credentials.
This exchanges an injected temporary identity for a short-lived Bearer token;
PostgreSQL still has to accept it through the existing service_role RPC gate.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_BODY = b'{"grant_type":"client_credentials"}'
_PATH = '/auth/v1/token/clientCredential'
_MAX_RESPONSE = 64 * 1024
_UNAVAILABLE = '云端身份暂不可用，请稍后重试。'
_CREDENTIAL_KEYS = ('TENCENTCLOUD_SECRETID', 'TENCENTCLOUD_SECRETKEY',
                    'TENCENTCLOUD_SESSIONTOKEN')


def _environment_credentials():
    # Read only when bearer() is called. Importing this module is side-effect free.
    return {name: os.environ.get(name) for name in _CREDENTIAL_KEYS}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _authorization(host, credentials, timestamp):
    secret_id, secret_key, session_token = credentials
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d')
    canonical_headers = f'content-type:application/json\nhost:{host}\n'
    signed_headers = 'content-type;host'
    canonical = (f'POST\n{_PATH}\n\n{canonical_headers}\n{signed_headers}\n'
                 + hashlib.sha256(_BODY).hexdigest())
    scope = f'{date}/tcb/tc3_request'
    to_sign = (f'TC3-HMAC-SHA256\n{timestamp}\n{scope}\n'
               + hashlib.sha256(canonical.encode('utf-8')).hexdigest())

    def digest(key, value):
        return hmac.new(key, value.encode('utf-8'), hashlib.sha256).digest()

    signing_key = digest(digest(digest(('TC3' + secret_key).encode('utf-8'), date),
                                'tcb'), 'tc3_request')
    signature = hmac.new(signing_key, to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    return (f'TC3-HMAC-SHA256 Credential={secret_id}/{scope}, '
            f'SignedHeaders={signed_headers}, Signature={signature}, '
            f'Timestamp={timestamp}, Token={session_token}')


class CloudBaseIdentity:
    """Obtain runtime Bearer tokens, failing closed on absent/expired credentials.

The reader supplies the three TENCENTCLOUD_* temporary-credential variables on
every call, including cache hits. An opaque session token's expiry is checked by
the provider; missing credentials never fall back to a previously cached token.
Errors deliberately omit provider bodies, URLs, credentials and exception causes.
"""
    def __init__(self, env_id, credential_reader=_environment_credentials, *,
                 opener=None, timeout=30, clock=time.time):
        if (not isinstance(env_id, str)
                or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', env_id)):
            raise ValueError('CloudBase 环境名称无效')
        if (not callable(credential_reader) or not callable(clock)
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('CloudBase 身份配置无效')
        self.endpoint = f'https://{env_id}.api.tcloudbasegateway.com{_PATH}'
        self._host = f'{env_id}.api.tcloudbasegateway.com'
        self._reader = credential_reader
        self._clock = clock
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self._timeout = timeout
        self._lock = threading.RLock()
        self._clear()

    def _clear(self):
        self._fingerprint = None
        self._token = None
        self._issued_at = 0
        self._expires_at = 0

    def _now(self):
        now = self._clock()
        if (isinstance(now, bool) or not isinstance(now, (int, float))
                or not math.isfinite(now) or now < 1):
            raise ValueError()
        return now

    def _credentials(self):
        supplied = self._reader()
        if not isinstance(supplied, Mapping):
            raise ValueError()
        credentials = tuple(supplied.get(name) for name in _CREDENTIAL_KEYS)
        for value in credentials:
            if (not isinstance(value, str) or not 1 <= len(value) <= 16384
                    or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in value)):
                raise ValueError()
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', credentials[0])
                or ',' in credentials[2]):
            raise ValueError()
        return credentials

    def bearer(self):
        with self._lock:
            try:
                credentials = self._credentials()
                # Keep only a digest between calls, not the raw temporary key tuple.
                fingerprint = hashlib.sha256(json.dumps(credentials).encode('utf-8')).digest()
                now = self._now()
                if (fingerprint == self._fingerprint and self._token is not None
                        and self._issued_at <= now < self._expires_at):
                    return self._token
                self._clear()
                request = Request(self.endpoint, data=_BODY, method='POST', headers={
                    'Host': self._host,
                    'Content-Type': 'application/json',
                    'Authorization': _authorization(self._host, credentials, int(now) - 1),
                })
                with self._opener.open(request, timeout=self._timeout) as response:
                    if response.status != 200 or response.geturl() != self.endpoint:
                        raise ValueError()
                    raw = response.read(_MAX_RESPONSE + 1)
                if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE:
                    raise ValueError()
                result = json.loads(raw.decode('utf-8'))
                token = result.get('access_token') if isinstance(result, dict) else None
                expires_in = result.get('expires_in') if isinstance(result, dict) else None
                if (not isinstance(token, str) or not 16 <= len(token) <= 8192
                        or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in token)
                        or type(expires_in) is not int or expires_in <= 60
                        or result.get('token_type', 'Bearer') != 'Bearer'):
                    raise ValueError()
                expires_at = now + min(expires_in - 60, 600)
                received_at = self._now()
                if not now <= received_at < expires_at:
                    raise ValueError()
                self._fingerprint = fingerprint
                self._token = token
                self._issued_at = now
                self._expires_at = expires_at
                return token
            except HTTPError as error:
                try:
                    error.close()
                except Exception:
                    pass
                self._clear()
                raise sqlite3.OperationalError(_UNAVAILABLE) from None
            except Exception:
                self._clear()
                raise sqlite3.OperationalError(_UNAVAILABLE) from None
