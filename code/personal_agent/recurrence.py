"""Finite local recurring series with transactional edits and exception instances."""
from datetime import datetime,timedelta
import hashlib
import json
from uuid import uuid4
from .errors import AgentError
from .timeutils import interval,iso,zone,parse_datetime,localize,overlaps

class Recurrence:
    def __init__(self,calendar):
        self.calendar=calendar
        with calendar.storage.connection(immediate=True) as c:
            c.execute('CREATE TABLE IF NOT EXISTS recurrence_series(id TEXT PRIMARY KEY,body TEXT NOT NULL,version INTEGER NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS recurrence_exceptions(series_id TEXT NOT NULL,anchor TEXT NOT NULL,body TEXT,PRIMARY KEY(series_id,anchor))')
    def occurrences(self,body):
        try:from dateutil.rrule import rrule,DAILY,WEEKLY,MONTHLY
        except ImportError:raise AgentError('dependency_missing','重复日程需要项目 .venv 中的 python-dateutil') from None
        rule=body['recurrence'];tz=zone(body['timezone']);start=parse_datetime(body['start'],tz).astimezone(tz)
        count=rule.get('count');until=rule.get('until');step=rule.get('interval',1)
        if rule.get('frequency') not in ('daily','weekly','monthly') or type(step) is not int or not 1<=step<=12:raise AgentError('validation_error','重复频率或间隔无效')
        if bool(count)==bool(until):raise AgentError('validation_error','重复日程请选择次数或结束日期之一')
        opts={'freq':{'daily':DAILY,'weekly':WEEKLY,'monthly':MONTHLY}[rule['frequency']],'dtstart':start,'interval':step}
        if count is not None:
            if type(count) is not int or not 1<=count<=366:raise AgentError('validation_error','重复次数须为 1 至 366')
            opts['count']=count
        else:
            try:end=datetime.fromisoformat(until).date();edge=localize(datetime.combine(end,datetime.max.time()),tz)
            except (ValueError,TypeError):raise AgentError('validation_error','重复结束日期无效') from None
            if edge<start or edge-start>timedelta(days=730):raise AgentError('validation_error','重复结束日期须在开始后的两年内')
            opts['until']=edge
        days=rule.get('weekdays')
        if days is not None:
            if rule['frequency']!='weekly' or not isinstance(days,list) or not days or any(type(d) is not int or not 0<=d<=6 for d in days) or len(set(days))!=len(days):raise AgentError('validation_error','每周重复的星期须为不重复的 0 至 6')
            opts['byweekday']=days
        duration=parse_datetime(body['end'],tz).astimezone(tz).replace(tzinfo=None)-start.replace(tzinfo=None)
        if duration<=timedelta() or duration>timedelta(days=1):raise AgentError('validation_error','重复安排单次时长不超过一天')
        result=[]
        for candidate in rrule(**opts):
            if len(result)>=366:raise AgentError('validation_error','重复实例超过 366 次，请缩短范围')
            a=localize(candidate.replace(tzinfo=None),tz);b=localize(candidate.replace(tzinfo=None)+duration,tz)
            result.append((iso(a),iso(b)))
        if not result:raise AgentError('validation_error','规则未产生安排')
        return result
    @staticmethod
    def identity(event_id):
        parts=event_id.split(':')
        if len(parts)!=3 or parts[0]!='series':raise AgentError('not_found','重复日程标识无效')
        try:anchor=iso(datetime.fromtimestamp(int(parts[2]),zone('UTC')))
        except (ValueError,OverflowError,OSError):raise AgentError('not_found','重复日程实例无效') from None
        return parts[1],anchor
    def _series(self,c,sid):
        row=c.execute('SELECT * FROM recurrence_series WHERE id=?',(sid,)).fetchone()
        if not row:raise AgentError('not_found','重复系列已删除')
        return row,json.loads(row['body'])
    def _event(self,row,body,anchor,end,override=None):
        event=dict(body);event.pop('recurrence',None)
        event.update(start=anchor,end=end,id='series:'+row['id']+':'+str(int(parse_datetime(anchor,zone('UTC')).timestamp())),version='series-'+str(row['version']),recurring=True,series_id=row['id'],series_version=row['version'],original_start=anchor,recurrence=body['recurrence'])
        if override:event.update(override)
        event['recurring']=True
        return event
    def list_events(self,a,b,calendar_id,c=None):
        if c is None:
            with self.calendar.storage.connection() as conn:return self.list_events(a,b,calendar_id,conn)
        result=[]
        for row in c.execute('SELECT * FROM recurrence_series'):
            body=json.loads(row['body'])
            if body['calendar_id']!=calendar_id:continue
            exceptions={r['anchor']:r['body'] for r in c.execute('SELECT * FROM recurrence_exceptions WHERE series_id=?',(row['id'],))}
            for anchor,end in self.occurrences(body):
                if anchor in exceptions and exceptions[anchor] is None:continue
                override=json.loads(exceptions[anchor]) if anchor in exceptions else None
                event=self._event(row,body,anchor,end,override)
                if overlaps(a,b,parse_datetime(event['start'],zone('UTC')),parse_datetime(event['end'],zone('UTC'))):result.append(event)
        return result
    def get(self,event_id,c=None):
        if c is None:
            with self.calendar.storage.connection() as conn:return self.get(event_id,conn)
        sid,anchor=self.identity(event_id);row,body=self._series(c,sid)
        for start,end in self.occurrences(body):
            if start!=anchor:continue
            exception=c.execute('SELECT body FROM recurrence_exceptions WHERE series_id=? AND anchor=?',(sid,anchor)).fetchone()
            if exception and exception['body'] is None:break
            return self._event(row,body,start,end,json.loads(exception['body']) if exception else None)
        raise AgentError('not_found','该次重复安排不存在或已取消')
    def _conflicts(self,event,c,exclude=None,series_id=None):
        a,b=interval(event['start'],event['end'],zone('UTC'))
        matches=[e for e in self.calendar._events(a,b,event['calendar_id'],c) if e['id']!=exclude and (not series_id or e.get('series_id')!=series_id)]
        if matches:raise AgentError('conflict','重复安排与已有日程冲突，整个操作未写入',{'conflicts':[{k:e[k] for k in ('id','title','start','end')} for e in matches[:10]]})
    def mutate(self,name,args):
        if self.calendar.backend is not self.calendar.storage:raise AgentError('unsupported_capability','当前重复日程仅在本地工作台管理')
        request_id=args.get('request_id')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:raise AgentError('validation_error','请提供本次操作编号')
        fp=hashlib.sha256(json.dumps([name,args],sort_keys=True,allow_nan=False).encode()).hexdigest()
        with self.calendar.storage.connection(immediate=True) as c:
            old=c.execute('SELECT * FROM requests WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['fingerprint']!=fp:raise AgentError('request_id_conflict','请求编号不能用于不同安排')
                if old['status']!='done':raise AgentError('unknown_outcome','此前操作结果待核查，不会重新执行')
                return dict(json.loads(old['result']),replayed=True)
            prefs=self.calendar._preferences(c)
            if name=='calendar_create_event':
                if args.get('calendar_id','local')!='local':raise AgentError('validation_error','重复日程目前只支持本地日历')
                rule=args.get('recurrence')
                if not isinstance(rule,dict) or set(rule)-{'frequency','interval','count','until','weekdays'}:raise AgentError('validation_error','重复规则无效')
                body=self.calendar._normalize(dict(args,calendar_id='local'),prefs);body['recurrence']=rule
                occurrences=self.occurrences(body)
                for start,end in occurrences:self._conflicts(dict(body,start=start,end=end),c)
                # Reject overlaps within the proposed series as well.
                if any(parse_datetime(occurrences[i][1],zone('UTC'))>parse_datetime(occurrences[i+1][0],zone('UTC')) for i in range(len(occurrences)-1)):raise AgentError('conflict','重复实例互相重叠')
                sid=uuid4().hex;c.execute('INSERT INTO recurrence_series VALUES(?,?,1)',(sid,json.dumps(body)))
                first_id='series:'+sid+':'+str(int(parse_datetime(occurrences[0][0],zone('UTC')).timestamp()))
                result={'event':self.get(first_id,c),'occurrences':len(occurrences),'series_id':sid}
            else:
                event=self.get(args['event_id'],c);sid,anchor=self.identity(args['event_id']);row,body=self._series(c,sid)
                if args.get('expected_version')!=event['version']:raise AgentError('version_conflict','重复系列已变化，请重新打开')
                scope=args.get('scope','occurrence')
                if scope not in ('occurrence','series'):raise AgentError('validation_error','请选择本次或整个系列')
                if name=='calendar_delete_event':
                    if scope=='series':c.execute('DELETE FROM recurrence_series WHERE id=?',(sid,));c.execute('DELETE FROM recurrence_exceptions WHERE series_id=?',(sid,))
                    else:c.execute('INSERT OR REPLACE INTO recurrence_exceptions VALUES(?,?,NULL)',(sid,anchor));c.execute('UPDATE recurrence_series SET version=version+1 WHERE id=?',(sid,))
                    result={'deleted':True,'event_id':event['id'],'scope':scope}
                else:
                    changes=args.get('changes')
                    if not isinstance(changes,dict) or not changes or set(changes)-{'title','start','end','location','description','all_day'}:raise AgentError('validation_error','重复安排修改字段无效')
                    updated=self.calendar._normalize(dict(event,**changes),prefs)
                    if scope=='occurrence':
                        self._conflicts(updated,c,exclude=event['id']);c.execute('INSERT OR REPLACE INTO recurrence_exceptions VALUES(?,?,?)',(sid,anchor,json.dumps(updated)))
                    else:
                        original=body.copy();base_a=parse_datetime(body['start'],zone(body['timezone'])).astimezone(zone(body['timezone']))
                        current_a=parse_datetime(event['start'],zone(body['timezone'])).astimezone(zone(body['timezone']))
                        new_a=parse_datetime(updated['start'],zone(body['timezone'])).astimezone(zone(body['timezone']))
                        delta=new_a.replace(tzinfo=None)-current_a.replace(tzinfo=None)
                        base_new=localize(base_a.replace(tzinfo=None)+delta,zone(body['timezone']))
                        duration=parse_datetime(updated['end'],zone('UTC'))-parse_datetime(updated['start'],zone('UTC'))
                        body.update(updated,start=iso(base_new),end=iso(base_new+duration),recurrence=original['recurrence'])
                        # Explicit series edit resets prior exceptions; the form warns before confirmation.
                        for start,end in self.occurrences(body):self._conflicts(dict(body,start=start,end=end),c,series_id=sid)
                        c.execute('UPDATE recurrence_series SET body=? WHERE id=?',(json.dumps(body),sid));c.execute('DELETE FROM recurrence_exceptions WHERE series_id=?',(sid,))
                    c.execute('UPDATE recurrence_series SET version=version+1 WHERE id=?',(sid,))
                    if scope=='occurrence':result={'event':self.get(event['id'],c),'scope':scope}
                    else:
                        first=self.occurrences(body)[0][0];eid='series:'+sid+':'+str(int(parse_datetime(first,zone('UTC')).timestamp()));result={'event':self.get(eid,c),'scope':scope}
            c.execute("INSERT INTO requests(request_id,fingerprint,status,result) VALUES(?,?,'done',?)",(request_id,fp,json.dumps(result)))
            return dict(result,replayed=False)
