"""Explicit CalDAV synchronization with ETag guards and a durable write journal."""
from datetime import datetime,date,timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin,urlsplit
from uuid import uuid4
from .private_config import PrivateConfig
from .personal_store import PersonalStore,now
from .api_model import base_url
from .errors import AgentError
from .timeutils import zone,iso,parse_datetime,interval

FIELDS=('title','start','end','all_day','location','description')
def anchor_key(value):
    if isinstance(value,datetime):
        if value.tzinfo is None:raise AgentError('calendar_unavailable','重复实例标识缺少时区')
        return iso(value)
    return value.isoformat()
def digest(event):return hashlib.sha256(json.dumps({k:event.get(k) for k in FIELDS},sort_keys=True).encode()).hexdigest()
def secret_http_url(value):
    # CalDAV is not a Chat Completions endpoint: retain its full path.
    checked=base_url(value)
    if checked!=value.rstrip('/'):raise AgentError('validation_error','日历地址格式无效')
    return value.rstrip('/')+'/' if value else ''

class CalDAVSync(PersonalStore):
    def __init__(self,data_dir,calendar,client_factory=None):
        self.path=Path(data_dir)/'caldav.sqlite3';self.path.parent.mkdir(parents=True,exist_ok=True);self.calendar=calendar;self.factory=client_factory
        self.config=PrivateConfig(Path(data_dir)/'caldav.json',{'url':'','username':'','password':'','calendar_url':'','enabled':False,'writes_enabled':False},secrets={'password'})
        with self.connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS mappings(profile TEXT,local_id TEXT,remote_url TEXT,uid TEXT,etag TEXT,local_digest TEXT,PRIMARY KEY(profile,local_id))')
            c.execute('CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,profile TEXT,local_id TEXT,remote_url TEXT,kind TEXT,body TEXT,etag TEXT,status TEXT,error TEXT,created_at TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY,profile TEXT,local_id TEXT,remote_url TEXT,reason TEXT,remote TEXT,etag TEXT,local_version TEXT,created_at TEXT)')
            c.execute("UPDATE operations SET status='unknown' WHERE status='sending'")
    def settings(self):return self.config.public()
    def save_settings(self,body):
        def validate(v,old,raw):
            for key in ('enabled','writes_enabled'):
                if type(v[key]) is not bool:raise AgentError('validation_error','日历启用状态无效')
            for key in ('url','calendar_url'):
                if not isinstance(v[key],str) or len(v[key])>1500:raise AgentError('validation_error','日历地址无效')
                if v[key]:v[key]=secret_http_url(v[key])
            if not isinstance(v['username'],str) or len(v['username'])>300 or not isinstance(v['password'],str) or len(v['password'])>2000:raise AgentError('validation_error','日历凭据无效')
            if (v['url']!=old['url'] or v['username']!=old['username']) and old['password'] and not raw.get('password') and not raw.get('clear_password'):raise AgentError('validation_error','更换日历地址或账号时请重新填写或清除密码')
            if v['calendar_url'] and v['url'] and urlsplit(v['calendar_url']).netloc!=urlsplit(v['url']).netloc:raise AgentError('validation_error','所选日历必须与服务地址属于同一主机')
            if v['enabled'] and not (v['url'] and v['calendar_url']):raise AgentError('validation_error','启用同步前请测试连接并选择日历')
        return self.config.save(body,validate)
    def profile(self,cfg):return hashlib.sha256(json.dumps([cfg['url'],cfg['username'],cfg['calendar_url']]).encode()).hexdigest()[:24]
    def client(self,cfg):
        if not cfg['url']:raise AgentError('validation_error','请先保存 CalDAV 地址和账号')
        if self.factory:return self.factory(cfg)
        try:
            import caldav
            client=caldav.DAVClient(url=cfg['url'],username=cfg['username'] or None,password=cfg['password'] or None,timeout=15,ssl_verify_cert=True,enable_rfc6764=False,auth_type='basic',rate_limit_handle=False,require_tls=urlsplit(cfg['url']).scheme=='https')
            # Never retry conditional mutations on 429 or redirects.
            client.rate_limit_handle=False
            client.session.trust_env=False
            original_request=client.session.request
            def request(method,url,**kwargs):
                target=urlsplit(str(url));origin=urlsplit(cfg['url'])
                if (target.scheme,target.netloc)!=(origin.scheme,origin.netloc):raise AgentError('calendar_unavailable','日历资源跳转到其他主机，未发送凭据')
                kwargs['allow_redirects']=False
                return original_request(method,url,**kwargs)
            client.session.request=request
            return client
        except ImportError:raise AgentError('dependency_missing','请使用项目 .venv 启动以启用 CalDAV') from None
    def test(self):
        cfg=self.config.read();client=self.client(cfg)
        try:
            calendars=client.principal().calendars();items=[]
            for calendar in calendars:
                url=str(calendar.url)
                if urlsplit(url).netloc!=urlsplit(cfg['url']).netloc:continue
                items.append({'url':url,'name':str(calendar.name or '未命名日历')})
            return {'connected':True,'calendars':items,'notice':'只读取了日历列表，未写入日程。'}
        except Exception:raise AgentError('calendar_unavailable','CalDAV 登录或读取失败，请核对地址、应用专用密码与网络') from None
        finally:
            if hasattr(client,'close'):client.close()
    def rows(self,table,profile):
        if table not in ('mappings','operations','conflicts'):raise ValueError()
        with self.connection() as c:return [dict(r) for r in c.execute('SELECT * FROM '+table+' WHERE profile=?',(profile,))]
    def status(self):
        cfg=self.config.read();profile=self.profile(cfg)
        return {'settings':self.settings(),'conflicts':[dict(r,remote=json.loads(r['remote']) if r['remote'] else None) for r in self.rows('conflicts',profile)],'operations':[{k:r[k] for k in ('id','local_id','kind','status','error','created_at')} for r in self.rows('operations',profile)][-50:]}
    def decode(self,resource,cfg):
        from icalendar import Calendar
        raw=resource.data
        if isinstance(raw,str):raw=raw.encode()
        if len(raw)>1024*1024:raise AgentError('calendar_unavailable','远端单条日程过大')
        parsed=Calendar.from_ical(raw);components=[x for x in parsed.walk() if x.name=='VEVENT']
        if len(components)!=1:raise AgentError('calendar_unavailable','远端日程未展开为单次实例')
        item=components[0];uid=str(item.get('UID',''))
        if not uid:raise AgentError('calendar_unavailable','远端日程缺少 UID')
        a=item.decoded('DTSTART');b=item.decoded('DTEND',None);all_day=isinstance(a,date) and not isinstance(a,datetime);tz=zone(self.calendar.call('calendar_get_preferences',{})['preferences']['timezone'])
        if all_day:
            a=datetime.combine(a,datetime.min.time(),tzinfo=tz);b=datetime.combine(b or (a.date()+timedelta(days=1)),datetime.min.time(),tzinfo=tz)
        if b is None:b=a+item.decoded('DURATION',timedelta(hours=1))
        if a.tzinfo is None or b.tzinfo is None:raise AgentError('calendar_unavailable','远端日程缺少时区，拒绝猜测')
        if b<=a:raise AgentError('calendar_unavailable','远端日程时间无效')
        recurrence=item.get('RECURRENCE-ID');anchor=anchor_key(item.decoded('RECURRENCE-ID')) if recurrence else ''
        repeating=bool(item.get('RRULE') or recurrence)
        etag=str(getattr(resource,'props',{}).get('{DAV:}getetag','') or '')
        return {'title':str(item.get('SUMMARY','未命名日程'))[:200],'start':iso(a),'end':iso(b),'all_day':all_day,'location':str(item.get('LOCATION',''))[:1000],'description':str(item.get('DESCRIPTION',''))[:5000],'uid':uid,'anchor':anchor,'repeating':repeating,'remote_url':str(resource.url),'etag':etag}
    def _get(self,client,url):
        response=client.request(url,'GET')
        if response.status==404:return None
        if response.status!=200:raise AgentError('calendar_unavailable','无法核查远端日程，未修改本地数据')
        class Resource:pass
        resource=Resource();resource.data=response.raw;resource.url=url;resource.props={'{DAV:}getetag':response.headers.get('Etag',response.headers.get('ETag',''))}
        return resource
    def _local(self,eid):
        try:return self.calendar.call('calendar_get_event',{'event_id':eid})['event']
        except AgentError as e:
            if e.code=='not_found':return None
            raise
    def _occurrence_absent(self,resource,anchor,a,b):
        from icalendar import Calendar
        import recurring_ical_events
        events=recurring_ical_events.of(Calendar.from_ical(resource.data)).between(a,b)
        return anchor not in {anchor_key(e.decoded('RECURRENCE-ID',e.decoded('DTSTART'))) for e in events}
    def _remove_mirror(self,local,profile):
        with self.calendar.storage.connection(immediate=True) as c:
            current=c.execute('SELECT body FROM events WHERE id=?',(local['id'],)).fetchone()
            if not current or json.loads(current['body'])['version']!=local['version']:raise AgentError('version_conflict','本地日程已变化，未删除')
            c.execute('DELETE FROM events WHERE id=?',(local['id'],))
        with self.connection() as c:c.execute('DELETE FROM mappings WHERE profile=? AND local_id=?',(profile,local['id']))
    def _conflict(self,profile,local_id,url,reason,remote,etag,local_version):
        cid=hashlib.sha256((profile+local_id).encode()).hexdigest()
        with self.connection() as c:c.execute('INSERT OR REPLACE INTO conflicts VALUES(?,?,?,?,?,?,?,?,?)',(cid,profile,local_id,url,reason,json.dumps(remote) if remote else None,etag,local_version,now()))
    def _mirror(self,remote,cfg,profile,local=None):
        eid=local['id'] if local else 'caldav-'+hashlib.sha256((profile+remote['uid']+remote['anchor']).encode()).hexdigest()[:32]
        body={k:remote[k] for k in FIELDS};body.update(id=eid,calendar_id='local',timezone=self.calendar.call('calendar_get_preferences',{})['preferences']['timezone'],version=str(int(local['version'])+1) if local else '1',recurring=False,reminder_minutes=None,external={'provider':'caldav','profile':profile,'readonly':not cfg['writes_enabled'] or remote['repeating'] or not remote['etag'],'recurring':remote['repeating'],'anchor':remote['anchor'],'remote_digest':digest(remote)})
        with self.calendar.storage.connection(immediate=True) as c:
            current=c.execute('SELECT body FROM events WHERE id=?',(eid,)).fetchone()
            if current and (not local or json.loads(current['body'])['version']!=local['version']):raise AgentError('version_conflict','同步时本地日程已变化，未覆盖')
            c.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?)',(eid,'local',parse_datetime(body['start'],zone('UTC')).timestamp(),parse_datetime(body['end'],zone('UTC')).timestamp(),json.dumps(body)))
        with self.connection() as c:c.execute('INSERT OR REPLACE INTO mappings VALUES(?,?,?,?,?,?)',(profile,eid,remote['remote_url'],remote['uid'],remote['etag'],digest(body)))
        return body
    def _push(self,client,cfg,profile,local,mapping,delete=False):
        from icalendar import Calendar,Event
        uid=mapping['uid'] if mapping else 'personal-agent-'+local['id']+'@local'
        url=mapping['remote_url'] if mapping else urljoin(cfg['calendar_url'],uid+'.ics')
        if urlsplit(url).netloc!=urlsplit(cfg['url']).netloc:raise AgentError('validation_error','远端资源主机与所选账号不一致')
        etag=mapping['etag'] if mapping else ''
        if mapping and not etag:raise AgentError('unsupported_capability','远端未返回 ETag，无法安全修改或删除')
        body=''
        if not delete:
            calendar=Calendar();calendar.add('prodid','-//Personal Agent//CalDAV//');calendar.add('version','2.0');event=Event();event.add('uid',uid)
            tz=zone(local.get('timezone','Asia/Shanghai'))
            event.add('dtstart',parse_datetime(local['start'],tz).astimezone(tz).date() if local['all_day'] else parse_datetime(local['start'],tz))
            event.add('dtend',parse_datetime(local['end'],tz).astimezone(tz).date() if local['all_day'] else parse_datetime(local['end'],tz))
            for key,ical in (('title','summary'),('location','location'),('description','description')):event.add(ical,local[key])
            calendar.add_component(event);body=calendar.to_ical().decode()
        lid=mapping['local_id'] if delete else local['id'];opid=hashlib.sha256(json.dumps([profile,lid,'delete' if delete else digest(local),etag]).encode()).hexdigest()
        with self.connection() as c:
            old=c.execute('SELECT status FROM operations WHERE id=?',(opid,)).fetchone()
            if old:return old['status']
            c.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)',(opid,profile,lid,url,'delete' if delete else 'put',body,etag,'sending','',now()))
        status='unknown';error='远端结果未确认，需核查，不会自动重发'
        try:
            headers={'If-Match':etag} if mapping else {'If-None-Match':'*'}
            response=client.delete(url,headers=headers) if delete else client.put(url,body,headers=dict(headers,**{'Content-Type':'text/calendar; charset=utf-8'}))
            if response.status==412:
                status='conflict';error='远端版本已变化，未覆盖'
                self._conflict(profile,lid,url,error,None,etag,local.get('version') if local else None)
            elif response.status in (200,201,204):
                if delete:
                    with self.connection() as c:c.execute('DELETE FROM mappings WHERE profile=? AND local_id=?',(profile,lid))
                else:
                    resource=self._get(client,url)
                    if not resource:raise ValueError()
                    remote=self.decode(resource,cfg)
                    if digest(remote)!=digest(local):raise ValueError()
                    with self.connection() as c:c.execute('INSERT OR REPLACE INTO mappings VALUES(?,?,?,?,?,?)',(profile,lid,url,uid,remote['etag'],digest(local)))
                status='applied';error=''
            elif 400<=response.status<500 and response.status not in (408,429):status='failed';error='远端拒绝写入（HTTP %s）'%response.status
        except Exception:pass
        with self.connection() as c:c.execute('UPDATE operations SET status=?,error=? WHERE id=?',(status,error,opid))
        return status
    def sync(self,start,end):
        cfg=self.config.read()
        if not cfg['enabled']:raise AgentError('validation_error','CalDAV 同步尚未启用')
        a,b=interval(start,end,zone(self.calendar.call('calendar_get_preferences',{})['preferences']['timezone']));profile=self.profile(cfg);client=self.client(cfg)
        try:
            resource_calendar=client.calendar(url=cfg['calendar_url']);resources=resource_calendar.search(start=a,end=b,event=True,expand=True)
            if len(resources)>200:raise AgentError('validation_error','远端超过 200 条，请缩短同步范围')
            remote_events=[self.decode(r,cfg) for r in resources]
            # Unknown writes require explicit read-only reconciliation. Do not import
            # their resource under a second local ID or issue another mutation.
            uncertain={o['remote_url'] for o in self.rows('operations',profile) if o['status'] in ('unknown','sending')}
            mappings=self.rows('mappings',profile);by_key={(m['uid'],m['remote_url']):m for m in mappings};seen=set();imported=0
            for remote in remote_events:
                if remote['remote_url'] in uncertain:continue
                if urlsplit(remote['remote_url']).netloc!=urlsplit(cfg['url']).netloc:raise AgentError('calendar_unavailable','远端混入其他主机资源')
                eid='caldav-'+hashlib.sha256((profile+remote['uid']+remote['anchor']).encode()).hexdigest()[:32]
                mapping=next((m for m in mappings if m['local_id']==eid),None) or (by_key.get((remote['uid'],remote['remote_url'])) if not remote['repeating'] else None)
                local=self._local(mapping['local_id']) if mapping else self._local(eid)
                if mapping:seen.add(mapping['local_id'])
                if not mapping and local and digest(local)!=local.get('external',{}).get('remote_digest'):
                    self._conflict(profile,local['id'],remote['remote_url'],'本地导入记录已修改，映射需核查',remote,remote['etag'],local['version']);continue
                if mapping and local and digest(local)!=mapping['local_digest']:
                    if remote['etag']!=mapping['etag']:self._conflict(profile,local['id'],remote['remote_url'],'本地与远端均已修改',remote,remote['etag'],local['version'])
                    elif cfg['writes_enabled']:self._push(client,cfg,profile,local,mapping)
                    continue
                if mapping and not local:
                    if cfg['writes_enabled']:self._push(client,cfg,profile,None,mapping,delete=True)
                    continue
                if not local or digest(local)!=digest(remote) or local.get('external',{}).get('readonly')!=(not cfg['writes_enabled'] or remote['repeating'] or not remote['etag']):self._mirror(remote,cfg,profile,local);imported+=1
                elif mapping and mapping['etag']!=remote['etag']:
                    with self.connection() as c:c.execute('UPDATE mappings SET etag=? WHERE profile=? AND local_id=?',(remote['etag'],profile,local['id']))
            # A missing search result is not proof of deletion: GET each mapped resource first.
            for mapping in mappings:
                if mapping['remote_url'] in uncertain:continue
                if mapping['local_id'] in seen:continue
                local=self._local(mapping['local_id'])
                if not local or not (parse_datetime(local['start'],zone('UTC'))<b and parse_datetime(local['end'],zone('UTC'))>a):continue
                resource=self._get(client,mapping['remote_url'])
                absent=resource is None or (local.get('external',{}).get('recurring') and self._occurrence_absent(resource,local['external']['anchor'],a,b))
                if absent:
                    if digest(local)!=mapping['local_digest']:self._conflict(profile,local['id'],mapping['remote_url'],'远端已删除，本地有修改',None,'',local['version']);continue
                    self._remove_mirror(local,profile)
            if cfg['writes_enabled']:
                mapped={m['local_id'] for m in self.rows('mappings',profile)}
                for local in self.calendar.call('calendar_list_events',{'start':start,'end':end})['events']:
                    if local['id'] not in mapped and not local.get('external') and not local.get('recurring'):self._push(client,cfg,profile,local,None)
            return dict(self.status(),imported=imported,notice='同步完成，请核对冲突和待核查操作；通知送达不属于此结果。')
        except AgentError:raise
        except Exception:raise AgentError('calendar_unavailable','CalDAV 同步未完成；已有写入请查看操作记录，不会自动重发') from None
        finally:
            if hasattr(client,'close'):client.close()
    def reconcile(self,operation_id):
        cfg=self.config.read();profile=self.profile(cfg)
        operation=next((o for o in self.rows('operations',profile) if o['id']==operation_id),None)
        if not operation:raise AgentError('not_found','找不到操作')
        client=self.client(cfg)
        try:
            resource=self._get(client,operation['remote_url']);confirmed=resource is None and operation['kind']=='delete'
            if resource and operation['kind']=='put':
                remote=self.decode(resource,cfg)
                class Saved:pass
                saved=Saved();saved.data=operation['body'];saved.url=operation['remote_url'];saved.props={}
                confirmed=digest(remote)==digest(self.decode(saved,cfg))
                if confirmed:
                    local=self._local(operation['local_id'])
                    with self.connection() as c:c.execute('INSERT OR REPLACE INTO mappings VALUES(?,?,?,?,?,?)',(profile,operation['local_id'],operation['remote_url'],remote['uid'],remote['etag'],digest(self.decode(saved,cfg))))
            if confirmed:
                with self.connection() as c:c.execute("UPDATE operations SET status='applied',error='' WHERE id=?",(operation_id,))
                if operation['kind']=='delete':
                    with self.connection() as c:c.execute('DELETE FROM mappings WHERE profile=? AND local_id=?',(profile,operation['local_id']))
            return {'confirmed':confirmed,'notice':'只核查远端状态，未重新发送写入。','status':self.status()}
        finally:
            if hasattr(client,'close'):client.close()
    def resolve(self,conflict_id,choice,expected_version):
        cfg=self.config.read();profile=self.profile(cfg);conflict=next((c for c in self.rows('conflicts',profile) if c['id']==conflict_id),None)
        if not conflict:raise AgentError('not_found','冲突不存在')
        local=self._local(conflict['local_id'])
        if (local or {}).get('version')!=expected_version:raise AgentError('version_conflict','本地日程已变化，请重新同步')
        client=self.client(cfg)
        try:
            resource=self._get(client,conflict['remote_url']);remote=self.decode(resource,cfg) if resource else None
            if remote and remote['etag']!=conflict['etag']:raise AgentError('version_conflict','远端版本再次变化，请重新同步')
            if choice=='remote':
                if remote:self._mirror(remote,cfg,profile,local)
                else:
                    with self.calendar.storage.connection(immediate=True) as c:c.execute('DELETE FROM events WHERE id=?',(conflict['local_id'],))
                    with self.connection() as c:c.execute('DELETE FROM mappings WHERE profile=? AND local_id=?',(profile,conflict['local_id']))
            elif choice=='local':
                if not cfg['writes_enabled']:raise AgentError('validation_error','尚未启用远端写入')
                mapping=next((m for m in self.rows('mappings',profile) if m['local_id']==conflict['local_id']),None)
                if mapping and remote:mapping['etag']=remote['etag']
                if local is None:
                    if remote and not mapping:raise AgentError('validation_error','缺少同步映射，请重新同步核查')
                    result=self._push(client,cfg,profile,None,mapping,delete=True) if remote else 'applied'
                else:result=self._push(client,cfg,profile,local,mapping if remote else None)
                if result!='applied':raise AgentError('unknown_outcome','写入未确认，请核查操作记录')
            else:raise AgentError('validation_error','请选择保留本地或采用远端')
            with self.connection() as c:c.execute('DELETE FROM conflicts WHERE id=?',(conflict_id,))
            return self.status()
        finally:
            if hasattr(client,'close'):client.close()
