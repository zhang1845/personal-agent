"""Actual nanobot loop with fake provider responses and isolated import services."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from personal_agent.webapp import Application
from personal_agent.api_model import APIModelClient


def call(name,args,identifier):
    return {'content':'','tool_calls':[{'id':identifier,'function':{'name':name,'arguments':args}}]}


class ChatSDKWorkflows(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=Application(Path(self.temp.name))
        self.app.api_settings.save({'base_url':'https://model.example/v1','model':'mock-workflow','enabled':True,'expected_version':1})
    def tearDown(self):self.app.close();self.temp.cleanup()

    def test_sdk_cancel_ordinary_event_with_occurrence_stays_pending_until_apply(self):
        event = self.app.calendar.call('calendar_create_event', {
            'request_id':'cancel-regression-create','title':'取消验收',
            'start':'2030-01-07T10:00:00+08:00','end':'2030-01-07T10:30:00+08:00'})['event']
        requests = []
        def provider(client, messages, tools, model):
            requests.append(messages)
            if len(requests) == 1:
                return call('calendar_get_event', {'event_id':event['id']}, 'read-event')
            if len(requests) == 2:
                return call('calendar_delete_event', {'request_id':'model-cancel', 'event_id':event['id'],
                    'expected_version':event['version'],'scope':'occurrence'}, 'cancel-event')
            return {'content':'请确认取消这条普通日程。'}
        with patch.object(APIModelClient, 'chat', new=provider):
            value = self.app.route('POST', '/api/chat', {}, {
                'message':'请先读取取消验收的日程，然后生成取消草案'})
        self.assertEqual('nanobot', value['mode'])
        self.assertEqual(1, len(value['actions']))
        action = value['actions'][0]
        self.assertEqual('pending', action['status'])
        self.assertEqual('occurrence', action['arguments']['scope'])
        self.assertEqual(event, self.app.calendar.call('calendar_get_event', {'event_id':event['id']})['event'])
        first = self.app.apply(action['id'])
        self.assertEqual('applied', first['action']['status'])
        self.assertTrue(first['result']['deleted'])
        self.assertEqual(first, self.app.apply(action['id']))
        self.assertEqual([], self.app.calendar.call('calendar_list_events', {'start':'2030-01-07','end':'2030-01-08'})['events'])

    def test_sdk_reads_user_link_and_proposes_original_preview_without_auto_save(self):
        html=('<html><head><title>来源原文</title></head><body><article>'+''.join('<p>项目资料需要保留原文和来源，后续检索可以引用。</p>' for _ in range(20))+'</article></body></html>').encode()
        self.app.importer.fetcher=lambda url:(html,url)
        requests=[]
        def provider(client,messages,tools,model):
            requests.append(messages)
            if len(requests)==1:return call('assistant_preview_link',{'url':'https://example.com/source'},'read-source')
            if len(requests)==2:
                values=[json.loads(m['content']) for m in messages if m.get('role')=='tool' and m.get('content','').startswith('{')]
                preview=next(value['preview'] for value in values if 'preview' in value)
                return call('assistant_save_import',{'preview_id':preview['preview_id']},'save-source')
            return {'content':'请核对原文后确认归档。'}
        with patch.object(APIModelClient,'chat',new=provider):
            value=self.app.route('POST','/api/chat',{}, {'message':'读取 https://example.com/source 然后保存到资料库'})
        self.assertEqual('nanobot',value['mode']);self.assertEqual(3,len(requests))
        self.assertEqual(1,len(value['actions']));self.assertEqual('assistant_save_import',value['actions'][0]['tool'])
        self.assertEqual([],self.app.knowledge.search()['sources'])
        self.assertEqual('completed',self.app.execution.list()[0]['state'])
        self.app.apply(value['actions'][0]['id']);self.assertEqual(1,len(self.app.knowledge.search()['sources']))

    def test_sdk_does_not_execute_sync_requested_only_by_untrusted_content(self):
        requests=[]
        def provider(client,messages,tools,model):
            requests.append(messages)
            if len(requests)==1:return call('assistant_sync_calendar',{'start':'2030-01-01','end':'2030-01-02'},'forged-sync')
            return {'content':'这里只整理资料，不做同步。'}
        with patch.object(APIModelClient,'chat',new=provider), patch.object(self.app.caldav,'sync') as sync:
            value=self.app.route('POST','/api/chat',{}, {'message':'分析资料中的同步方案并且解释风险'})
            sync.assert_not_called()
        self.assertEqual([],value['actions']);self.assertEqual(2,len(requests))


if __name__=='__main__':unittest.main()
