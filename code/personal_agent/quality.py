"""Human feedback, versioned retrieval cases and offline baseline comparisons."""
import json
import sqlite3
from pathlib import Path
import threading
from time import perf_counter
from uuid import uuid4
from .personal_store import PersonalStore, checked_text, now
from .knowledge import fields, version, terms
from .errors import AgentError
from .retrieval import rank, prepare, fingerprint, REVISION, BASELINE

CATEGORIES={'retrieval','citation','intent','context','execution','other'}
STATES={'new','investigating','retest','closed'}


class QualityStore(PersonalStore):
    def __init__(self, data_dir, store, library):
        self.path=Path(data_dir)/'quality.sqlite3';self.store=store;self.library=library
        self.run_lock=threading.Lock()
        with self.connection() as c:
            for table in ('feedback','cases'):
                c.execute(f'CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY,body TEXT NOT NULL,version INTEGER NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS answer_runs(message_id INTEGER PRIMARY KEY,run_id TEXT)')

    def rows(self, table, limit=None):
        assert table in ('feedback','cases','evaluations')
        with self.connection() as c:
            return [dict(json.loads(r['body']),id=r['id'],**({'version':r['version']} if table!='evaluations' else {})) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid DESC'+(' LIMIT ?' if limit else ''),(limit,) if limit else ())]

    def row(self, table, identifier):
        assert table in ('feedback','cases','evaluations')
        with self.connection() as c:row=c.execute(f'SELECT * FROM {table} WHERE id=?',(identifier,)).fetchone()
        if row is None:raise AgentError('not_found','记录不存在')
        return dict(json.loads(row['body']),id=row['id'],**({'version':row['version']} if table!='evaluations' else {}))

    def save(self, table, body, identifier=None, expected_version=None):
        assert table in ('feedback','cases')
        with self.connection() as c:
            if identifier:
                version(expected_version)
                if not c.execute(f'UPDATE {table} SET body=?,version=version+1 WHERE id=? AND version=?',(json.dumps(body,ensure_ascii=False),identifier,expected_version)).rowcount:
                    raise AgentError('version_conflict','记录已更新，请刷新后再保存')
            else:
                identifier=f"message-{body['message_id']}" if table=='feedback' else uuid4().hex
                try:c.execute(f'INSERT INTO {table} VALUES(?,?,1)',(identifier,json.dumps(body,ensure_ascii=False)))
                except sqlite3.IntegrityError:raise AgentError('version_conflict','反馈已保存，请刷新后编辑') from None
        return self.row(table,identifier)

    def record_run(self, message_id, run_id):
        if run_id:
            with self.connection() as c:c.execute('INSERT OR REPLACE INTO answer_runs VALUES(?,?)',(message_id,run_id))

    def feedback(self, body):
        fields(body,{'session_id','message_id','rating','category','note','expected_version'},{'session_id','message_id','rating'})
        if not isinstance(body['session_id'],str) or type(body['message_id']) is not int:raise AgentError('validation_error','请选择真实的助手回复')
        if body['rating'] not in ('helpful','issue') or body.get('category','other') not in CATEGORIES:raise AgentError('validation_error','反馈类型无效')
        note=body.get('note','')
        if not isinstance(note,str) or len(note)>2000:raise AgentError('validation_error','反馈说明最多2000字')
        with self.store.connection() as c:
            message=c.execute("SELECT * FROM messages WHERE id=? AND session_id=? AND role='assistant'",(body['message_id'],body['session_id'])).fetchone()
            if not message:raise AgentError('not_found','助手回复不存在或不属于当前对话')
            question=c.execute("SELECT content FROM messages WHERE session_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1",(body['session_id'],body['message_id'])).fetchone()
        existing=next((f for f in self.rows('feedback') if f['message_id']==body['message_id']),None)
        if existing:
            version(body.get('expected_version'))
            if body['expected_version']!=existing['version']:raise AgentError('version_conflict','反馈已变化，请刷新')
            data={k:v for k,v in existing.items() if k not in ('id','version')}
            data['history']=(data.get('history',[])+[{k:data.get(k) for k in ('rating','category','note','status','diagnosis','fix','verification','updated_at')}])[-30:]
            if body['rating']=='issue' and (data['rating']!='issue' or data['note']!=note or data['category']!=body.get('category','other')):
                data.update(status='new',diagnosis='',fix='',verification='')
        else:
            with self.connection() as c:run=c.execute('SELECT run_id FROM answer_runs WHERE message_id=?',(body['message_id'],)).fetchone()
            data={'session_id':body['session_id'],'message_id':body['message_id'],'question':question['content'] if question else '',
                  'answer':message['content'],'sources':self.library.answer_sources(body['message_id']),
                  'project_id':self.library.session_project(body['session_id']),'run_id':run['run_id'] if run else None,
                  'status':'new','diagnosis':'','fix':'','verification':'','created_at':now()}
        data.update(rating=body['rating'],category=body.get('category','other'),note=note,updated_at=now())
        return self.save('feedback',data,existing['id'] if existing else None,body.get('expected_version'))

    def update_feedback(self, identifier, body):
        fields(body,{'expected_version','status','diagnosis','fix','verification'},{'expected_version','status','diagnosis','fix','verification'})
        old=self.row('feedback',identifier)
        if body['status'] not in STATES:raise AgentError('validation_error','处理状态无效')
        for key in ('diagnosis','fix','verification'):
            if not isinstance(body[key],str) or len(body[key])>2000:raise AgentError('validation_error','处理说明最多2000字')
        if body['status'] in ('retest','closed') and not (body['diagnosis'].strip() and body['fix'].strip()):raise AgentError('validation_error','请先填写问题原因和改进措施')
        if body['status']=='closed' and not body['verification'].strip():raise AgentError('validation_error','关闭前请记录实际复测结果；不会自动判为解决')
        value={k:v for k,v in old.items() if k not in ('id','version')}
        value['history']=(value.get('history',[])+[{k:old.get(k) for k in ('status','diagnosis','fix','verification','updated_at')}])[-30:]
        value.update({k:v for k,v in body.items() if k!='expected_version'});value['updated_at']=now()
        return self.save('feedback',value,identifier,body['expected_version'])

    def save_case(self, body, identifier=None):
        if identifier and set(body)=={'expected_version','active'}:
            if type(body['active']) is not bool:raise AgentError('validation_error','用例状态无效')
            old=self.row('cases',identifier);value={k:v for k,v in old.items() if k not in ('id','version')}
            value.update(active=body['active'],updated_at=now())
            return self.save('cases',value,identifier,body['expected_version'])
        allowed={'title','query','project_id','expected_source_ids','kind','reference_answer','feedback_id','expected_version','active'}
        fields(body,allowed,{'title','query','expected_source_ids','kind'})
        title=checked_text(body['title'],'用例名称',100);query=checked_text(body['query'],'检索问题',300)
        if not terms(query):raise AgentError('validation_error','问题需要有效的检索关键词')
        project_id=body.get('project_id')
        if project_id is not None:self.library.project(project_id)
        expected=body['expected_source_ids'];kind=body['kind'];active=body.get('active',True)
        if kind not in ('answerable','unanswerable') or type(active) is not bool or not isinstance(expected,list) or len(expected)>10 or any(not isinstance(i,str) or not i for i in expected) or len(set(expected))!=len(expected):raise AgentError('validation_error','用例类型或预期资料无效')
        if (kind=='answerable' and not expected) or (kind=='unanswerable' and expected):raise AgentError('validation_error','应找到资料时请选择预期资料；无相关资料用例应留空')
        references=[]
        for sid in expected:
            source=self.library.source(sid)
            if project_id and source['project_id']!=project_id:raise AgentError('validation_error','预期资料不属于所选项目')
            references.append({'id':sid,'version':source['version'],'title':source['title']})
        answer=body.get('reference_answer','')
        if not isinstance(answer,str) or len(answer)>3000:raise AgentError('validation_error','参考答案最多3000字')
        fid=body.get('feedback_id')
        if fid:
            feedback=self.row('feedback',fid)
            if feedback['project_id']!=project_id:raise AgentError('validation_error','用例项目必须与原反馈一致')
        value={'title':title,'query':query,'project_id':project_id,'expected':references,'kind':kind,'reference_answer':answer,
               'feedback_id':fid,'active':active,'updated_at':now()}
        return self.save('cases',value,identifier,body.get('expected_version'))

    def evaluate(self, body):
        fields(body,{'top_k'},{'top_k'})
        k=body['top_k']
        if type(k) is not int or not 1<=k<=10:raise AgentError('validation_error','返回资料数须为1至10')
        if not self.run_lock.acquire(False):raise AgentError('conflict','评测正在运行，请稍后查看结果')
        try:
            cases=[r for r in self.rows('cases') if r['active']]
            if not cases:raise AgentError('validation_error','请先添加并确认检索用例')
            if len(cases)>50:raise AgentError('validation_error','一次最多运行50条启用用例，请先停用多余用例')
            corpus=self.library.corpus()
            if len(corpus)>500:raise AgentError('validation_error','当前离线评测最多支持500条资料')
            by_id={s['id']:s for s in corpus}
            for case in cases:
                if case['project_id']:self.library.project(case['project_id'])
                for expected in case['expected']:
                    source=by_id.get(expected['id'])
                    if not source or source['version']!=expected['version'] or case['project_id'] and source['project_id']!=case['project_id']:
                        raise AgentError('case_needs_review','预期资料已修改、移动或删除，请打开用例核对资料并重新保存')
            grouped={p:[s for s in corpus if not p or s['project_id']==p] for p in {c['project_id'] for c in cases}}
            indexes={p:prepare(rows) for p,rows in grouped.items()};engines={}
            for method in (BASELINE,REVISION):
                results=[];start=perf_counter()
                for case in cases:
                    t=perf_counter();rows,_=rank(grouped[case['project_id']],case['query'],k,method, indexes[case['project_id']])
                    ids=[r['id'] for r in rows];expected={r['id'] for r in case['expected']};hits=len(expected.intersection(ids))
                    reciprocal=next((1/(i+1) for i,sid in enumerate(ids) if sid in expected),0)
                    results.append({'case_id':case['id'],'case_version':case['version'],'title':case['title'],'query':case['query'],'kind':case['kind'],
                        'expected':case['expected'],'reference_answer':case.get('reference_answer',''),'retrieved':[{'id':r['id'],'title':r['title'],'version':r['version'],'excerpt':r['excerpt'],'chunk':r.get('chunk')} for r in rows],
                        'hit':hits>0,'recall':hits/len(expected) if expected else None,'reciprocal_rank':reciprocal,
                        'passed':hits==len(expected) if expected else not rows,'milliseconds':round((perf_counter()-t)*1000,3)})
                positives=[r for r in results if r['kind']=='answerable'];negatives=[r for r in results if r['kind']=='unanswerable']
                mean=lambda values:sum(values)/len(values) if values else None
                engines[method]={'cases':results,'metrics':{'count':len(results),'positive_count':len(positives),'negative_count':len(negatives),
                    'hit_at_k':mean([r['hit'] for r in positives]),'recall_at_k':mean([r['recall'] for r in positives]),
                    'mrr':mean([r['reciprocal_rank'] for r in positives]),'empty_result_rate':mean([r['passed'] for r in negatives]),
                    'passed':sum(r['passed'] for r in results),'milliseconds':round((perf_counter()-start)*1000,3)}}
            identifier=uuid4().hex
            value={'created_at':now(),'top_k':k,'corpus_revision':fingerprint(corpus),'case_revisions':{c['id']:c['version'] for c in cases},
                   'engines':engines,'notice':'仅评测检索结果，不调用模型；不代表回答正确率或实际任务完成率。'}
            with self.connection() as c:c.execute('INSERT INTO evaluations VALUES(?,?)',(identifier,json.dumps(value,ensure_ascii=False)))
            return dict(value,id=identifier)
        finally:self.run_lock.release()

    def overview(self):
        feedback=self.rows('feedback');cases=self.rows('cases');runs=self.rows('evaluations',10)
        corpus=self.library.corpus();revision=fingerprint(corpus);case_versions={c['id']:c['version'] for c in cases if c['active']}
        for run in runs:run['stale']=run['corpus_revision']!=revision or run['case_revisions']!=case_versions
        return {'feedback':feedback[:100],'cases':cases,'evaluations':runs,'projects':self.library.projects(),
                'source_choices':[{k:r[k] for k in ('id','title','version','project_id')} for r in corpus],
                'counts':{'feedback':len(feedback),'open':sum(f['rating']=='issue' and f['status']!='closed' for f in feedback),'cases':len(cases)}}

    def route(self, method, path, query, body):
        fields(query,set());parts=path.strip('/').split('/')
        if path=='/api/quality' and method=='GET':return self.overview()
        if path=='/api/quality/feedback' and method=='POST':return {'feedback':self.feedback(body)}
        if path=='/api/quality/cases' and method=='POST':return {'case':self.save_case(body)}
        if len(parts)==4 and parts[2]=='feedback' and method=='PATCH':return {'feedback':self.update_feedback(parts[3],body)}
        if len(parts)==4 and parts[2]=='cases' and method=='PATCH':return {'case':self.save_case(body,parts[3])}
        if path=='/api/quality/evaluations' and method=='POST':return {'evaluation':self.evaluate(body)}
        raise AgentError('not_found','找不到评测接口')
