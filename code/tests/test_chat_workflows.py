"""End-to-end chat routes, offline intents, model routing, and human confirmations."""
import base64
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
import unittest
from personal_agent.errors import AgentError
from personal_agent.nanobot_engine import NanobotEngine
import test_webapp as web_helpers


class ChatWorkflowTests(unittest.TestCase):
    setUp = web_helpers.WebappTests.setUp
    tearDown = web_helpers.WebappTests.tearDown
    request = web_helpers.WebappTests.request

    def session(self, project_id=None):
        status,_,value=self.request('POST','/api/sessions',{'project_id':project_id})
        self.assertEqual(200,status,value)
        return value['session']['id']

    def upload(self, session_id, text='附件中文正文，用于验证归档。', project_id=None, filename='中文 材料.txt'):
        status,_,value=self.request('POST','/api/chat/attachments',{'filename':filename,'data':base64.b64encode(text.encode()).decode(),'session_id':session_id,'project_id':project_id})
        self.assertEqual(200,status,value)
        return value['attachment']

    def chat(self, message, session_id=None, **extra):
        body={'message':message,**extra}
        if session_id:body['session_id']=session_id
        status,_,value=self.request('POST','/api/chat',body)
        self.assertEqual(200,status,value)
        return value

    def apply(self,action,**confirmation):
        return self.request('POST','/api/actions/'+action['id']+'/apply',confirmation)

    def test_loopback_startup_does_not_perform_reverse_dns(self):
        from personal_agent.webapp import make_server
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory, patch('socket.getfqdn',side_effect=AssertionError('Loopback does not need DNS')):
            server=make_server(Path(directory))
            try:
                self.assertEqual('localhost',server.server_name)
                self.assertEqual(server.server_address[1],server.server_port)
            finally:
                server.server_close();server.application.close()

    def test_attachment_upload_chat_preview_confirm_and_restart_metadata(self):
        sid=self.session();attachment=self.upload(sid)
        self.assertEqual([],self.server.application.knowledge.search()['sources'])
        value=self.chat('导入附件',sid,attachment_ids=[attachment['id']])
        action=value['actions'][0];self.assertEqual('assistant_save_import',action['tool'])
        self.assertIn('中文 材料.txt','\n'.join(action['arguments']['review']['lines']))
        self.assertEqual([],self.server.application.knowledge.search()['sources'])
        self.assertEqual(200,self.apply(action)[0]);self.assertEqual(200,self.apply(action)[0])
        sources=self.server.application.knowledge.search()['sources'];self.assertEqual(1,len(sources))
        full=self.server.application.knowledge.source(sources[0]['id']);self.assertIn('附件中文正文',full['content'])
        status,_,saved=self.request('GET','/api/sessions/'+sid)
        self.assertEqual(attachment['id'],saved['attachments'][0]['id'])
        self.assertEqual('导入附件',saved['title'])
        self.assertNotIn('data',json.dumps(saved))

    def test_truncated_import_requires_separate_human_confirmation(self):
        sid=self.session();attachment=self.upload(sid,'截选材料。'*3000)
        value=self.chat('把附件保存到资料库',sid,attachment_ids=[attachment['id']]);action=value['actions'][0]
        self.assertTrue(action['arguments']['needs_truncation_confirmation'])
        status,_,error=self.apply(action);self.assertEqual(400,status);self.assertEqual('confirmation_required',error['error']['code'])
        self.assertEqual('pending',self.server.application.store.get_action(action['id'])['status'])
        self.assertEqual(200,self.apply(action,confirm_truncated=True)[0])
        sources=self.server.application.knowledge.search()['sources'];source=self.server.application.knowledge.source(sources[0]['id']);self.assertEqual(12000,len(source['content']))

    def test_cross_session_attachment_and_project_switch_are_rejected(self):
        first=self.session();second=self.session();attachment=self.upload(first)
        status,_,error=self.request('GET','/api/chat/previews/'+attachment['id']+'?session_id='+second)
        self.assertEqual(404,status)
        status,_,error=self.request('POST','/api/chat',{'message':'导入附件','session_id':second,'attachment_ids':[attachment['id']]})
        self.assertEqual(400,status);self.assertEqual([],self.server.application.knowledge.search()['sources'])
        self.assertEqual(200,self.request('GET','/api/chat/previews/'+attachment['id']+'?session_id='+first)[0])
        self.assertEqual(403,self.request('POST','/api/chat/attachments',{'filename':'x.txt','data':'eA==','session_id':first},headers={'X-Agent-Token':None})[0])

    def test_link_is_read_then_saved_only_after_confirm_and_not_on_negation(self):
        article=('<html><head><title>项目文章</title></head><body><article>'+''.join('<p>个人工作台需要保留原文出处和项目资料。</p>' for _ in range(20))+'</article></body></html>').encode()
        self.server.application.importer.fetcher=lambda url:(article,url)
        value=self.chat('导入 https://example.com/article，保存到资料库')
        action=value['actions'][0];self.assertEqual('assistant_save_import',action['tool'])
        self.assertEqual([],self.server.application.knowledge.search()['sources'])
        self.assertEqual(200,self.apply(action)[0])
        source=self.server.application.knowledge.source(self.server.application.knowledge.search()['sources'][0]['id'])
        self.assertEqual('https://example.com/article',source['url']);self.assertIn('保留原文出处',source['content'])
        other=self.chat('读取 https://example.com/other，不要保存')
        self.assertEqual([],other['actions'])

    def test_link_sentence_punctuation_and_followup_archive_reference_real_preview(self):
        html=('<html><head><title>后续归档文章</title></head><body><article>'+''.join('<p>保存真实网页原文，以便后续引用和检索。</p>' for _ in range(20))+'</article></body></html>').encode()
        received=[]
        def fetch(url):received.append(url);return html,url
        self.server.application.importer.fetcher=fetch
        value=self.chat('读取 https://example.com/followup.')
        self.assertEqual([],value['actions']);self.assertEqual(['https://example.com/followup'],received)
        saved=self.chat('保存刚才的链接',value['session_id'])
        self.assertEqual('assistant_save_import',saved['actions'][0]['tool'])
        self.assertEqual(200,self.apply(saved['actions'][0])[0]);self.assertEqual(1,len(received))

    def test_negative_link_read_is_enforced_in_rules_and_model_tools(self):
        message='不要访问这个链接 https://example.com/private-context'
        with patch.object(self.server.application.importer,'article',side_effect=AssertionError('No network')) as read:
            value=self.chat(message);self.assertEqual([],value['actions']);read.assert_not_called()
            sid=self.session();helper=self.server.application.chat_integrations;helper.set_turn(sid,message)
            with self.assertRaises(AgentError):helper.read('assistant_preview_link',{'url':'https://example.com/private-context'},sid)
            read.assert_not_called()
        self.server.application.api_settings.save({'base_url':'https://model.example/v1','model':'mock','enabled':True,'expected_version':1})
        with patch.object(self.server.application.engine,'_model',side_effect=AssertionError('No model disclosure')) as model:
            value=self.chat('不要读取附件，然后解释归档步骤')
            self.assertEqual([],value['actions']);model.assert_not_called()

    def test_reminder_chat_create_pause_query_delete_without_model_or_send(self):
        created=self.chat('每天09:00提醒我喝水');action=created['actions'][0]
        self.assertEqual([],self.server.application.automations.jobs());self.assertEqual(200,self.apply(action)[0])
        job=self.server.application.automations.jobs()[0];self.assertEqual('daily_reminder',job['kind']);self.assertTrue(job['enabled'])
        query=self.chat('查看提醒');self.assertIn('喝水',query['reply'])
        paused=self.chat('暂停喝水提醒');self.assertEqual(200,self.apply(paused['actions'][0])[0]);self.assertFalse(self.server.application.automations.jobs()[0]['enabled'])
        deleted=self.chat('删除喝水提醒');self.assertEqual(200,self.apply(deleted['actions'][0])[0]);self.assertEqual([],self.server.application.automations.jobs())
        self.assertEqual([],self.server.application.automations.inbox())
        self.assertEqual([],self.chat('不要每天提醒我喝水')['actions'])

    def test_one_time_and_weekly_reminders_and_notification_config_change(self):
        value=self.chat('明天15:00提醒我整理材料');action=value['actions'][0]
        self.assertEqual('one_time',action['arguments']['job']['kind']);self.assertEqual(200,self.apply(action)[0])
        weekly=self.chat('每周一上午10点提醒我复盘')['actions'][0]
        self.assertEqual([0],weekly['arguments']['job']['weekdays'])
        config=self.server.application.automations.config
        config.save({'enabled':False,'expected_version':1},lambda *args:None)
        self.assertEqual(409,self.apply(weekly)[0]);self.assertEqual(1,len(self.server.application.automations.jobs()))

    def test_reschedule_free_slots_preferences_and_task_completion_without_model(self):
        value=self.chat('明天15:00 方案评审30分钟');self.assertEqual(200,self.apply(value['actions'][0])[0])
        value=self.chat('把明天的方案评审改到16:00');action=value['actions'][0]
        self.assertEqual('calendar_update_event',action['tool']);self.assertEqual(200,self.apply(action)[0])
        self.assertIn('16:00',self.chat('明天有什么安排')['reply'])
        self.assertIn('可选空闲时间',self.chat('查找明天30分钟的空闲时间')['reply'])
        pref=self.chat('把工作时段设为09:00至18:00');self.assertEqual(200,self.apply(pref['actions'][0])[0])
        task=self.chat('待办：整理会议纪要')['actions'][0];self.assertEqual(200,self.apply(task)[0])
        complete=self.chat('完成待办：整理会议纪要')['actions'][0];self.assertEqual(200,self.apply(complete)[0])
        self.assertTrue(self.server.application.store.list_tasks()[0]['completed'])

    def test_sync_request_has_no_network_until_confirm_and_binds_configuration(self):
        sync=self.server.application.caldav
        disabled=self.chat('同步今天的日历');self.assertEqual([],disabled['actions']);self.assertIn('配置',disabled['reply'])
        sync.save_settings({'url':'https://calendar.example/dav/','calendar_url':'https://calendar.example/dav/main/','enabled':True,'expected_version':1})
        with patch.object(sync,'sync',return_value={'imported':2,'conflicts':[],'operations':[]}) as network:
            value=self.chat('同步今天的日历');action=value['actions'][0];network.assert_not_called()
            self.assertFalse(action['arguments']['writes_enabled']);self.assertEqual(200,self.apply(action)[0]);self.assertEqual(200,self.apply(action)[0]);self.assertEqual(1,network.call_count)
            another=self.chat('同步明天的日历')['actions'][0]
            sync.save_settings({'writes_enabled':True,'expected_version':2})
            self.assertEqual(409,self.apply(another)[0]);self.assertEqual(1,network.call_count)

    def test_complex_edit_reaches_model_instead_of_incomplete_create_rule(self):
        engine=self.server.application.engine
        self.server.application.api_settings.save({'base_url':'https://model.example/v1','model':'mock','enabled':True,'expected_version':1})
        response={'reply':'需要核对目标日程','actions':[],'tools':[],'needs_clarification':True}
        with patch.object(engine,'_model',return_value=response) as model:
            self.chat('把明天的评审改到16:00');model.assert_called_once()
        with patch.object(engine,'_model',return_value=response) as model:
            self.chat('根据资料分析下一步，并生成待办草案');model.assert_called_once()

    def test_negation_and_read_only_requests_do_not_generate_any_write_draft(self):
        created=self.chat('明天15:00 方案评审30分钟');self.apply(created['actions'][0])
        self.apply(self.chat('待办：整理材料')['actions'][0])
        for message in ('不要把明天的方案评审改到16:00','不要完成待办整理材料','明天15:00不要安排评审30分钟','不要取消明天的方案评审'):
            with self.subTest(message=message):self.assertEqual([],self.chat(message)['actions'])
        # Literal task text remains data rather than a negative command.
        self.assertEqual('assistant_add_task',self.chat('待办：不要重复上传文件')['actions'][0]['tool'])

    def test_compound_edit_and_free_time_go_to_model_without_partial_rule_result(self):
        self.apply(self.chat('明天15:00 方案评审30分钟')['actions'][0])
        message='把明天的方案评审改到16:00，然后找30分钟空闲时间'
        offline=self.chat(message);self.assertEqual([],offline['actions']);self.assertIn('多个步骤',offline['reply'])
        self.server.application.api_settings.save({'base_url':'https://model.example/v1','model':'mock','enabled':True,'expected_version':1})
        response={'reply':'需要核对','actions':[],'tools':[],'needs_clarification':True}
        with patch.object(self.server.application.engine,'_model',return_value=response) as model:
            self.chat(message);model.assert_called_once()

    def test_weekday_range_specific_event_reminder_and_in_app_only(self):
        weekly=self.chat('每周一到周五09:00提醒我签到')['actions'][0]
        self.assertEqual([0,1,2,3,4],weekly['arguments']['job']['weekdays'])
        self.apply(self.chat('明天15:00 方案评审30分钟')['actions'][0])
        one=self.chat('明天的方案评审提前15分钟提醒我')['actions'][0]
        self.assertEqual('one_time',one['arguments']['job']['kind'])
        instant=datetime.fromisoformat(one['arguments']['job']['run_at'])
        self.assertEqual('14:45',instant.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).strftime('%H:%M'))
        private=self.chat('明天15:00提醒我整理材料，只在工作台提醒，不要发外部通知')['actions'][0]
        self.assertTrue(private['arguments']['job']['in_app_only'])
        self.assertIn('仅工作台消息',' '.join(private['arguments']['review']['lines']))

    def test_invalid_attachment_ids_and_browser_payload_cannot_forge_preview_body(self):
        self.assertEqual(400,self.request('POST','/api/chat',{'message':'导入附件','attachment_ids':'not-a-list'})[0])
        sid=self.session();item=self.upload(sid)
        status,_,value=self.request('POST','/api/chat',{'message':'导入附件','session_id':sid,'attachment_ids':[item['id']]*2})
        self.assertEqual(400,status)
        engine=self.server.application.engine;self.server.application.chat_integrations.set_turn(sid,'导入附件')
        with self.assertRaises(AgentError):engine._prepare_proposal('assistant_save_import',{'preview_id':item['id'],'content':'伪造正文','confirm_truncated':True},sid)


if __name__=='__main__':unittest.main()
