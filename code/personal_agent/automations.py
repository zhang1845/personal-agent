"""Durable schedules, in-app inbox, opt-in notifications; no AI calls required."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import logging
import re
from pathlib import Path
import threading
from uuid import uuid4
from .personal_store import PersonalStore,now,checked_text
from .private_config import PrivateConfig
from .errors import AgentError
from .timeutils import zone,parse_datetime
_logger=logging.getLogger("apprise");_logger.addHandler(logging.NullHandler());_logger.propagate=False

class Automations(PersonalStore):
    def __init__(self,data_dir,calendar,store,sender=None):
        self.path=Path(data_dir)/'automations.sqlite3';self.path.parent.mkdir(parents=True,exist_ok=True);self.calendar=calendar;self.store=store;self.sender=sender;self.scheduler=None;self.tick_lock=threading.Lock()
        self.config=PrivateConfig(Path(data_dir)/'notifications.json',{'urls':'','enabled':False},secrets={'urls'})
        with self.connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,body TEXT NOT NULL,version INTEGER NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,dedupe TEXT UNIQUE,job_id TEXT,title TEXT,body TEXT,state TEXT,created_at TEXT,read INTEGER NOT NULL DEFAULT 0)')
            columns={row['name'] for row in c.execute('PRAGMA table_info(notifications)')}
            if 'in_app_only' not in columns:c.execute('ALTER TABLE notifications ADD COLUMN in_app_only INTEGER NOT NULL DEFAULT 0')
            c.execute("UPDATE notifications SET state='unknown' WHERE state='sending'")
    def settings(self):return self.config.public()
    def save_settings(self,body):
        def validate(v,old,raw):
            if type(v['enabled']) is not bool or not isinstance(v['urls'],str) or len(v['urls'])>4000:raise AgentError('validation_error','通知配置无效')
            if v['enabled'] and not v['urls'].strip():raise AgentError('validation_error','请先配置通知地址；也可仅使用工作台消息')
            if v['urls']:
                try:
                    import apprise
                    app=apprise.Apprise()
                    if not all(app.add(url.strip()) for url in v['urls'].splitlines() if url.strip()):raise ValueError()
                except (ImportError,ValueError):raise AgentError('validation_error','通知地址无效，请使用 Apprise 支持的地址格式') from None
        return self.config.save(body,validate)
    def jobs(self):
        with self.connection() as c:return [dict(json.loads(r['body']),id=r['id'],version=r['version']) for r in c.execute('SELECT * FROM jobs ORDER BY id')]
    def validate_job(self,body,job_id=None):
        """Normalize a schedule without writing; shared by forms and chat drafts."""
        allowed={'name','kind','time','enabled','lead_minutes','weekdays','expected_version','run_at','message','in_app_only'}
        if not isinstance(body,dict) or set(body)-allowed:raise AgentError('validation_error','计划字段无效')
        kind=body.get('kind');name=checked_text(body.get('name'),'计划名称',100);enabled=body.get('enabled',False);at=body.get('time','09:00');days=body.get('weekdays',list(range(7)));lead=body.get('lead_minutes',15);in_app_only=body.get('in_app_only',False)
        try:
            if not isinstance(at,str) or not re.fullmatch(r'\d{2}:\d{2}',at):raise ValueError()
            datetime.strptime(at,'%H:%M')
        except (TypeError,ValueError):raise AgentError('validation_error','计划时间须为 HH:MM') from None
        if kind not in ('daily_summary','event_reminders','task_digest','one_time','daily_reminder') or type(enabled) is not bool or type(in_app_only) is not bool or type(lead) is not int or not 0<=lead<=1440 or not isinstance(days,list) or not days or any(type(d) is not int or not 0<=d<=6 for d in days) or len(set(days))!=len(days):raise AgentError('validation_error','计划类型、提醒时间或工作日无效')
        data={'name':name,'kind':kind,'time':at,'enabled':enabled,'lead_minutes':lead,'weekdays':days,'in_app_only':in_app_only}
        if kind=='one_time':
            tz=zone(self.calendar.call('calendar_get_preferences',{})['preferences']['timezone'])
            due=parse_datetime(body.get('run_at'),tz)
            if enabled and due<=datetime.now(timezone.utc):raise AgentError('validation_error','提醒时间已过去，请选择未来时间')
            data['run_at']=due.isoformat();data['message']=checked_text(body.get('message') or name,'提醒内容',1000)
        elif kind=='daily_reminder':
            if 'run_at' in body:raise AgentError('validation_error','重复提醒请使用每日时间与星期')
            data['message']=checked_text(body.get('message') or name,'提醒内容',1000)
        elif 'run_at' in body or 'message' in body:
            raise AgentError('validation_error','仅自定义提醒支持提醒内容')
        if job_id:
            if type(body.get('expected_version')) is not int or body['expected_version']<1:raise AgentError('validation_error','请提供最新计划版本')
            current=next((job for job in self.jobs() if job['id']==job_id),None)
            if not current:raise AgentError('not_found','找不到提醒计划')
            if current['version']!=body['expected_version']:raise AgentError('version_conflict','计划已变化，请重新加载')
        return data
    def save_job(self,body,job_id=None):
        data=self.validate_job(body,job_id)
        with self.connection() as c:
            if job_id:
                if not c.execute('UPDATE jobs SET body=?,version=version+1 WHERE id=? AND version=?',(json.dumps(data),job_id,body['expected_version'])).rowcount:raise AgentError('version_conflict','计划已变化，请重新加载')
            else:job_id=uuid4().hex;c.execute('INSERT INTO jobs VALUES(?,?,1)',(job_id,json.dumps(data)))
        return next(j for j in self.jobs() if j['id']==job_id)
    def delete_job(self,job_id,version):
        with self.connection() as c:
            if type(version) is not int or not c.execute('DELETE FROM jobs WHERE id=? AND version=?',(job_id,version)).rowcount:raise AgentError('version_conflict','计划已变化')
        return {'deleted':True}
    def inbox(self):
        with self.connection() as c:return [dict(r) for r in c.execute('SELECT * FROM notifications ORDER BY created_at DESC,id DESC LIMIT 100')]
    def mark_read(self,nid):
        with self.connection() as c:
            if not c.execute('UPDATE notifications SET read=1 WHERE id=?',(nid,)).rowcount:raise AgentError('not_found','消息不存在')
        return {'read':True}
    def _send(self,title,body,config=None):
        config=config or self.config.read()
        if not config['enabled']:return 'in_app'
        if self.sender:
            if self.sender(title,body,config['urls']) is False:raise AgentError('notification_unknown','通知渠道未确认送达，请先核查，不会自动重发')
            return 'sent'
        try:
            import apprise
            asset=apprise.AppriseAsset();asset.async_mode=False
            app=apprise.Apprise(asset=asset)
            for url in config['urls'].splitlines():
                if url.strip() and not app.add(url.strip()):raise ValueError()
            if not app.notify(body=body,title=title):raise ValueError()
            return 'sent'
        except Exception:raise AgentError('notification_unknown','通知发送结果未确认，请核查目标渠道，不会自动重发') from None
    def emit(self,key,job_id,title,body,in_app_only=False):
        if type(in_app_only) is not bool:raise AgentError('validation_error','通知发送方式无效')
        nid=uuid4().hex
        with self.connection() as c:
            if c.execute('SELECT 1 FROM notifications WHERE dedupe=?',(key,)).fetchone():return None
            c.execute("INSERT INTO notifications(id,dedupe,job_id,title,body,state,created_at,read,in_app_only) VALUES(?,?,?,?,?,'queued',?,0,?)",(nid,key,job_id,title,body,now(),int(in_app_only)))
        return nid
    def drain(self):
        with self.connection() as c:rows=[dict(r) for r in c.execute("SELECT * FROM notifications WHERE state='queued'")]
        for row in rows:
            if datetime.now(timezone.utc)-datetime.fromisoformat(row['created_at'])>timedelta(minutes=5):
                with self.connection() as c:c.execute("UPDATE notifications SET state='expired' WHERE id=? AND state='queued'",(row['id'],))
                continue
            with self.connection() as c:
                if not c.execute("UPDATE notifications SET state='sending' WHERE id=? AND state='queued'",(row['id'],)).rowcount:continue
            try:state='in_app' if row['in_app_only'] else self._send(row['title'],row['body'])
            except AgentError:state='unknown'
            with self.connection() as c:c.execute('UPDATE notifications SET state=? WHERE id=?',(state,row['id']))
    def test_notification(self):
        settings=self.config.read()
        if not settings['enabled']:raise AgentError('validation_error','请先保存并启用通知渠道；未发送测试消息')
        nid=self.emit('manual-test:'+uuid4().hex,'manual','Daynest 通知测试','这是你主动发起的测试消息。')
        self.drain();item=next(n for n in self.inbox() if n['id']==nid)
        if item['state']=='unknown':raise AgentError('notification_unknown','测试结果未确认，请检查通知渠道')
        return {'notification':item}
    def summary(self,kind,instant):
        tz_name=self.calendar.call('calendar_get_preferences',{})['preferences']['timezone'];tz=zone(tz_name);day=instant.astimezone(tz).date();start=day.isoformat();end=(day+timedelta(days=1)).isoformat()
        tasks=[t for t in self.store.list_tasks() if not t['completed']]
        if kind=='task_digest':events=[]
        else:events=self.calendar.call('calendar_list_events',{'start':start,'end':end})['events']
        relevant=[t for t in tasks if t.get('due_date') and t['due_date']<=start]
        lines=[start+' · '+tz_name,('到期/逾期待办 %s 项。'%len(relevant)) if kind=='task_digest' else ('当日日程 %s 项；到期/逾期待办 %s 项。'%(len(events),len(relevant)))]
        lines += [parse_datetime(e['start'],tz).astimezone(tz).strftime('%H:%M')+' '+e['title'] for e in events]
        lines += ['待办：'+t['title'] for t in relevant]
        return '\n'.join(lines)
    def tick(self,instant=None):
        if not self.tick_lock.acquire(False):return
        try:
            tz=zone(self.calendar.call('calendar_get_preferences',{})['preferences']['timezone']);instant=instant or datetime.now(tz);local=instant.astimezone(tz)
            for job in self.jobs():
                if not job['enabled']:continue
                try:
                    if job['kind']=='one_time':
                        due=parse_datetime(job['run_at'],tz);lag=instant.astimezone(timezone.utc)-due
                        if lag<timedelta(0):continue
                        if lag<=timedelta(minutes=5):
                            self.emit(job['id']+':once:'+str(job['version'])+':'+job['run_at'],job['id'],job['name'],job['message'],in_app_only=job.get('in_app_only',False))
                        # Persist completion even when the scheduled time was missed.
                        data={k:v for k,v in job.items() if k not in ('id','version')};data['enabled']=False
                        with self.connection() as c:c.execute('UPDATE jobs SET body=?,version=version+1 WHERE id=? AND version=?',(json.dumps(data),job['id'],job['version']))
                        continue
                    if local.weekday() not in job['weekdays']:continue
                    if job['kind']=='event_reminders':
                        events=self.calendar.call('calendar_list_events',{'start':local.isoformat(),'end':(local+timedelta(minutes=job['lead_minutes']+2)).isoformat()})['events']
                        for event in events:
                            if event['all_day']:continue
                            start=parse_datetime(event['start'],tz);due=start-timedelta(minutes=job['lead_minutes'])
                            if due<=instant and (instant<start or job['lead_minutes']==0) and instant-due<=timedelta(minutes=2):
                                key=job['id']+':'+event['id']+':'+event['start']+':'+str(job['lead_minutes'])
                                self.emit(key,job['id'],'日程提醒：'+event['title'],start.astimezone(tz).strftime('%Y-%m-%d %H:%M')+' · '+str(tz),in_app_only=job.get('in_app_only',False))
                    elif local.strftime('%H:%M')==job['time']:
                        self.emit(job['id']+':'+local.date().isoformat(),job['id'],job['name'],job['message'] if job['kind']=='daily_reminder' else self.summary(job['kind'],instant),in_app_only=job.get('in_app_only',False))
                except AgentError:
                    self.emit('error:'+job['id']+':'+local.date().isoformat(),job['id'],'计划执行失败','日历或任务读取未完成，不能将失败视为没有安排；请检查连接。',in_app_only=job.get('in_app_only',False))
            self.drain()
        finally:self.tick_lock.release()
    def start(self):
        if self.scheduler:return
        from apscheduler.schedulers.background import BackgroundScheduler
        self.scheduler=BackgroundScheduler(daemon=True);self.scheduler.add_job(self.tick,'interval',seconds=30,max_instances=1,coalesce=True,misfire_grace_time=30);self.scheduler.start()
    def close(self):
        if self.scheduler:self.scheduler.shutdown(wait=False);self.scheduler=None
