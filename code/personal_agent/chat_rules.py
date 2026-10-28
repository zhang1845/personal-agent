"""Explicit local chat intents; model routing remains available for complex requests."""
from datetime import datetime, timedelta
import re
from .errors import AgentError
from .timeutils import parse_datetime

URL = re.compile(r'https?://[^\s<>"\'，。；]+')
NUM = r'[0-9零〇一二两三四五六七八九十]+'


def result(reply, actions=None, tools=None, clarify=False):
    return {'reply': reply, 'actions': actions or [], 'tools': tools or [], 'needs_clarification': clarify}


def number(text):
    from .assistant import _number
    return _number(text)


def day_in(message, engine):
    now, tz = engine._now()
    day, _ = engine._day(message, now)
    if day:
        return day, now, tz
    matches = list(re.finditer(r'(下周|本周|这周|周|星期)([一二三四五六日天])', message))
    if len(matches) == 1:
        marker, label = matches[0].groups(); index = '一二三四五六日'.find('日' if label == '天' else label)
        monday = now.date() - timedelta(days=now.weekday())
        day = monday + timedelta(days=index + (7 if marker == '下周' else 0))
        if marker in ('周', '星期') and day < now.date():
            day += timedelta(days=7)
    elif '明早' in message:
        day = now.date() + timedelta(days=1)
    elif '今晚' in message or '今早' in message:
        day = now.date()
    return day, now, tz


def clock_in(message):
    times = list(re.finditer(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', message))
    chinese = list(re.finditer(r'(' + NUM + r')点(?:(半)|(' + NUM + r')分)?', message))
    if len(times) + len(chinese) != 1:
        return None
    match = (times or chinese)[0]
    try:
        if times:
            hour, minute = int(match[1]), int(match[2])
        else:
            hour = number(match[1]); minute = 30 if match[2] else number(match[3]) if match[3] else 0
        parts = list(re.finditer(r'上午|下午|晚上|凌晨|中午|早上|明早|今晚|今早', message))
        if len(parts) > 1:
            return None
        if parts:
            part = parts[0].group()
            if part in ('下午', '晚上', '今晚') and 1 <= hour < 12:
                hour += 12
            elif part == '凌晨' and hour == 12:
                hour = 0
            elif part == '中午' and hour == 1:
                hour = 13
            elif part == '中午' and hour != 12:
                return None
            elif part in ('上午', '早上', '明早', '今早', '凌晨') and hour >= 12:
                return None
            elif part in ('晚上', '今晚') and hour == 12:
                return None
        elif chinese and 1 <= hour <= 12:
            return None  # A bare "三点" does not tell us AM or PM.
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            return None
        return hour, minute
    except ValueError:
        return None


def literal_content(message):
    return bool(re.match(r'^(?:添加)?待办\s*[:：]|^(?:请)?(?:帮我)?记住',message))


def denies_writes(message):
    if literal_content(message):
        return False
    message=re.sub(r'(?:不要|别|不必|无需|暂不|不需要)(?:发送|发)(?:送到)?(?:外部通知|外部渠道|邮件|短信)','',message)
    return bool(re.search(r'(?:不要|别|不必|无需|暂不|不需要).{0,12}(?:保存|归档|导入|同步|创建|添加|安排|提醒|修改|改到|改为|改成|取消|删除|完成|设置|调整|记住|执行)|只(?:查询|查看|读取|预览)|仅(?:查询|查看|读取|预览)',message))


def weekday_selection(message):
    if '工作日' in message:
        return list(range(5))
    if '每周' not in message:
        return None
    if '每周末' in message:
        return [5,6]
    section=message.split('每周',1)[1]
    section=re.split(r'提醒|上午|下午|晚上|凌晨|中午|早上|\d{1,2}:|\d{1,2}点',section,maxsplit=1)[0]
    span=re.search(r'([一二三四五六日天])\s*(?:到|至|-|—)\s*(?:周|星期)?([一二三四五六日天])',section)
    if span:
        a,b=('一二三四五六日'.index('日' if d=='天' else d) for d in span.groups())
        if b<a:raise AgentError('validation_error','请按周一至周日顺序说明提醒日期，或逐个列出星期')
        return list(range(a,b+1))
    days=re.findall(r'[一二三四五六日天]',section)
    if not days:raise AgentError('validation_error','请说明每周几以及提醒时间')
    return sorted(set('一二三四五六日'.index('日' if d=='天' else d) for d in days))


def compound_request(message):
    return not literal_content(message) and bool(re.search(r'然后|并且|并生成|再生成|同时|并(?:帮我)?(?:设置|创建|保存|提醒|同步|修改|取消|查找)',message))


def handle(engine, message, session_id, model_available=False, attachment_ids=None):
    # Literal task/memory content may itself contain words such as "同步".
    if literal_content(message):
        return None
    helper = getattr(engine, 'integrations', None)
    project_id = engine.knowledge.session_project(session_id) if engine.knowledge else None
    def read(name, args=None):
        return engine._read(name, args or {}, session_id=session_id)
    def propose(name, args):
        return engine._proposal(session_id, name, args)

    if helper:
        from .chat_integrations import extract_urls, denies_preview_reads
        urls = extract_urls(message)
        if urls and denies_preview_reads(message):
            return result("本条消息要求不读取链接，未发起抓取或保存。")
        save = bool(re.search(r'保存|归档|导入|存到|存入', message)) and not re.search(r'(?:不要|不必|不需要|别|暂不|无需)(?:再|自动)?(?:保存|归档|导入|存到|存入)',message)
        analysis = bool(re.search(r'分析|总结|对比|生成|根据|然后|并且', message))
        if urls and (not model_available or not analysis):
            if len(urls) > 5:
                return result('一次最多处理 5 个链接，请分批发送。', clarify=True)
            actions, tools, lines = [], [], []
            for url in urls:
                value = read('assistant_preview_link', {'url': url}); preview = value['preview']
                tools.append({'name':'assistant_preview_link','result':value})
                lines.append(preview['title'] + '\n' + preview['content'][:1000])
                if save:
                    actions.append(propose('assistant_save_import', {'preview_id':preview['preview_id']}))
            return result(('已提取正文，并生成归档草案。确认后保存。' if save else '已读取以下网页正文，尚未保存。') + '\n\n' + '\n\n'.join(lines), actions, tools)
        if not urls and save and re.search(r'链接|网页|这份资料|刚才的资料|刚才的预览',message) and not re.search(r'附件|文件',message):
            previews=[item for item in helper.list_previews(session_id,project_id) if not item.get('filename')]
            named=[item for item in previews if item.get('title') and item['title'] in message]
            if named:previews=named
            elif re.search(r'刚才|最近|上一个',message):previews=previews[:1]
            if len(previews)==1:
                return result('已用这次对话的真实网页预览生成归档草案，确认后保存。',[propose('assistant_save_import',{'preview_id':previews[0]['preview_id']})])
            return result('请先提供链接，或说明要保存的网页标题。多个预览时不会猜测。',clarify=True)
        mentions_attachment = bool(attachment_ids) or bool(re.search(r'附件|上传的文件|这个文件|这些文件', message))
        if mentions_attachment and denies_preview_reads(message):
            return result('本条消息要求不读取附件，不会将正文交给模型或生成归档草案。')
        if mentions_attachment and (not model_available or not analysis):
            attachments = helper.list_attachments(session_id, project_id)
            if attachment_ids:
                attachments = [item for item in attachments if item['id'] in attachment_ids]
            elif len(attachments) > 1 and not re.search(r'这些|全部|所有', message):
                attachments = [item for item in attachments if item.get('filename') and item['filename'] in message]
            if not attachments:
                return result('请先上传文件；有多个附件时，请说明文件名或“全部附件”。', clarify=True)
            actions, lines, tools = [], [], []
            for item in attachments:
                value = read('assistant_read_attachment', {'attachment_id':item['id']}); preview=value['preview']
                tools.append({'name':'assistant_read_attachment','result':value})
                lines.append(preview['title']+'\n'+preview['content'][:1000])
                if save:
                    actions.append(propose('assistant_save_import', {'preview_id':preview['preview_id']}))
            return result(('已生成附件归档草案，确认后保存。' if save else '已读取附件，尚未归档。')+'\n\n'+'\n\n'.join(lines),actions,tools)

        if re.fullmatch(r'(?:查看|查询|看看|列出)(?:我的|所有)?(?:提醒|提醒计划|定时计划|自动化计划)[？?]?', message):
            value=read('assistant_list_reminders'); jobs=value['jobs']
            lines=[]
            for job in jobs:
                when=job.get('run_at') if job['kind']=='one_time' else ('日程前 '+str(job['lead_minutes'])+' 分钟' if job['kind']=='event_reminders' else job['time'])
                lines.append('• '+job['name']+' · '+when+' · '+('已启用' if job['enabled'] else '已暂停'))
            return result('暂无提醒计划。' if not lines else '提醒计划：\n'+'\n'.join(lines),tools=[{'name':'assistant_list_reminders','result':value}])
        if re.search(r'提醒|定时计划|摘要',message) and re.search(r'暂停|停用|启用|恢复|取消|删除',message):
            value=read('assistant_list_reminders'); jobs=[j for j in value['jobs'] if j['name'] in message]
            if len(jobs)!=1:
                return None if model_available else result('请说明提醒计划的准确名称，可以先说“查看提醒”。',clarify=True)
            job=jobs[0];args={'reminder_id':job['id'],'expected_version':job['version']}
            if re.search(r'取消|删除',message):tool='assistant_delete_reminder'
            else:
                tool='assistant_update_reminder';args['changes']={'enabled':bool(re.search(r'启用|恢复',message))}
            return result('已生成提醒计划变更，确认后执行。',[propose(tool,args)])
        if re.search(r'提醒|摘要', message) and re.search(r'(?:不要|不必|不需要|别|暂不|无需).{0,8}(?:提醒|创建|设置)',message):
            return result('不会新建提醒。如需停用已有计划，请说明计划名称，例如“暂停喝水提醒”。')
        if re.search(r'提醒|摘要', message) and not re.search(r'怎么|如何|能否|可以吗', message):
            if model_available and analysis:
                return None
            day, now, tz=day_in(message,engine);at=clock_in(message)
            lead=re.search(r'提前\s*('+NUM+r')\s*分钟',message)
            if lead and re.search(r'日程|会议|评审|例会',message):
                minutes=number(lead[1])
                if day and not re.search(r'每次|所有|全部|每天|长期',message):
                    value=read('calendar_list_events',{'start':day.isoformat(),'end':(day+timedelta(days=1)).isoformat()})
                    events=[event for event in value['events'] if not event.get('all_day')]
                    named=[event for event in events if event['title'] in message]
                    if named:events=named
                    if len(events)!=1:
                        return None if model_available else result('请说明要提前提醒哪一场日程的准确标题；不会扩大到所有日程。',clarify=True)
                    event=events[0];run_at=parse_datetime(event['start'],tz)-timedelta(minutes=minutes)
                    args={'name':event['title']+'提前提醒','kind':'one_time','run_at':run_at.isoformat(),'message':'日程提醒：'+event['title'],'enabled':True}
                elif re.search(r'每次|所有|全部|每天|长期',message):
                    args={'name':'日程提前提醒','kind':'event_reminders','lead_minutes':minutes,'enabled':True}
                else:
                    return None if model_available else result('请说明日期和日程标题，或明确“每次日程都提前提醒”。',clarify=True)
            else:
                weekdays=list(range(7))
                recurring=bool(re.search(r'每天|每日|每周|工作日',message))
                selected_days=weekday_selection(message)
                if selected_days is not None:weekdays=selected_days
                content=re.split(r'提醒我|提醒|摘要',message,maxsplit=1)[-1].strip(' ，,。:：')
                if not content:
                    if re.search(r'安排摘要|日程摘要',message):content='今日安排摘要'
                    elif re.search(r'到期待办摘要|待办摘要',message):content='到期待办摘要'
                    else:return result('请说明要提醒的内容，例如“明天15:00提醒我整理材料”。',clarify=True)
                if recurring:
                    if not at:return result('请说明提醒时间，例如“每天09:00提醒我查看今天安排”。',clarify=True)
                    kind='daily_summary' if re.search(r'今天.*安排|今日.*(?:日程|安排)|安排摘要|日程摘要',message) else 'task_digest' if re.search(r'到期.*(?:待办|任务)|待办摘要',message) else 'daily_reminder'
                    args={'name':content[:100],'kind':kind,'time':'%02d:%02d'%at,'weekdays':weekdays,'enabled':True}
                    if kind=='daily_reminder':args['message']=content
                else:
                    relative=re.search(r'('+NUM+r')\s*(分钟|小时)后',message)
                    if relative:
                        amount=number(relative[1]);run_at=now+timedelta(minutes=amount*(60 if relative[2]=='小时' else 1))
                    elif day and at:
                        run_at=datetime.combine(day,datetime.min.time()).replace(hour=at[0],minute=at[1])
                    else:return None if model_available else result('请说明提醒日期和时间，例如“明天15:00提醒我整理材料”。',clarify=True)
                    args={'name':content[:100],'kind':'one_time','run_at':run_at.isoformat(),'message':content,'enabled':True}
            if args['kind']=='event_reminders':
                selected_days=weekday_selection(message)
                if selected_days is not None:args['weekdays']=selected_days
            if re.search(r'只在工作台|仅(?:在)?工作台|只用工作台|不要发.{0,6}外部|不发.{0,6}外部通知',message):args['in_app_only']=True
            return result('已生成提醒计划，确认后启用。',[propose('assistant_create_reminder',args)])
        if re.search(r'同步|连接状态',message) and re.search(r'日历|日程',message):
            value=read('assistant_calendar_sync_status')
            if not re.search(r'同步',message):
                return result('外部日历已启用。' if value.get('enabled') else '外部日历尚未启用，请先在“自动化 → 外部日历”保存账号并选择日历。',tools=[{'name':'assistant_calendar_sync_status','result':value}])
            if not value.get('enabled'):
                return result('请先在“自动化 → 外部日历”配置账号、选择日历并启用同步；助手不会自动开启外部写入。',clarify=True)
            dates=re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)',message)
            if len(dates)==2:start,end=dates
            else:
                day,now,tz=day_in(message,engine)
                if day:start,end=day.isoformat(),(day+timedelta(days=1)).isoformat()
                elif '本周' in message or '这周' in message:
                    start_day=now.date()-timedelta(days=now.weekday());start,end=start_day.isoformat(),(start_day+timedelta(days=7)).isoformat()
                else:return None if model_available else result('请说明同步范围，例如“同步今天的日历”；明确日期范围时，结束日期不含当天。',clarify=True)
            return result('已生成日历同步草案，请核对日期和同步范围后确认。',[propose('assistant_sync_calendar',{'start':start,'end':end})])

    if re.search(r'空闲|空档|有空',message):
        day,now,tz=day_in(message,engine);duration=re.search(r'('+NUM+r')\s*(分钟|小时)',message)
        if day and duration:
            minutes=number(duration[1])*(60 if duration[2]=='小时' else 1)
            args={'start':day.isoformat(),'end':(day+timedelta(days=1)).isoformat(),'duration_minutes':minutes}
            value=read('calendar_find_free_slots',args)
            slots=value.get('slots',[])
            lines=[parse_datetime(s['start'],tz).astimezone(tz).strftime('%H:%M')+'–'+parse_datetime(s['end'],tz).astimezone(tz).strftime('%H:%M') for s in slots]
            return result('没有找到符合工作时段的空闲时间。' if not lines else '可选空闲时间：\n'+'\n'.join(lines),tools=[{'name':'calendar_find_free_slots','result':value}])
        return None if model_available else result('请说明日期和时长，例如“查找明天30分钟的空闲时间”。',clarify=True)
    if denies_writes(message):
        return result('本条消息包含只读或否定要求，不会生成修改草案。需要变更时，请单独说明要执行的操作。')
    if re.search(r'工作时段|工作时间|午休',message) and re.search(r'设为|设置|改为|改成|调整',message):
        clocks=re.findall(r'(?<!\d)\d{1,2}:\d{2}(?!\d)',message)
        if len(clocks)==2:
            fields=('lunch_start','lunch_end') if '午休' in message else ('work_start','work_end')
            return result('已生成日程偏好变更，确认后保存。',[propose('calendar_set_preferences',dict(zip(fields,clocks)))])
        return None if model_available else result('请给出起止时间，例如“把工作时段设为09:00至18:00”。',clarify=True)
    if re.search(r'完成.*待办|完成.*任务|待办.*(?:已完成|做完了)',message):
        tasks=read('assistant_list_tasks')['tasks'];matches=[t for t in tasks if t['title'] in message and not t.get('completed')]
        if len(matches)==1:return result('已生成完成待办的草案。',[propose('assistant_complete_task',{'task_id':matches[0]['id']})])
        return None if model_available else result('请说明待办的准确标题，可以先说“查看待办”。',clarify=True)
    operation=re.search(r'改到|改为|改成|挪到|推迟到|提前到|改期|取消|删除',message)
    if operation and not re.search(r'提醒|资料|附件|文件|记忆|待办|任务',message):
        day,now,tz=day_in(message,engine)
        if not day:return None if model_available else result('请说明要修改或取消的日期和日程标题。',clarify=True)
        args={'start':day.isoformat(),'end':(day+timedelta(days=1)).isoformat()};value=read('calendar_list_events',args);events=value['events']
        exact=[e for e in events if e['title'] in message]
        if not exact:
            hint=message[:operation.start()] if operation.start()>0 else message[operation.end():]
            hint=re.sub(r'今天|明天|后天|\d{4}-\d{2}-\d{2}|下周[一二三四五六日天]|本周[一二三四五六日天]|请|帮我|把|将|的|日程|会议|[，。,:：\s]','',hint)
            exact=[e for e in events if len(hint)>=2 and hint in e['title']]
        if len(exact)!=1:return None if model_available else result('无法唯一定位日程，请提供准确标题；我不会猜测要修改哪一场。',clarify=True)
        event=exact[0];proposal={'event_id':event['id'],'expected_version':event['version']}
        if re.search(r'整个系列|全部重复',message):proposal['scope']='series'
        if operation.group() in ('取消','删除'):tool='calendar_delete_event'
        else:
            at=clock_in(message)
            if not at:return None if model_available else result('请说明新的开始时间，例如“把明天的方案评审改到16:00”。',clarify=True)
            if event.get('all_day'):return None if model_available else result('全天日程请在日历中修改日期，或明确说明如何调整。',clarify=True)
            duration=parse_datetime(event['end'],tz)-parse_datetime(event['start'],tz)
            start=datetime.combine(day,datetime.min.time()).replace(hour=at[0],minute=at[1]);end=start+duration
            proposal['changes']={'start':start.isoformat(),'end':end.isoformat()};tool='calendar_update_event'
        return result('已定位原日程并生成变更草案，确认后执行。',[propose(tool,proposal)])
    return None
