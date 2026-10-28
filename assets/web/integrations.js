"use strict";
class IntegrationsUI {
  constructor(services) { Object.assign(this,services); this.notification=null; this.caldav=null; this.jobs=[]; this.poll=null; }
  n(id) { return document.getElementById(id); }
  button(text,fn,cls="button is-light is-small") { const b=this.el("button",cls,text);b.type="button";b.addEventListener("click",fn);return b; }
  async safely(button,fn) { await this.busy(button,async()=>{try {this.showError("integration-error",null);await fn();}catch(error){this.showError("integration-error",error);}}); }
  stamp(value) { try { return new Intl.DateTimeFormat("zh-CN",{timeZone:this.timezone(),dateStyle:"short",timeStyle:"short"}).format(new Date(value)); } catch(_){return value;} }
  async load() {
    const [data,cal,runtime,runs]=await Promise.all([this.api("/api/automations"),this.api("/api/caldav"),this.api("/api/runtime"),this.api("/api/agent-runs")]);
    this.jobs=data.jobs;this.notification=data.notifications;this.caldav=cal.settings;
    const stopped=this.jobs.some(job=>job.enabled)&&!runtime.scheduler_running;
    this.n("automation-notice").hidden=!stopped;
    this.n("automation-notice").textContent=stopped?"定时计划已停止，摘要和提醒暂不会自动发送。请重新启动工作台。":"";
    this.onRuntime(runtime,cal.settings);
    if(document.activeElement!==this.n("notification-urls"))this.n("notification-urls").value="";
    this.n("notification-enabled").checked=this.notification.enabled;this.n("notification-clear").checked=false;this.n("notification-secret-state").textContent=this.notification.has_urls?"地址已保存，留空保留。":"尚未连接外部通知。";
    this.n("test-notifications").disabled=!this.notification.enabled;
    for(const [id,key] of [["url","url"],["user","username"],["calendar","calendar_url"]])this.n("caldav-"+id).value=this.caldav[key]||"";
    this.n("caldav-password").value="";this.n("caldav-clear").checked=false;this.n("caldav-enabled").checked=this.caldav.enabled;this.n("caldav-writes").checked=this.caldav.writes_enabled;
    this.n("caldav-secret-state").textContent=this.caldav.has_password?"密码已保存，留空保留。":"未保存密码。";
    this.n("caldav-status").textContent=this.caldav.enabled?"账号已保存，可先测试连接。":"日历同步未启用。";
    if(!this.n("caldav-start").value){this.n("caldav-start").value=this.today();this.n("caldav-end").value=this.addDays(this.today(),7);}
    this.renderJobs();this.renderInbox(data.inbox);this.renderCalDAV(cal);this.renderRuns(runs.runs);
  }
  renderJobs() {
    const target=this.n("automation-list");target.replaceChildren();
    if(!this.jobs.length)target.append(this.el("p","library-empty","暂无定时计划。"));
    const labels={daily_summary:"每日摘要",task_digest:"到期待办",event_reminders:"日程提醒"};
    this.jobs.forEach(job=>{const row=this.el("div","integration-row");const text=this.el("div","integration-row-main");text.append(this.el("h3","",job.name),this.el("p","field-help",`${labels[job.kind]} · ${job.kind==="event_reminders"?"提前 "+job.lead_minutes+" 分钟":job.time} · ${job.enabled?"已启用":"已暂停"}`));row.append(text,this.button(job.enabled?"暂停":"启用",()=>this.safely(row.querySelector("button"),async()=>{const {id,version,...body}=job;await this.api(`/api/automations/${id}`,{method:"PATCH",body:{...body,enabled:!job.enabled,expected_version:version}});await this.load();})),this.button("删除",()=>this.confirmOperation("删除定时计划",job.name+"\n仅删除计划，不删除已有消息。","删除计划",async()=>{await this.api(`/api/automations/${job.id}`,{method:"DELETE",body:{expected_version:job.version}});await this.load();})));target.append(row);});
  }
  renderInbox(items) {
    const target=this.n("notification-inbox");target.replaceChildren();const count=items.filter(n=>!n.read).length;this.n("inbox-count").textContent=count;this.n("inbox-count").hidden=!count;
    if(!items.length)target.append(this.el("p","library-empty","暂无消息。"));
    const labels={in_app:"工作台消息",sent:"已提交发送",unknown:"结果待确认，请到接收应用查看",expired:"已过期，未发送",queued:"等待发送",sending:"正在发送"};
    items.forEach(item=>{const row=this.el("article","integration-row inbox-row"+(item.read?" is-read":""));const main=this.el("div","integration-row-main");main.append(this.el("h3","",item.title),this.el("p","source-meta",`${this.stamp(item.created_at)} · ${labels[item.state]||item.state}`),this.el("p","notification-body",item.body));row.append(main);if(!item.read)row.append(this.button("标为已读",()=>this.safely(row.querySelector("button"),async()=>{await this.api(`/api/notifications/${item.id}/read`,{method:"POST",body:{}});await this.load();})));target.append(row);});
  }
  renderCalDAV(data) {
    const conflicts=this.n("caldav-conflicts");conflicts.replaceChildren();
    data.conflicts.forEach(conflict=>{const row=this.el("article","integration-row");const main=this.el("div","integration-row-main");main.append(this.el("h3","","同步冲突"),this.el("p","",conflict.reason),this.el("p","field-help",conflict.remote?conflict.remote.title:"外部日历中的日程已删除或发生变化"));const actions=this.el("div","integration-actions");[ ["使用外部日历内容","remote"],["保留本机内容并同步","local"] ].forEach(([label,choice])=>{const b=this.button(label,()=>this.confirmOperation("处理日历冲突",`${conflict.reason}\n${label}会更新日程，请确认内容。`,label,async()=>{await this.api(`/api/caldav/conflicts/${conflict.id}/resolve`,{method:"POST",body:{choice,expected_version:conflict.local_version}});await this.load();await this.refreshData();}));if(choice==="local")b.disabled=!this.caldav.writes_enabled;actions.append(b);});main.append(actions);row.append(main);conflicts.append(row);});
    const ops=this.n("caldav-operations");ops.replaceChildren();const labels={applied:"已完成",unknown:"结果待核查",sending:"正在处理",failed:"同步被拒绝",conflict:"外部日历内容已变化"};
    data.operations.slice().reverse().forEach(op=>{const row=this.el("div","integration-row");const main=this.el("div","integration-row-main");main.append(this.el("p","",`${op.kind==="put"?"日历写入":"日历删除"} · ${labels[op.status]||op.status}`),this.el("p","field-help",op.error||this.stamp(op.created_at)));row.append(main);if(["unknown","sending"].includes(op.status))row.append(this.button("查看同步结果",()=>this.safely(row.querySelector("button"),async()=>{const result=await this.api(`/api/caldav/operations/${op.id}/reconcile`,{method:"POST",body:{}});await this.load();this.toast(result.confirmed?"已完成":"暂时无法确认，请到原日历查看；没有重复发送。");})));ops.append(row);});
  }
  renderRuns(runs) {
    const target=this.n("agent-runs");target.replaceChildren();if(!runs.length)target.append(this.el("p","library-empty","暂无处理记录。"));
    const states={running:"处理中",completed:"处理完成",failed:"处理失败",interrupted:"已中断"};
    runs.forEach(run=>{const details=this.el("details","run-record");details.append(this.el("summary","",`${this.stamp(run.created_at)} · ${run.model} · ${states[run.state]||run.state}`));if(run.error)details.append(this.el("p","field-help",run.error));const list=this.el("ol","run-steps");run.steps.forEach(step=>list.append(this.el("li","",this.stepLabel(step.name)+" · "+({running:"开始",completed:"完成",failed:"失败",draft:"等待确认"}[step.state]||step.state))));details.append(list);if(run.usage)details.append(this.el("p","field-help",`${run.usage.requests||0} 次处理 · ${run.usage.reported?"输入 "+run.usage.input_tokens+" / 输出 "+run.usage.output_tokens+" token":"服务商未提供用量"}`));target.append(details);});
  }
  stepLabel(name) {
    const labels={"模型请求":"分析请求",calendar_status:"检查日历连接",calendar_list_calendars:"查看日历",calendar_list_events:"查询日程",calendar_get_event:"读取日程",calendar_find_free_slots:"查找空闲时间",calendar_get_preferences:"读取日程偏好",calendar_set_preferences:"准备日程偏好",calendar_create_event:"准备新日程",calendar_update_event:"准备日程修改",calendar_delete_event:"准备取消日程",knowledge_list_projects:"查看项目",knowledge_project_context:"读取项目背景",knowledge_search:"检索资料",knowledge_get_source:"读取资料",assistant_list_tasks:"读取待办",assistant_list_memories:"读取记忆",assistant_add_task:"准备待办",assistant_complete_task:"准备完成待办",assistant_save_memory:"准备保存记忆"};
    return labels[name]||"处理请求";
  }
  async progress() {
    try {const data=await this.api("/api/agent-runs");const run=data.runs.find(r=>r.state==="running");const box=this.n("agent-live-status");box.hidden=!run;if(run){const last=run.steps[run.steps.length-1];box.textContent=`助手正在处理${last?" · "+this.stepLabel(last.name):""}`;}this.renderRuns(data.runs);}catch(_){/* Main request displays connection errors. */}
  }
  watch(active) {clearInterval(this.poll);this.poll=null;this.n("agent-live-status").hidden=!active;if(active){this.progress();this.poll=setInterval(()=>this.progress(),1200);}else this.progress();}
  bind() {
    const jobFields=()=>{const reminder=this.n("automation-kind").value==="event_reminders";this.n("automation-time").closest(".field").hidden=reminder;this.n("automation-time").required=!reminder;this.n("automation-lead").closest(".field").hidden=!reminder;};
    this.n("automation-kind").addEventListener("change",jobFields);this.n("automation-form").addEventListener("reset",()=>setTimeout(jobFields,0));jobFields();
    this.n("refresh-integrations").addEventListener("click",()=>this.safely(this.n("refresh-integrations"),()=>this.load()));
    this.n("automation-form").addEventListener("submit",e=>{e.preventDefault();this.safely(this.n("save-automation"),async()=>{await this.api("/api/automations",{method:"POST",body:{name:this.n("automation-name").value,kind:this.n("automation-kind").value,time:this.n("automation-time").value,lead_minutes:Number(this.n("automation-lead").value),enabled:this.n("automation-enabled").checked}});this.n("automation-form").reset();await this.load();this.toast("计划已保存");});});
    this.n("notification-form").addEventListener("submit",e=>{e.preventDefault();this.safely(this.n("save-notifications"),async()=>{await this.api("/api/notifications/settings",{method:"PUT",body:{urls:this.n("notification-urls").value,clear_urls:this.n("notification-clear").checked,enabled:this.n("notification-enabled").checked,expected_version:this.notification.version}});await this.load();this.toast("通知设置已保存");});});
    this.n("test-notifications").addEventListener("click",()=>this.confirmOperation("发送通知测试","将向已保存且启用的通知渠道发送一条测试消息。","发送测试",async()=>{await this.api("/api/notifications/test",{method:"POST",body:{}});await this.load();this.toast("已提交发送测试请求，请检查实际设备");},false));
    this.n("caldav-form").addEventListener("submit",e=>{e.preventDefault();this.safely(this.n("save-caldav"),async()=>{await this.api("/api/caldav",{method:"PUT",body:{url:this.n("caldav-url").value.trim(),username:this.n("caldav-user").value.trim(),password:this.n("caldav-password").value,clear_password:this.n("caldav-clear").checked,calendar_url:this.n("caldav-calendar").value.trim(),enabled:this.n("caldav-enabled").checked,writes_enabled:this.n("caldav-writes").checked,expected_version:this.caldav.version}});await this.load();this.toast("日历设置已保存");});});
    this.n("test-caldav").addEventListener("click",()=>this.safely(this.n("test-caldav"),async()=>{const result=await this.api("/api/caldav/test",{method:"POST",body:{}});const select=this.n("caldav-picker");select.replaceChildren();result.calendars.forEach(c=>{const option=this.el("option","",c.name);option.value=c.url;select.append(option);});this.n("caldav-picker-field").hidden=!result.calendars.length;if(result.calendars.length){select.value=this.caldav.calendar_url||result.calendars[0].url;this.n("caldav-calendar").value=select.value;}this.n("caldav-status").textContent=`找到 ${result.calendars.length} 个日历，请选择后保存。`;}));
    this.n("caldav-picker").addEventListener("change",e=>{this.n("caldav-calendar").value=e.target.value;});
    this.n("caldav-sync-form").addEventListener("submit",async e=>{e.preventDefault();const start=this.n("caldav-start").value,end=this.n("caldav-end").value;
      await this.safely(this.n("sync-caldav"),async()=>{if(!this.caldav.enabled)throw {message:"请先保存并启用日历同步。"};const local=await this.api(`/api/events?${new URLSearchParams({start,end})}`);const names=local.events.filter(x=>!x.recurring&&!x.external).map(x=>x.title);this.confirmOperation("同步日历",`${start} 至 ${end}（不含结束当天）\n${this.caldav.writes_enabled?"将同步新日程、修改和删除。本机单次日程 "+names.length+" 项：\n"+names.slice(0,10).join("、")+"\n请核对日程；同步会更新外部日历，也可能删除日程。":"只导入外部日程，不修改外部日历。"}`,"确认同步",async()=>{const result=await this.api("/api/caldav/sync",{method:"POST",body:{start,end}});await this.load();await this.refreshData();this.n("caldav-status").textContent=`本次更新 ${result.imported} 项，请查看同步结果。`;},false);});});
  }
}
