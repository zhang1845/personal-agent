"""Feature acceptance uses real parsers/SDK and fake networks, never real accounts."""
import base64
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from docx import Document
from icalendar import Calendar,Event
from personal_agent.service import CalendarService
from personal_agent.personal_store import PersonalStore
from personal_agent.knowledge import KnowledgeStore
from personal_agent.importing import ImportService,public_target
from personal_agent.automations import Automations
from personal_agent.caldav_sync import CalDAVSync,digest
from personal_agent.execution import ExecutionLog
from personal_agent.nanobot_engine import NanobotEngine
from personal_agent.errors import AgentError
import test_webapp as http_helpers

class Base(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.calendar=CalendarService(str(self.root/'calendar.sqlite3'));self.store=PersonalStore(self.root/'assistant.sqlite3');self.library=KnowledgeStore(self.root/'knowledge.sqlite3')
 def tearDown(self):self.calendar.close();self.temp.cleanup()
 def event(self,request='one',**kw):
  args={'request_id':request,'title':'评审','start':'2030-01-07T09:00:00+08:00','end':'2030-01-07T10:00:00+08:00'};args.update(kw);return self.calendar.call('calendar_create_event',args)

class ImportTests(Base):
 def test_public_dns_is_opt_in_and_never_allows_internal_targets(self):
  fake=[(2,1,6,'',('198.18.0.2',443))]
  with patch('socket.getaddrinfo',return_value=fake),patch('personal_agent.importing._public_dns',return_value=['8.8.8.8']) as resolver:
   with self.assertRaises(AgentError):public_target('https://example.com')
   resolver.assert_not_called();self.assertEqual('8.8.8.8',public_target('https://example.com',True)[1]);resolver.assert_called_once_with('example.com')
  with patch('personal_agent.importing._public_dns') as resolver:
   with self.assertRaises(AgentError):public_target('https://127.0.0.1',True)
   resolver.assert_not_called()
  with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]),patch('personal_agent.importing._public_dns') as resolver:
   with self.assertRaises(AgentError):public_target('https://internal.example',True)
   resolver.assert_not_called()
 def test_public_dns_private_answer_is_rejected(self):
  from personal_agent.importing import _public_dns
  with patch('personal_agent.importing.http.client.HTTPSConnection') as connection:
   response=connection.return_value.getresponse.return_value;response.status=200;response.read.return_value=b'{"Status":0,"Answer":[{"type":1,"data":"127.0.0.1"}]}'
   with self.assertRaises(AgentError):_public_dns('example.com')
 def test_real_text_pdf_parser(self):
  from pypdf import PdfWriter
  from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
  writer=PdfWriter();page=writer.add_blank_page(width=300,height=300)
  font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
  page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
  stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 200 Td (Actual PDF article text.) Tj ET');page[NameObject('/Contents')]=writer._add_object(stream)
  out=io.BytesIO();writer.write(out);result=ImportService(self.library).document('text.pdf',base64.b64encode(out.getvalue()).decode());self.assertIn('Actual PDF article text',result['content'])
 def test_docx_preview_does_not_save_and_retains_text(self):
  d=Document();d.add_heading('报告',0);d.add_paragraph('原始材料正文。');out=io.BytesIO();d.save(out)
  result=ImportService(self.library).document('report.docx',base64.b64encode(out.getvalue()).decode())
  self.assertIn('原始材料正文',result['content']);self.assertEqual(0,self.library.search()['total']);self.assertFalse(result['truncated'])
 def test_pdf_preview_and_scan_warning(self):
  from pypdf import PdfWriter
  out=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=200,height=200);writer.write(out)
  with self.assertRaises(AgentError) as error:ImportService(self.library).document('scan.pdf',base64.b64encode(out.getvalue()).decode())
  self.assertIn('扫描件',error.exception.message)
 def test_article_preview_duplicates_and_truncation_are_explicit(self):
  html=('<html><head><title>材料原文</title></head><body><article>'+''.join('<p>个人日程与资料检索的材料要保留来源，不能将推断当作原文事实。</p>' for _ in range(20))+'</article></body></html>').encode()
  importer=ImportService(self.library,fetcher=lambda url:(html,url));result=importer.article('https://example.com/article')
  self.assertIn('保留来源',result['content']);self.assertEqual(0,self.library.search()['total'])
  self.library.save_source({'title':result['title'],'kind':'material','content':result['content'],'url':result['url']})
  self.assertEqual(1,len(importer.article(result['url'])['duplicates']))
  result=importer.result('长文','长'*12001);self.assertTrue(result['truncated']);self.assertTrue(result['warning']);self.assertEqual(12000,len(result['content']))
 def test_ssrf_local_and_mixed_dns_targets_rejected(self):
  for url in ('file:///etc/passwd','http://127.0.0.1/','http://localhost/','http://169.254.169.254/','https://user:pass@example.com/'):
   with self.subTest(url=url),patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]),self.assertRaises(AgentError):public_target(url)
  with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443)),(2,1,6,'',('10.0.0.1',443))]),self.assertRaises(AgentError):public_target('https://example.com')
 def test_invalid_document_and_exact_duplicate_save_rejected(self):
  with self.assertRaises(AgentError):ImportService(self.library).document('wrong.pdf',base64.b64encode(b'not PDF').decode())
  self.library.save_source({'title':'原始','kind':'material','content':'同一正文'})
  with self.assertRaises(AgentError) as error:self.library.save_source({'title':'重复','kind':'material','content':'同一正文'})
  self.assertEqual('duplicate_source',error.exception.code)

class RecurrenceTests(Base):
 def series(self,**kw):return self.event('series',recurrence={'frequency':'weekly','count':4},**kw)
 def events(self):return self.calendar.call('calendar_list_events',{'start':'2030-01-01','end':'2030-02-01'})['events']
 def test_create_replay_and_occurrences_in_calendar(self):
  result=self.series();self.assertEqual(4,result['occurrences']);self.assertTrue(self.series()['replayed']);events=self.events();self.assertEqual(4,len(events));self.assertEqual(4,len({e['id'] for e in events}));self.assertTrue(all(e['recurring'] for e in events))
 def test_single_edit_delete_and_stale_series_version(self):
  self.series();before=self.events();event=before[1]
  updated=self.calendar.call('calendar_update_event',{'request_id':'edit-one','event_id':event['id'],'expected_version':event['version'],'changes':{'title':'仅本次改名'},'scope':'occurrence'})['event']
  after=self.events();self.assertEqual(1,sum(e['title']=='仅本次改名' for e in after))
  self.assertTrue(updated['recurring']);self.assertEqual(updated['series_id'],event['series_id'])
  with self.assertRaises(AgentError):self.calendar.call('calendar_delete_event',{'request_id':'stale','event_id':before[0]['id'],'expected_version':before[0]['version']})
  self.calendar.call('calendar_delete_event',{'request_id':'delete-one','event_id':updated['id'],'expected_version':updated['version'],'scope':'occurrence'});self.assertEqual(3,len(self.events()))
 def test_whole_series_edit_and_delete(self):
  result=self.series();event=result['event']
  updated=self.calendar.call('calendar_update_event',{'request_id':'edit-all','event_id':event['id'],'expected_version':event['version'],'changes':{'title':'全部改名'},'scope':'series'})['event']
  self.assertTrue(all(e['title']=='全部改名' for e in self.events()))
  self.calendar.call('calendar_delete_event',{'request_id':'delete-all','event_id':updated['id'],'expected_version':updated['version'],'scope':'series'});self.assertEqual([],self.events())
 def test_series_conflict_rolls_back_all_and_request_ids_are_shared(self):
  self.event('ordinary')
  with self.assertRaises(AgentError) as error:self.series()
  self.assertEqual('conflict',error.exception.code);self.assertEqual(1,len(self.events()))
  with self.assertRaises(AgentError) as error:self.event('ordinary',start='2030-01-08T09:00:00+08:00',end='2030-01-08T10:00:00+08:00',recurrence={'frequency':'daily','count':2})
  self.assertEqual('request_id_conflict',error.exception.code)
 def test_monthly_rule_skips_invalid_dates_and_is_finite(self):
  result=self.event('month',start='2030-01-31T09:00:00+08:00',end='2030-01-31T10:00:00+08:00',recurrence={'frequency':'monthly','count':3})
  self.assertEqual(3,result['occurrences'])
  with self.calendar.storage.connection() as c:body=json.loads(c.execute('SELECT body FROM recurrence_series').fetchone()['body'])
  starts=[a[:10] for a,b in self.calendar.recurrence.occurrences(body)];self.assertEqual(['2030-01-31','2030-03-31','2030-05-31'],starts)
 def test_exception_move_still_in_conflict_checks_and_free_time(self):
  result=self.series();event=result['event'];self.calendar.call('calendar_update_event',{'request_id':'move','event_id':event['id'],'expected_version':event['version'],'changes':{'start':'2030-01-08T11:00:00+08:00','end':'2030-01-08T12:00:00+08:00'}})
  with self.assertRaises(AgentError) as error:self.event('overlap',start='2030-01-08T11:15:00+08:00',end='2030-01-08T11:45:00+08:00')
  self.assertEqual('conflict',error.exception.code)
 def test_dst_gap_rejects_entire_series(self):
  self.calendar.call('calendar_set_preferences',{'timezone':'America/New_York'})
  with self.assertRaises(AgentError):self.event('dst',start='2030-03-09T02:30:00-05:00',end='2030-03-09T03:00:00-05:00',recurrence={'frequency':'daily','count':3})
  with self.calendar.storage.connection() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM recurrence_series').fetchone()[0])
 def test_unknown_calendar_not_silently_replaced_with_local(self):
  with self.assertRaises(AgentError):self.series(calendar_id='another-calendar')

class AutomationTests(Base):
 def setUp(self):super().setUp();self.auto=Automations(self.root,self.calendar,self.store)
 def tearDown(self):self.auto.close();super().tearDown()
 def test_daily_summary_is_durable_and_deduplicated(self):
  self.store.add_task('到期任务','2030-01-07');job=self.auto.save_job({'name':'早报','kind':'daily_summary','time':'09:00','enabled':True})
  instant=datetime.fromisoformat('2030-01-07T09:00:10+08:00');self.auto.tick(instant);self.auto.tick(instant+timedelta(seconds=20))
  inbox=self.auto.inbox();self.assertEqual(1,len(inbox));self.assertIn('到期任务',inbox[0]['body']);self.assertEqual('in_app',inbox[0]['state'])
  restarted=Automations(self.root,self.calendar,self.store);restarted.tick(instant);self.assertEqual(1,len(restarted.inbox()))
 def test_reminder_emits_once_and_skips_cancelled_events(self):
  event=self.event()['event'];self.auto.save_job({'name':'提前提醒','kind':'event_reminders','lead_minutes':15,'enabled':True})
  instant=datetime.fromisoformat('2030-01-07T08:45:20+08:00');self.auto.tick(instant);self.auto.tick(instant);self.assertEqual(1,len(self.auto.inbox()))
  self.calendar.call('calendar_delete_event',{'request_id':'cancel','event_id':event['id'],'expected_version':event['version']});self.auto.tick(instant+timedelta(seconds=20));self.assertEqual(1,len(self.auto.inbox()))
 def test_unknown_delivery_is_not_retried_and_secrets_not_public(self):
  sent=[];self.auto.sender=lambda *args:sent.append(args) or False
  self.auto.save_settings({'urls':'json://localhost/hook','enabled':True,'expected_version':1})
  nid=self.auto.emit('once','job','title','body');self.auto.drain();self.auto.drain();self.assertEqual(1,len(sent));self.assertEqual('unknown',self.auto.inbox()[0]['state']);self.assertNotIn('urls',self.auto.settings())
 def test_paused_or_missed_schedules_do_not_backfill(self):
  self.auto.save_job({'name':'暂停','kind':'daily_summary','time':'09:00','enabled':False});self.auto.tick(datetime.fromisoformat('2030-01-07T09:00:00+08:00'));self.auto.tick(datetime.fromisoformat('2030-01-07T09:01:00+08:00'));self.assertEqual([],self.auto.inbox())
 def test_zero_lead_reminder_at_start(self):
  self.event();self.auto.save_job({'name':'开始提醒','kind':'event_reminders','lead_minutes':0,'enabled':True});self.auto.tick(datetime.fromisoformat('2030-01-07T09:00:15+08:00'));self.assertEqual(1,len(self.auto.inbox()))
 def test_expired_outbox_not_delivered_after_restart(self):
  sent=[];self.auto.sender=lambda *args:sent.append(args);self.auto.save_settings({'urls':'json://localhost/hook','enabled':True,'expected_version':1})
  nid=self.auto.emit('expired','job','old','body')
  with self.auto.connection() as c:c.execute('UPDATE notifications SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),nid))
  self.auto.drain();self.assertEqual([],sent);self.assertEqual('expired',self.auto.inbox()[0]['state'])

class Response:
 def __init__(self,status=200,raw='',etag='"1"'):self.status=status;self.raw=raw;self.headers={'ETag':etag}
class Resource:
 def __init__(self,url,raw,etag='"1"'):self.url=url;self.data=raw;self.props={'{DAV:}getetag':etag};self.name='测试日历'
class DAV:
 def __init__(self):self.records={};self.puts=0;self.deletes=0;self.unknown=False
 def principal(self):return self
 def calendars(self):return [Resource('https://dav.example/calendar/','')]
 def calendar(self,url):return self
 def search(self,**kwargs):return [Resource(url,raw,etag) for url,(raw,etag) in self.records.items()]
 def request(self,url,method):
  value=self.records.get(url);return Response(200,value[0],value[1]) if value else Response(404)
 def put(self,url,body,headers=None):
  self.puts+=1
  if headers.get('If-None-Match')=='*' and url in self.records:return Response(412)
  if 'If-Match' in headers and (url not in self.records or self.records[url][1]!=headers['If-Match']):return Response(412)
  old=self.records.get(url);etag='"'+str(int(old[1].strip('"'))+1 if old else 1)+'"';self.records[url]=(body,etag)
  if self.unknown:raise OSError('pretend network interruption')
  return Response(201,etag=etag)
 def delete(self,url,headers=None):
  self.deletes+=1
  if url not in self.records:return Response(404)
  if headers['If-Match']!=self.records[url][1]:return Response(412)
  del self.records[url];return Response(204)
 def close(self):pass

def remote_ics(title='远端安排',uid='remote-one'):
 cal=Calendar();cal.add('version','2.0');event=Event();event.add('uid',uid);event.add('summary',title);event.add('dtstart',datetime.fromisoformat('2030-01-09T09:00:00+08:00').astimezone(timezone.utc));event.add('dtend',datetime.fromisoformat('2030-01-09T10:00:00+08:00').astimezone(timezone.utc));cal.add_component(event);return cal.to_ical().decode()

class CalDAVTests(Base):
 def setUp(self):
  super().setUp();self.dav=DAV();self.sync=CalDAVSync(self.root,self.calendar,client_factory=lambda _:self.dav)
  self.sync.save_settings({'url':'https://dav.example/','username':'user','password':'private-test-password','calendar_url':'https://dav.example/calendar/','enabled':True,'writes_enabled':False,'expected_version':1})
 def run_sync(self):return self.sync.sync('2030-01-01','2030-02-01')
 def enable_writes(self):return self.sync.save_settings({'writes_enabled':True,'expected_version':self.sync.settings()['version']})
 def test_discovery_and_read_only_import_are_real_mapping_without_writes(self):
  self.dav.records['https://dav.example/calendar/remote.ics']=(remote_ics(),'"1"');self.assertTrue(self.sync.test()['connected']);result=self.run_sync();self.assertEqual(1,result['imported']);self.assertEqual(0,self.dav.puts)
  event=self.calendar.call('calendar_list_events',{'start':'2030-01-01','end':'2030-02-01'})['events'][0];self.assertTrue(event['external']['readonly']);self.assertNotIn('password',self.sync.settings())
  with self.assertRaises(AgentError):self.calendar.call('calendar_update_event',{'request_id':'edit-readonly','event_id':event['id'],'expected_version':event['version'],'changes':{'title':'cannot'}})
 def test_push_checks_etag_and_preserves_uid_and_deletion(self):
  local=self.event()['event'];self.enable_writes();self.run_sync();self.assertEqual(1,self.dav.puts);mapping=self.sync.rows('mappings',self.sync.profile(self.sync.config.read()))[0]
  local=self.calendar.call('calendar_update_event',{'request_id':'local-edit','event_id':local['id'],'expected_version':local['version'],'changes':{'title':'本地更新'}})['event'];self.run_sync();self.assertEqual(2,self.dav.puts);self.assertIn(mapping['uid'],self.dav.records[mapping['remote_url']][0])
  self.calendar.call('calendar_delete_event',{'request_id':'local-delete','event_id':local['id'],'expected_version':self.calendar.call('calendar_get_event',{'event_id':local['id']})['event']['version']});self.run_sync();self.assertEqual(1,self.dav.deletes)
 def test_concurrent_local_and_remote_edits_are_not_overwritten(self):
  url='https://dav.example/calendar/remote.ics';self.dav.records[url]=(remote_ics(),'"1"');self.enable_writes();self.run_sync();event=self.calendar.call('calendar_list_events',{'start':'2030-01-01','end':'2030-02-01'})['events'][0]
  self.calendar.call('calendar_update_event',{'request_id':'local','event_id':event['id'],'expected_version':event['version'],'changes':{'title':'本地修改'}});self.dav.records[url]=(remote_ics('远端修改'),'"2"');result=self.run_sync();self.assertEqual(1,len(result['conflicts']));self.assertEqual(0,self.dav.puts)
  current=self.calendar.call('calendar_get_event',{'event_id':event['id']})['event'];self.sync.resolve(result['conflicts'][0]['id'],'remote',current['version']);self.assertEqual('远端修改',self.calendar.call('calendar_get_event',{'event_id':event['id']})['event']['title'])
 def test_unknown_write_is_not_resent_and_can_be_read_reconciled(self):
  local=self.event()['event'];self.enable_writes();self.dav.unknown=True;self.run_sync();self.assertEqual(1,self.dav.puts);op=self.sync.status()['operations'][0];self.assertEqual('unknown',op['status'])
  self.run_sync();self.assertEqual(1,self.dav.puts);self.assertEqual(1,len(self.calendar.call('calendar_list_events',{'start':'2030-01-01','end':'2030-02-01'})['events']))
  result=self.sync.reconcile(op['id']);self.assertTrue(result['confirmed']);self.assertEqual(1,self.dav.puts)
 def test_remote_removed_repeat_occurrences_confirmed_from_master(self):
  import recurring_ical_events
  url='https://dav.example/calendar/repeat.ics'
  def recurring(count):
   parsed=Calendar.from_ical(remote_ics());parsed.walk('VEVENT')[0].add('rrule',{'freq':'daily','count':count});return parsed.to_ical().decode()
  def search(**kwargs):
   result=[]
   for event in recurring_ical_events.of(Calendar.from_ical(self.dav.records[url][0])).between(kwargs['start'],kwargs['end']):
    cal=Calendar();cal.add('version','2.0');cal.add_component(event);result.append(Resource(url,cal.to_ical().decode(),self.dav.records[url][1]))
   return result
  self.dav.search=search;self.dav.records[url]=(recurring(3),'"1"');self.run_sync();self.assertEqual(3,len(self.sync.rows('mappings',self.sync.profile(self.sync.config.read()))))
  self.dav.records[url]=(recurring(1),'"2"');self.run_sync();self.assertEqual(1,len(self.sync.rows('mappings',self.sync.profile(self.sync.config.read()))))
 def test_remote_deletion_requires_get_confirmation(self):
  url='https://dav.example/calendar/remote.ics';self.dav.records[url]=(remote_ics(),'"1"');self.run_sync();self.dav.records.clear();self.run_sync();self.assertEqual([],self.calendar.call('calendar_list_events',{'start':'2030-01-01','end':'2030-02-01'})['events'])

class NanobotTests(Base):
 def test_repeated_request_not_downgraded_to_single_rule_event(self):
  engine,model,log=self.model_engine([{'content':'请在表单确认重复规则。'}])
  result=engine.respond('明天15:00 评审30分钟，每天重复三次',model='fake-model',model_provider='nanobot');self.assertEqual(1,len(model.calls));self.assertEqual([],result['actions'])
 def model_engine(self,responses):
  class Model:
   def __init__(self):self.calls=[]
   def chat(inner,messages,tools,model):
    inner.calls.append((messages,tools,model));response=responses.pop(0)
    if isinstance(response,Exception):raise response
    return response
  model=Model();log=ExecutionLog(self.root/'runs.sqlite3');engine=NanobotEngine(self.calendar,self.store,model,self.library,workspace=self.root/'nano',execution=log);return engine,model,log
 def test_real_sdk_reads_sources_then_creates_only_a_draft(self):
  self.library.save_source({'title':'方案资料','kind':'material','content':'先核对资料再推进方案。'})
  calls=lambda name,args:{'content':'','tool_calls':[{'id':name,'function':{'name':name,'arguments':args}}]}
  engine,model,log=self.model_engine([calls('knowledge_search',{'query':'方案'}),calls('assistant_add_task',{'title':'推进方案'}),{'content':'草案供确认。'}])
  result=engine.respond('根据方案资料整理下一步并生成待办',model='fake-model',model_provider='nanobot');self.assertEqual('nanobot',result['mode']);self.assertEqual(3,len(model.calls));self.assertEqual(1,len(result['actions']));self.assertEqual([],self.store.list_tasks());self.assertEqual('completed',log.list()[0]['state'])
  names={t['function']['name'] for t in model.calls[0][1]};self.assertNotIn('exec',names);self.assertNotIn('spawn',names);self.assertNotIn('read_file',names)
 def test_api_failure_not_retried_and_visible_in_trace(self):
  engine,model,log=self.model_engine([AgentError('model_unavailable','API 故障')]);result=engine.respond('分析材料',model='fake-model',model_provider='nanobot');self.assertEqual(1,len(model.calls));self.assertEqual('failed',log.list()[0]['state']);self.assertEqual([],result['actions'])
 def test_api_failure_keeps_already_prepared_drafts_visible(self):
  response={'content':'','tool_calls':[{'id':'draft','function':{'name':'assistant_add_task','arguments':{'title':'后续推进'}}}]}
  engine,model,log=self.model_engine([response,AgentError('model_unavailable','API 故障')]);result=engine.respond('整理推进计划',model='fake-model',model_provider='nanobot');self.assertEqual(1,len(result['actions']));self.assertEqual([],self.store.list_tasks());self.assertIn('尚未执行',result['reply']);self.assertEqual('failed',log.list()[0]['state'])

class FeatureHTTPTests(unittest.TestCase):
 setUp=http_helpers.WebappTests.setUp;tearDown=http_helpers.WebappTests.tearDown;request=http_helpers.WebappTests.request
 def test_new_endpoints_default_to_disconnected_and_preserve_secrets(self):
  for path in ('/api/runtime','/api/automations','/api/caldav','/api/agent-runs'):
   status,_,value=self.request('GET',path);self.assertEqual(200,status,value)
  _,_,data=self.request('GET','/api/caldav');self.assertFalse(data['settings']['enabled']);self.assertFalse(data['settings']['writes_enabled']);self.assertNotIn('password',data['settings'])
 def test_import_requires_origin_and_does_not_save_preview(self):
  payload={'filename':'notes.txt','data':base64.b64encode('导入正文'.encode()).decode()}
  status,_,_=self.request('POST','/api/imports/document',payload,headers={'X-Agent-Token':None});self.assertEqual(403,status)
  status,_,value=self.request('POST','/api/imports/document',payload);self.assertEqual(200,status,value);self.assertEqual('导入正文',value['preview']['content'])
  _,_,value=self.request('GET','/api/knowledge/sources');self.assertEqual(0,value['total'])
 def test_repeat_create_and_scoped_cancel_http(self):
  args=http_helpers.WebappTests.create_arguments('repeat-http');args['recurrence']={'frequency':'daily','count':3}
  status,_,value=self.request('POST','/api/events',args);self.assertEqual(200,status,value);event=value['event']
  status,_,value=self.request('DELETE','/api/events/'+event['id'],{'request_id':'cancel-series','expected_version':event['version'],'scope':'series'});self.assertEqual(200,status,value)
if __name__=='__main__':unittest.main()
