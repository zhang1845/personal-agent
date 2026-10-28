"""Config and protocol tests use fake APIs exclusively, never real paid endpoints."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from personal_agent.api_model import APISettings, APIModelClient, base_url
from personal_agent.errors import AgentError
import test_webapp as helpers
from test_ollama import FakeOpener

SECRET='sk-unit-test-only-DO-NOT-USE'


def config(**kw):
    result={'base_url':'https://provider.example/v1','api_key':SECRET,'model':'vendor/model-name','enabled':True};result.update(kw);return result


def reply(content='OK', calls=None):
    message={'role':'assistant','content':content}
    if calls:message['tool_calls']=calls
    return {'choices':[{'message':message}]}


class APIConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=APISettings(Path(self.temp.name)/'model-api.json')
    def tearDown(self):self.temp.cleanup()
    def save(self,**kw):
        body=dict(config(),expected_version=self.store.get()['version']);body.update(kw);return self.store.save(body)

    def test_default_disabled_and_key_never_in_public_get_or_save(self):
        self.assertFalse(self.store.get()['enabled']);self.assertFalse(self.store.get()['has_key'])
        saved=self.save();self.assertTrue(saved['has_key']);self.assertNotIn(SECRET,json.dumps(saved));self.assertNotIn('api_key',saved)
        if os.name != "nt":
            self.assertEqual(0o600,self.store.path.stat().st_mode & 0o777)
        self.assertEqual(saved,APISettings(self.store.path).get())

    def test_key_blank_retains_explicit_clear_removes_and_address_change_requires_key(self):
        self.save();saved=self.save(api_key='',model='another/model');self.assertTrue(saved['has_key'])
        with self.assertRaises(AgentError):self.save(api_key='',base_url='https://different.example/v1')
        self.assertEqual('https://provider.example/v1',self.store.get()['base_url'])
        saved=self.save(api_key='',clear_key=True,base_url='https://different.example/v1');self.assertFalse(saved['has_key'])
        self.assertNotIn(SECRET,self.store.path.read_text())

    def test_bad_urls_and_header_injection_rejected_without_partial_write(self):
        for address in ('file:///etc/passwd','http://public.example/v1','https://user:pass@provider.example','https://provider.example?api_key=secret','https://provider.example/#key','https://provider.example\\@evil.example','https://provider.example:99999'):
            with self.subTest(address=address),self.assertRaises(AgentError):self.save(base_url=address)
        for key in (None,'secret\r\nAuthorization: other','a b','中文'):
            with self.subTest(key=key),self.assertRaises(AgentError):self.save(api_key=key)
        self.assertFalse(self.store.path.exists())

    def test_url_normalization_and_drafts(self):
        self.assertEqual('https://provider.example/v1',base_url(' https://provider.example/v1/chat/completions/ '))
        self.assertEqual('http://localhost:8899/v1',base_url('http://localhost:8899/v1/'))
        self.assertEqual('http://[::1]:8899',base_url('http://[::1]:8899'))
        self.assertFalse(self.save(base_url='',model='',enabled=False)['enabled'])
        with self.assertRaises(AgentError):self.save(base_url='',enabled=True)

    def test_version_conflict_and_test_result_invalidated_on_changes(self):
        saved=self.save();version=saved['version'];self.store.record_test(version,True,'ok')
        self.assertTrue(self.store.get()['last_test']['ok'])
        self.save(api_key='',model='changed/model',enabled=False)
        self.assertIsNone(self.store.get()['last_test'])
        with self.assertRaises(AgentError):self.store.record_test(version,True,'ok')
        with self.assertRaises(AgentError):self.save(expected_version=version)

    def test_enable_switch_preserves_test_for_unchanged_endpoint_model_and_key(self):
        saved=self.save(enabled=False);self.store.record_test(saved['version'],True,'ok')
        updated=self.save(api_key='',enabled=True)
        self.assertTrue(updated['last_test']['ok'])

    def test_corrupt_configuration_never_falls_back_to_enabled(self):
        self.store.path.write_text('{broken')
        with self.assertRaises(AgentError):self.store.get()

    def test_symlink_configuration_never_overwrites_target(self):
        other=self.store.path.parent/'other';other.write_text('do not overwrite')
        try:
            self.store.path.symlink_to(other)
        except OSError as exc:
            if os.name == 'nt' and getattr(exc, 'winerror', None) == 1314:
                self.skipTest('Windows symlinks require Developer Mode or elevated privileges')
            raise
        with self.assertRaises(AgentError):self.save()
        self.assertEqual('do not overwrite',other.read_text())


class APIClientTests(unittest.TestCase):
    def test_chat_protocol_exact_url_auth_and_configured_model(self):
        opener=FakeOpener([reply('你好')]);client=APIModelClient(config(),opener)
        self.assertEqual('你好',client.chat([{'role':'user','content':'你好'}],[],config()['model'])['content'])
        req,_=opener.requests[0];self.assertEqual('https://provider.example/v1/chat/completions',req.full_url);self.assertEqual('Bearer '+SECRET,req.get_header('Authorization'))
        body=json.loads(req.data);self.assertFalse(body['stream']);self.assertEqual(config()['model'],body['model']);self.assertNotIn('api_key',body)

    def test_tool_call_ids_and_json_arguments_survive_round_trip(self):
        messages=[{'role':'user','content':'查看任务'},{'role':'assistant','content':'','tool_calls':[{'id':'call-real-7','function':{'name':'assistant_list_tasks','arguments':{}}}]},{'role':'tool','tool_name':'assistant_list_tasks','content':'{"tasks":[]}'}]
        opener=FakeOpener([reply()]);APIModelClient(config(),opener).chat(messages,[],config()['model'])
        body=json.loads(opener.requests[0][0].data);self.assertEqual('{}',body['messages'][1]['tool_calls'][0]['function']['arguments']);self.assertEqual('call-real-7',body['messages'][2]['tool_call_id']);self.assertNotIn('tool_name',body['messages'][2])

    def test_test_request_is_short_and_excludes_personal_data_and_tools(self):
        opener=FakeOpener([reply()]);client=APIModelClient(config(enabled=False),opener);self.assertTrue(client.test()['ok'])
        body=json.loads(opener.requests[0][0].data);self.assertEqual([{'role':'user','content':'Reply OK.'}],body['messages']);self.assertEqual(16,body['max_tokens']);self.assertNotIn('tools',body)

    def test_disabled_or_wrong_model_makes_no_network_call(self):
        opener=FakeOpener([])
        for values,model in ((config(enabled=False),config()['model']),(config(),'different-model')):
            with self.assertRaises(AgentError):APIModelClient(values,opener).chat([{'role':'user','content':'hi'}],[],model)
        self.assertEqual([],opener.requests)

    def test_errors_redacted_and_no_retry_or_redirect(self):
        for error in (URLError(SECRET),HTTPError('https://provider.example',401,SECRET,{},None),HTTPError('https://provider.example',302,SECRET,{},None)):
            opener=FakeOpener([error])
            with self.assertRaises(AgentError) as ctx:APIModelClient(config(),opener).test()
            self.assertNotIn(SECRET,str(ctx.exception.as_dict()));self.assertEqual(1,len(opener.requests))

    def test_response_size_json_and_empty_response_validation(self):
        for value in (b'not json',{},reply(''),{'choices':[{'message':{'content':[], 'role':'assistant'}}]}):
            with self.subTest(value=value),self.assertRaises(AgentError):APIModelClient(config(),FakeOpener([value])).test()
        client=APIModelClient(config(),FakeOpener([b'x'*101]));client.MAX_RESPONSE=100
        with self.assertRaises(AgentError):client.test()


class APIHTTPTests(unittest.TestCase):
    setUp=helpers.WebappTests.setUp
    tearDown=helpers.WebappTests.tearDown
    request=helpers.WebappTests.request

    def save(self,enabled=False,**kw):
        _,_,current=self.request('GET','/api/model-api')
        body=dict(config(enabled=enabled),expected_version=current['settings']['version']);body.update(kw)
        status,_,saved=self.request('PUT','/api/model-api',body);self.assertEqual(200,status,saved)
        return saved['settings']

    def test_save_reload_is_private_and_never_calls_provider(self):
        with patch('personal_agent.api_model.APIModelClient.chat',side_effect=AssertionError('unexpected network')):
            saved=self.save();_,_,loaded=self.request('GET','/api/model-api');self.assertEqual(saved,loaded['settings']);self.assertNotIn(SECRET,json.dumps(loaded))
            status,_,chat=self.request('POST','/api/chat',{'message':'帮我分析这个没有材料的问题'});self.assertEqual(200,status);self.assertEqual('rules',chat['mode'])

    def test_test_endpoint_requires_token_and_only_tests_saved_revision(self):
        saved=self.save()
        status,_,_=self.request('POST','/api/model-api/test',{'expected_version':saved['version']},headers={'X-Agent-Token':None});self.assertEqual(403,status)
        with patch('personal_agent.api_model.APIModelClient.test',return_value={'ok':True,'message':'test response'}) as call:
            status,_,result=self.request('POST','/api/model-api/test',{'expected_version':saved['version']});self.assertEqual(200,status);call.assert_called_once();self.assertTrue(result['settings']['last_test']['ok'])
        status,_,_=self.request('POST','/api/model-api/test',{'expected_version':999});self.assertEqual(409,status)

    def test_enabled_api_routes_chat_to_client_and_keeps_writes_as_drafts(self):
        self.save(enabled=True)
        response={'role':'assistant','content':'','tool_calls':[{'id':'call-task','function':{'name':'assistant_add_task','arguments':'{"title":"核对 API 配置"}'}}]}
        with patch('personal_agent.api_model.APIModelClient.chat',return_value=response) as call:
            status,_,result=self.request('POST','/api/chat',{'message':'请帮我记一项工作，内容是核对 API 配置'})
            self.assertEqual(200,status,result);self.assertEqual('api',result['mode']);self.assertEqual(1,len(result['actions']));self.assertEqual(config()['model'],call.call_args.args[2])
        _,_,tasks=self.request('GET','/api/tasks');self.assertEqual([],tasks['tasks'])
        status,_,_=self.request('POST','/api/actions/'+result['actions'][0]['id']+'/apply',{});self.assertEqual(200,status)
        _,_,tasks=self.request('GET','/api/tasks');self.assertEqual('核对 API 配置',tasks['tasks'][0]['title'])

    def test_read_tool_conversation_uses_real_ids_in_followup_request(self):
        self.save(enabled=True)
        calls=[reply('',[{'id':'call-tasks-42','type':'function','function':{'name':'assistant_list_tasks','arguments':'{}'}}]),reply('暂无未完成事项。')]
        opener=FakeOpener(calls)
        with patch('personal_agent.api_model.build_opener',return_value=opener):
            status,_,result=self.request('POST','/api/chat',{'message':'请检查有没有未完成的工作事项'})
        self.assertEqual(200,status,result);self.assertEqual('api',result['mode']);self.assertEqual(2,len(opener.requests))
        body=json.loads(opener.requests[1][0].data);self.assertEqual('call-tasks-42',body['messages'][-1]['tool_call_id'])

    def test_enabled_api_failure_never_uses_local_or_another_provider(self):
        self.save(enabled=True)
        with patch('personal_agent.api_model.APIModelClient.chat',side_effect=AgentError('model_unavailable','API 不可用')) as call:
            status,_,result=self.request('POST','/api/chat',{'message':'帮我分析当前进展'})
        self.assertEqual(200,status);self.assertEqual('api',result['mode']);self.assertIn('API 不可用',result['reply']);call.assert_called_once()
        status,_,_=self.request('PUT','/api/model-api',dict(config(),expected_version=2,unexpected='bad'));self.assertEqual(400,status)

if __name__=='__main__':unittest.main()
