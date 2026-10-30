"""Cloud auth and actual workbench tools, using only isolated fake credentials."""
from io import BytesIO
import http.client
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode

import test_webapp as web_helpers
from personal_agent.cloud import AuthStore, CloudConfig, CloudGateway, password_hash, COOKIE
from cloud_admin import read_config, write_config

PASSWORD = 'daynest-unit-test-only-2026'
HASH = password_hash(PASSWORD)


class CloudConfigTests(unittest.TestCase):
    def values(self, **changes):
        values = {'DAYNEST_PUBLIC_URL': 'https://Daynest.Example:443/',
                  'DAYNEST_DATA_DIR': str(Path(tempfile.gettempdir())/'云端 数据'), 'DAYNEST_PASSWORD_HASH': HASH}
        values.update(changes)
        return values

    def test_normalizes_https_and_requires_persistent_absolute_directory(self):
        config = CloudConfig.from_env(self.values())
        self.assertEqual('https://daynest.example', config.origin)
        for url in ('http://example.com', 'https://a.example/path', 'https://a.example?q=1',
                    'https://user:password@a.example', 'https://a.example/#x', 'https://a%0d.example', 'https://a.example:bad'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                CloudConfig.from_env(self.values(DAYNEST_PUBLIC_URL=url))
        for path in ('', 'relative/data'):
            with self.assertRaises(ValueError):
                CloudConfig.from_env(self.values(DAYNEST_DATA_DIR=path))
        for value in ('', 'password', 'pbkdf2_sha256$1$salt$hash'):
            with self.assertRaises(ValueError):
                CloudConfig.from_env(self.values(DAYNEST_PASSWORD_HASH=value))

    def test_private_config_roundtrips_unicode_paths_and_never_contains_plain_password(self):
        with tempfile.TemporaryDirectory(prefix='云端 配置 ') as folder:
            path = Path(folder)/'private/cloud.env'
            data = Path(folder)/"O'Neill 数据"
            write_config(path, 'https://daynest.example', data, PASSWORD)
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn(PASSWORD, raw)
            self.assertNotIn('API_KEY', raw)
            self.assertEqual(data, CloudConfig.from_env(read_config(path)).data_dir)
            with self.assertRaises(FileExistsError):
                write_config(path, 'https://daynest.example', data, PASSWORD)
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).parents[1]/'run_cloud.py'),
                '--env-file', str(path), '--check'], capture_output=True, text=True, encoding='utf-8', timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn(PASSWORD, result.stdout + result.stderr)
            self.assertNotIn(HASH, result.stdout + result.stderr)
            self.assertFalse(data.exists())

    def test_invalid_or_duplicate_env_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'cloud.env'
            for content in ('MODEL_API_KEY="not-allowed"', 'DAYNEST_PUBLIC_URL="https://a.example"\nDAYNEST_PUBLIC_URL="https://b.example"'):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError): read_config(path)


class CloudAuthTests(unittest.TestCase):
    def test_session_is_hashed_expires_and_is_revoked_by_password_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = AuthStore(folder, HASH)
            state, token = auth.login(PASSWORD)
            self.assertEqual('ok', state); self.assertTrue(auth.authenticated(token))
            self.assertFalse(auth.authenticated('bad-token'))
            with auth.connection() as c:
                stored = c.execute('SELECT token FROM sessions').fetchone()[0]
            self.assertNotEqual(token, stored)
            self.assertFalse(AuthStore(folder, password_hash(PASSWORD+'new')).authenticated(token))
            with auth.connection() as c:c.execute('UPDATE sessions SET expires=?', (time.time()-1,))
            self.assertFalse(auth.authenticated(token))

    def test_login_attempt_limit_is_persistent_and_cannot_be_bypassed_with_ip_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = AuthStore(folder, HASH)
            for _ in range(10):self.assertEqual('invalid', auth.login('wrong')[0])
            self.assertEqual('limited', AuthStore(folder, HASH).login(PASSWORD)[0])
            with auth.connection() as c:c.execute('UPDATE failures SET at=?', (time.time()-601,))
            self.assertEqual('ok', auth.login(PASSWORD)[0])


class CloudGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='daynest 云端 ')
        self.folder = Path(self.temp.name)
        self.config = CloudConfig.from_env({'DAYNEST_PUBLIC_URL':'https://daynest.example',
            'DAYNEST_DATA_DIR':str(self.folder/'data'), 'DAYNEST_PASSWORD_HASH':HASH})
        self.cookie = ''
        self.gateway = CloudGateway(self.config, model_client=web_helpers.OfflineModel(), scheduler=False)

    def tearDown(self):
        self.gateway.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, headers=None, raw=None, cookie=True):
        payload = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else b''
        environ = {'REQUEST_METHOD':method, 'PATH_INFO':path, 'QUERY_STRING':'',
            'HTTP_HOST':'daynest.example', 'CONTENT_LENGTH':str(len(payload)), 'wsgi.input':BytesIO(payload),
            'CONTENT_TYPE':'application/json'}
        if method not in ('GET','HEAD'):environ['HTTP_ORIGIN']=self.config.origin
        if cookie and self.cookie:environ['HTTP_COOKIE']=self.cookie
        environ.update(headers or {})
        captured = {}
        def start(status, response_headers):captured.update(status=int(status.split()[0]),headers=dict(response_headers))
        value = b''.join(self.gateway(environ, start))
        if value and 'application/json' in captured['headers'].get('Content-Type',''):value=json.loads(value)
        return captured['status'],captured['headers'],value

    def login(self):
        code, headers, _ = self.request('POST','/login',raw=urlencode({'password':PASSWORD}).encode(),
            headers={'CONTENT_TYPE':'application/x-www-form-urlencoded'})
        self.assertEqual(303,code)
        self.cookie=headers['Set-Cookie'].split(';')[0]
        _,_,bootstrap=self.request('GET','/api/bootstrap')
        self.csrf=bootstrap['token']
        return headers

    def mutate(self, method, path, body):
        return self.request(method,path,body,headers={'HTTP_X_AGENT_TOKEN':self.csrf})

    def test_anonymous_cannot_read_data_or_bootstrap_and_health_discloses_no_details(self):
        for path in ('/api/bootstrap','/api/tasks','/api/quality','/api/model-api','/api/runtime'):
            code,_,value=self.request('GET',path)
            self.assertEqual(401,code);self.assertEqual('authentication_required',value['error']['code'])
        self.assertEqual('/login',self.request('GET','/')[1]['Location'])
        self.assertEqual({'ok':True},self.request('GET','/healthz',headers={'HTTP_HOST':'127.0.0.1'})[2])
        self.assertEqual(200,self.request('GET','/login')[0])
        self.assertEqual(200,self.request('GET','/login.css')[0])

    def test_secure_cookie_bootstrap_and_logout_replay(self):
        headers=self.login()
        for flag in ('Secure','HttpOnly','SameSite=Strict','Path=/'):self.assertIn(flag,headers['Set-Cookie'])
        self.assertTrue(self.cookie.startswith(COOKIE+'='))
        code,_,value=self.request('GET','/api/bootstrap')
        self.assertEqual(200,code);self.assertEqual('cloud',value['deployment']['mode'])
        self.assertEqual(405,self.request('GET','/logout')[0])
        code,headers,_=self.request('POST','/logout')
        self.assertEqual(303,code);self.assertIn('Max-Age=0',headers['Set-Cookie'])
        self.assertEqual(401,self.request('GET','/api/tasks')[0])

    def test_origin_host_and_original_csrf_checks_remain_effective(self):
        self.login()
        self.assertEqual(403,self.request('POST','/api/tasks',{'title':'blocked'})[0])
        self.assertEqual(403,self.request('POST','/api/tasks',{'title':'blocked'},headers={
            'HTTP_ORIGIN':'https://evil.example','HTTP_X_AGENT_TOKEN':self.csrf,'HTTP_X_FORWARDED_HOST':'daynest.example'})[0])
        self.assertEqual(400,self.request('GET','/api/tasks',headers={'HTTP_HOST':'evil.example','HTTP_X_FORWARDED_HOST':'daynest.example'})[0])
        self.assertEqual(403,self.request('POST','/login',headers={'HTTP_ORIGIN':''})[0])
        self.assertEqual([],self.request('GET','/api/tasks')[2]['tasks'])

    def test_chat_drafts_calendar_sources_and_api_key_redaction(self):
        self.login()
        code,_,reply=self.mutate('POST','/api/chat',{'message':'待办：云端验收任务'})
        self.assertEqual(200,code)
        self.assertEqual([],self.request('GET','/api/tasks')[2]['tasks'])
        self.assertEqual(200,self.mutate('POST','/api/actions/'+reply['actions'][0]['id']+'/apply',{})[0])
        self.assertEqual('云端验收任务',self.request('GET','/api/tasks')[2]['tasks'][0]['title'])
        self.assertEqual(200,self.mutate('POST','/api/events',web_helpers.WebappTests.create_arguments())[0])
        code,_,result=self.mutate('POST','/api/knowledge/projects',{'name':'云端验收','background':''})
        self.assertEqual(200,code);project=result['project']['id']
        self.assertEqual(200,self.mutate('POST','/api/knowledge/sources',{'title':'云端资料','kind':'material','project_id':project,'content':'原文需要保存来源。'})[0])
        canary='cloud-test-key-never-a-real-provider'
        code,_,saved=self.mutate('PUT','/api/model-api',{'base_url':'https://provider.invalid/v1','model':'test-model','api_key':canary,'enabled':False,'expected_version':1})
        self.assertEqual(200,code)
        self.assertNotIn(canary,json.dumps(saved))
        self.assertNotIn(canary,json.dumps(self.request('GET','/api/model-api')[2]))

    def test_restart_keeps_data_and_session_and_duplicate_instance_is_rejected(self):
        self.login();self.mutate('POST','/api/tasks',{'title':'重启后保留'})
        with self.assertRaisesRegex(ValueError,'已有云端进程'):
            CloudGateway(self.config,model_client=web_helpers.OfflineModel(),scheduler=False)
        self.gateway.close()
        self.gateway=CloudGateway(self.config,model_client=web_helpers.OfflineModel(),scheduler=False)
        self.assertEqual('重启后保留',self.request('GET','/api/tasks')[2]['tasks'][0]['title'])
        # A new backend CSRF token must still be obtained after a process restart.
        self.assertEqual(403,self.mutate('POST','/api/tasks',{'title':'stale'})[0])

    def test_cloud_cannot_stop_backend_or_accept_oversized_requests(self):
        self.login()
        self.assertEqual(404,self.mutate('POST','/api/service/stop',{})[0])
        self.assertEqual(413,self.request('POST','/api/tasks',headers={'CONTENT_LENGTH':str(13*1024*1024)})[0])
        self.assertEqual(200,self.request('GET','/api/bootstrap')[0])


@unittest.skipUnless(importlib.util.find_spec('waitress'), '可选云端 HTTP 测试需要 requirements-cloud.txt；CI 必装')
class CloudHTTPTests(unittest.TestCase):
    def setUp(self):
        from waitress import create_server
        self.temp=tempfile.TemporaryDirectory(prefix='云端 HTTP ')
        config=CloudConfig.from_env({'DAYNEST_PUBLIC_URL':'https://daynest.example',
            'DAYNEST_DATA_DIR':self.temp.name,'DAYNEST_PASSWORD_HASH':HASH})
        self.gateway=CloudGateway(config,model_client=web_helpers.OfflineModel(),scheduler=False)
        self.front=create_server(self.gateway,host='127.0.0.1',port=0,asyncore_loop_timeout=.05)
        self.thread=threading.Thread(target=self.front.run,daemon=True);self.thread.start()

    def tearDown(self):
        self.front.close();self.front.task_dispatcher.shutdown()
        self.thread.join(timeout=3)
        self.gateway.close();self.temp.cleanup()
        self.assertFalse(self.thread.is_alive())

    def request(self,method,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',int(self.front.effective_port),timeout=5)
        supplied={'Host':'daynest.example','Connection':'close'};supplied.update(headers or {})
        try:
            connection.request(method,path,body=body,headers=supplied)
            response=connection.getresponse();value=response.read();response_headers=dict(response.getheaders())
            if value and 'application/json' in response_headers.get('Content-Type',''):value=json.loads(value)
            return response.status,response_headers,value
        finally:connection.close()

    def test_real_http_login_and_existing_write_confirmation_boundary(self):
        self.assertEqual(401,self.request('GET','/api/bootstrap')[0])
        code,headers,_=self.request('POST','/login',urlencode({'password':PASSWORD}),
            {'Origin':'https://daynest.example','Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(303,code);cookie=headers['Set-Cookie'].split(';')[0]
        code,_,bootstrap=self.request('GET','/api/bootstrap',headers={'Cookie':cookie})
        self.assertEqual(200,code)
        headers={'Cookie':cookie,'Origin':'https://daynest.example','Content-Type':'application/json','X-Agent-Token':bootstrap['token']}
        self.assertEqual(200,self.request('POST','/api/tasks',json.dumps({'title':'HTTP 云端任务'}),headers)[0])
        self.assertEqual('HTTP 云端任务',self.request('GET','/api/tasks',headers={'Cookie':cookie})[2]['tasks'][0]['title'])
        headers['Origin']='https://evil.example'
        self.assertEqual(403,self.request('POST','/api/tasks',json.dumps({'title':'blocked'}),headers)[0])

    def test_real_http_headers_and_wrong_password_do_not_return_credentials(self):
        code,headers,body=self.request('POST','/login',urlencode({'password':'wrong'}),
            {'Origin':'https://daynest.example','Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(401,code)
        self.assertIn('frame-ancestors',headers['Content-Security-Policy'])
        # no-referrer turns browser navigation/form POST Origin into null.
        self.assertEqual('same-origin',headers['Referrer-Policy'])
        self.assertNotIn(PASSWORD,body.decode());self.assertNotIn(HASH,body.decode())
        self.assertEqual(400,self.request('GET','/login',headers={'Host':'evil.example','X-Forwarded-Host':'daynest.example'})[0])


if __name__=='__main__':unittest.main()
