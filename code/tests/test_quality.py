"""Feedback and retrieval evaluation acceptance, isolated from personal data."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from personal_agent.errors import AgentError
from personal_agent.knowledge import KnowledgeStore
from personal_agent.personal_store import PersonalStore
from personal_agent.quality import QualityStore
from personal_agent.retrieval import chunks,rank,BASELINE,REVISION
from personal_agent.webapp import Application
import test_webapp as http_helpers


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='质量 中文 ');self.root=Path(self.temp.name)
        self.store=PersonalStore(self.root/'assistant.sqlite3');self.library=KnowledgeStore(self.root/'knowledge.sqlite3')
        self.quality=QualityStore(self.root,self.store,self.library)
        self.project=self.library.save_project({'name':'项目甲','background':''})
        self.source=self.save_source('项目甲手册','退款申请需要订单编号。申请通过后七个工作日到账。')
        self.session=self.store.new_session('反馈验收')['id'];self.library.bind_session(self.session,self.project['id'])
        self.store.add_message(self.session,'user','退款申请需要什么？');self.message=self.store.add_message(self.session,'assistant','请提供订单编号。')
        self.library.save_answer_sources(self.message,[dict(id=self.source['id'],version=1,title=self.source['title'],excerpt='退款申请需要订单编号。')])
    def tearDown(self):self.temp.cleanup()
    def save_source(self,title,content,project=None):
        return self.library.save_source({'title':title,'kind':'material','content':content,'project_id':project or self.project['id']})
    def feedback(self,**changes):
        body={'session_id':self.session,'message_id':self.message,'rating':'issue','category':'retrieval','note':'请核对出处'};body.update(changes);return self.quality.feedback(body)
    def case(self,**changes):
        body={'title':'退款问题','query':'退款申请需要什么','kind':'answerable','project_id':self.project['id'],'expected_source_ids':[self.source['id']]};body.update(changes);return self.quality.save_case(body)
    def error(self,code,fn,*args):
        with self.assertRaises(AgentError) as caught:fn(*args)
        self.assertEqual(code,caught.exception.code)

    def test_long_source_uses_exact_late_chunk_and_stable_versioned_offsets(self):
        source=self.save_source('长文','普通记录。'*170+'量子菠萝退货凭证必须提供星图编号。'+'附录说明。'*40)
        found=self.library.search('量子菠萝退货凭证',self.project['id'])['sources'][0]
        self.assertEqual(source['id'],found['id']);self.assertGreater(found['chunk']['start'],0)
        self.assertIn('星图编号',found['excerpt'])
        self.assertEqual(source['content'][found['chunk']['start']:found['chunk']['end']],found['excerpt'])
        self.assertEqual(found,self.library.search('量子菠萝退货凭证',self.project['id'])['sources'][0])

    def test_unicode_chunk_offsets_cover_original_without_gaps(self):
        text='😀中文资料\n'*200;parts=list(chunks(text))
        self.assertEqual(0,parts[0][0]);self.assertEqual(len(text),parts[-1][1])
        for (a,b,value),next_part in zip(parts,parts[1:]):
            self.assertEqual(text[a:b],value);self.assertLess(next_part[0],b)

    def test_project_filter_holds_for_chunks_and_personal_thought_is_separate(self):
        other=self.library.save_project({'name':'乙','background':''})
        self.save_source('乙私密','量子菠萝退货凭证',other['id'])
        self.assertEqual([],self.library.search('量子菠萝',self.project['id'])['sources'])
        self.assertEqual('chunks-bm25-v1',self.library.search('')['method'])

    def test_feedback_captures_original_question_answer_and_citation_snapshot(self):
        self.quality.record_run(self.message,'run-local');feedback=self.feedback()
        self.assertEqual('退款申请需要什么？',feedback['question']);self.assertEqual('run-local',feedback['run_id'])
        self.library.delete_source(self.source['id'],1)
        self.assertEqual('退款申请需要订单编号。',self.quality.row('feedback',feedback['id'])['sources'][0]['excerpt'])
        self.error('validation_error',self.feedback)

    def test_foreign_session_and_user_message_cannot_receive_answer_feedback(self):
        other=self.store.new_session('其他')['id']
        with self.assertRaises(AgentError):self.feedback(session_id=other)
        user=self.store.add_message(self.session,'user','这不是助手回复')
        with self.assertRaises(AgentError):self.feedback(message_id=user)
        self.assertEqual([],self.quality.rows('feedback'))

    def test_concurrent_feedback_is_unique_and_updates_require_latest_version(self):
        def write(_):
            try:return self.feedback()['id']
            except AgentError:return None
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(write,range(2)))
        self.assertEqual(1,len(self.quality.rows('feedback')))
        f=self.quality.rows('feedback')[0]
        self.feedback(expected_version=f['version'],note='新说明')
        with self.assertRaises(AgentError):self.feedback(expected_version=f['version'])

    def test_manual_close_requires_diagnosis_fix_and_real_verification_note(self):
        f=self.feedback();patch={'expected_version':1,'status':'closed','diagnosis':'','fix':'','verification':''}
        self.error('validation_error',self.quality.update_feedback,f['id'],patch)
        patch.update(diagnosis='关键词过宽',fix='补充检索用例',verification='重新核对引用原文')
        updated=self.quality.update_feedback(f['id'],patch);self.assertEqual('closed',updated['status'])
        reopened=self.feedback(expected_version=updated['version'],note='仍有问题')
        self.assertEqual('new',reopened['status']);self.assertTrue(reopened['history'])

    def test_case_requires_human_expected_sources_and_preserves_feedback_link(self):
        f=self.feedback();case=self.case(feedback_id=f['id'])
        self.assertEqual(f['id'],case['feedback_id']);self.assertEqual(self.source['id'],case['expected'][0]['id'])
        with self.assertRaises(AgentError):self.case(expected_source_ids=[])
        other=self.library.save_project({'name':'乙','background':''});foreign=self.save_source('外部','退款',other['id'])
        with self.assertRaises(AgentError):self.case(expected_source_ids=[foreign['id']])

    def test_evaluation_compares_same_cases_and_tracks_denominators_and_negatives(self):
        self.case();self.case(title='无相关资料',query='火星企鹅航线',kind='unanswerable',expected_source_ids=[])
        report=self.quality.evaluate({'top_k':3})
        self.assertEqual({BASELINE,REVISION},set(report['engines']))
        for engine in report['engines'].values():
            self.assertEqual(2,engine['metrics']['count']);self.assertEqual(1,engine['metrics']['positive_count'])
            self.assertEqual(1,engine['metrics']['negative_count']);self.assertEqual(1,engine['metrics']['hit_at_k'])
            self.assertEqual(1,engine['metrics']['empty_result_rate']);self.assertEqual(2,engine['metrics']['passed'])
        self.assertFalse(self.quality.overview()['evaluations'][0]['stale'])
        self.save_source('新增','新材料');self.assertTrue(self.quality.overview()['evaluations'][0]['stale'])

    def test_empty_dataset_does_not_produce_fake_metrics(self):
        self.error('validation_error',self.quality.evaluate,{'top_k':3})
        self.case(title='仅负例',query='火星企鹅航线',kind='unanswerable',expected_source_ids=[])
        for result in self.quality.evaluate({'top_k':3})['engines'].values():self.assertIsNone(result['metrics']['hit_at_k'])

    def test_changed_expected_source_blocks_eval_until_case_is_reviewed(self):
        c=self.case();source=self.source
        self.library.save_source({'title':source['title'],'kind':'material','content':'退款申请规则已更新。','project_id':self.project['id'],'expected_version':1},source['id'])
        self.error('case_needs_review',self.quality.evaluate,{'top_k':3})
        self.quality.save_case({'expected_version':1,'active':False},c['id'])
        self.quality.save_case({'expected_version':2,'active':True},c['id'])
        self.error('case_needs_review',self.quality.evaluate,{'top_k':3})
        body={'title':'已核对','query':'退款申请','kind':'answerable','project_id':self.project['id'],'expected_source_ids':[source['id']],'expected_version':3}
        self.quality.save_case(body,c['id']);self.quality.evaluate({'top_k':3})

    def test_old_reports_are_immutable_and_case_changes_mark_stale(self):
        case=self.case();run=self.quality.evaluate({'top_k':2});old=self.quality.row('evaluations',run['id'])
        self.quality.save_case({'expected_version':1,'active':False},case['id'])
        self.assertTrue(self.quality.overview()['evaluations'][0]['stale'])
        self.assertEqual(old,self.quality.row('evaluations',run['id']))

    def test_fixed_sample_benchmark_is_isolated_and_has_explicit_scope(self):
        from quality_benchmark import benchmark
        result=benchmark()
        self.assertEqual(12,result['engines'][REVISION]['metrics']['count'])
        self.assertEqual(1,len(self.library.corpus()))
        self.assertEqual([],self.quality.rows('cases'))
        self.assertIn('不代表',result['scope'])


class QualityHTTPTests(unittest.TestCase):
    setUp=http_helpers.WebappTests.setUp;tearDown=http_helpers.WebappTests.tearDown;request=http_helpers.WebappTests.request
    def test_feedback_real_route_session_history_and_csrf(self):
        status,_,chat=self.request('POST','/api/chat',{'message':'今天有什么安排'})
        self.assertEqual(200,status);self.assertIsInstance(chat['message_id'],int)
        body={'session_id':chat['session_id'],'message_id':chat['message_id'],'rating':'issue','category':'other','note':'验收'}
        code,_,_=self.request('POST','/api/quality/feedback',body,headers={'X-Agent-Token':'bad'})
        self.assertEqual(403,code)
        code,_,result=self.request('POST','/api/quality/feedback',body);self.assertEqual(200,code)
        code,_,session=self.request('GET','/api/sessions/'+chat['session_id']);self.assertEqual(1,len(session['feedback']))
        code,_,overview=self.request('GET','/api/quality');self.assertEqual(1,overview['counts']['open'])
        code,_,_=self.request('POST','/api/quality/evaluations',{'top_k':5});self.assertEqual(400,code)
        code,headers,js=self.request('GET','/quality.js');self.assertEqual(200,code)


if __name__=='__main__':unittest.main()
