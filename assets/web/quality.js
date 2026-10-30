"use strict";

class QualityUI {
  constructor(services) { Object.assign(this,services); this.data=null; this.request=0; this.currentCase=null; this.fromFeedback=null; this.answer=null; }
  n(id) { return document.getElementById(id); }
  button(text,callback) { const b=this.el("button","button is-ghost is-small",text);b.type="button";b.addEventListener("click",callback);return b; }
  async safe(button,fn,error="quality-error") { return this.busy(button,async()=>{this.showError(error,null);try {await fn();}catch(e){this.showError(error,e);}}); }
  async load() {
    const request=++this.request;const data=await this.api("/api/quality");if(request!==this.request)return;
    this.data=data;this.n("quality-counts").textContent=`${data.counts.feedback} 条反馈 · ${data.counts.open} 个待处理问题 · ${data.counts.cases} 条用例`;
    const history=this.n("quality-run-history");history.replaceChildren();data.evaluations.forEach(run=>{const option=this.el("option","",new Date(run.created_at).toLocaleString("zh-CN")+(run.stale?" · 历史快照":""));option.value=run.id;history.append(option);});history.disabled=!data.evaluations.length;
    this.renderFeedback();this.renderCases();this.renderEvaluation(data.evaluations[0]);
    this.n("quality-run").disabled=!data.cases.some(c=>c.active);
  }
  answerControls(message,sessionId,existing) {
    const box=this.el("div","answer-feedback");
    const helpful=this.button(existing?.rating==="helpful"?"已标记有帮助":"有帮助",()=>this.safe(helpful,async()=>{
      const result=await this.api("/api/quality/feedback",{method:"POST",body:{session_id:sessionId,message_id:message.id,rating:"helpful",...(existing?{expected_version:existing.version}:{})}});
      this.onFeedback(result.feedback);this.toast("反馈已保存");
    },"chat-error"));
    const issue=this.button(existing?.rating==="issue"?"编辑问题反馈":"有问题",()=>{
      this.answer={message,sessionId,existing};this.n("feedback-category").value=existing?.category||"other";this.n("feedback-note").value=existing?.note||"";
      this.n("feedback-question").textContent="反馈会关联这条回复、原始问题和引用资料。";this.showError("feedback-error",null);this.n("feedback-dialog").showModal();
    });box.append(helpful,issue);return box;
  }
  citation(source) {
    this.n("citation-title").textContent=source.title;this.n("citation-excerpt").textContent=source.excerpt||"这条历史引用没有保存摘录。";
    this.n("citation-position").textContent=source.chunk?`第 ${source.chunk.number} 段 · 原文第 ${source.chunk.start+1}–${source.chunk.end} 字`:"引用时保存的摘录";
    this.showError("citation-error",null);this.cited=source;this.n("citation-dialog").showModal();
  }
  renderFeedback() {
    const target=this.n("quality-feedback-list");target.replaceChildren();
    if(!this.data.feedback.length)target.append(this.el("p","library-empty","暂无反馈。在助手回复下标记即可。"));
    const categories={retrieval:"资料检索",citation:"引用出处",intent:"请求理解",context:"上下文",execution:"操作执行",other:"其他"};
    const statuses={new:"待分析",investigating:"处理中",retest:"待复测",closed:"已关闭（人工验收）"};
    this.data.feedback.forEach(f=>{
      const row=this.el("details","quality-row");row.append(this.el("summary","",`${f.rating==="helpful"?"有帮助":categories[f.category]} · ${f.rating==="helpful"?"已记录":statuses[f.status]} · ${f.question.slice(0,48)}`));
      row.append(this.el("p","quality-question",f.question),this.el("p","quality-answer",f.answer),this.el("p","field-help",f.note||"未补充问题说明"));
      const actions=this.el("div","quality-actions");actions.append(this.button("查看原对话",()=>this.onOpenSession(f.session_id)));
      if(f.rating==="issue") {
        actions.append(this.button("转为检索用例",()=>this.openCase(null,f)));
        const form=this.el("form","quality-form");const select=this.el("select","input");
        for(const [value,label] of Object.entries(statuses)){const option=this.el("option","",label);option.value=value;select.append(option);}select.value=f.status;
        const label=this.el("label","label","处理状态");label.append(select);form.append(label);
        const controls={};for(const [key,text] of [["diagnosis","问题原因"],["fix","改进措施"],["verification","实际复测结果"]]){
          const l=this.el("label","label",text);const input=this.el("textarea","textarea");input.rows=2;input.maxLength=2000;input.value=f[key]||"";l.append(input);form.append(l);controls[key]=input;
        }
        form.append(this.el("p","field-help","关闭需要人工填写复测结果；不会自动训练模型或自动修改规则。"));
        const save=this.el("button","button is-light is-small","保存处理进展");save.type="submit";form.append(save);
        form.addEventListener("submit",e=>{e.preventDefault();this.safe(save,async()=>{await this.api(`/api/quality/feedback/${f.id}`,{method:"PATCH",body:{expected_version:f.version,status:select.value,diagnosis:controls.diagnosis.value,fix:controls.fix.value,verification:controls.verification.value}});await this.load();this.toast("处理记录已保存");});});row.append(form);
      }
      row.append(actions);
      if(f.history?.length){const history=this.el("details","quality-history");history.append(this.el("summary","field-help",`处理历史（最近 ${f.history.length} 次）`));f.history.slice().reverse().forEach(h=>history.append(this.el("p","field-help",[h.updated_at?new Date(h.updated_at).toLocaleString("zh-CN"):"",statuses[h.status]||"标注更新",h.note,h.diagnosis,h.fix,h.verification].filter(Boolean).join(" · "))));row.append(history);}
      target.append(row);
    });
  }
  renderCases() {
    const target=this.n("quality-case-list");target.replaceChildren();if(!this.data.cases.length)target.append(this.el("p","library-empty","暂无检索用例，添加问题并选择预期资料。"));
    this.data.cases.forEach(c=>{
      const row=this.el("article","quality-row");row.append(this.el("h3","",c.title),this.el("p","",c.query),this.el("p","field-help",`${c.active?"参与评测":"已停用"} · ${c.kind==="unanswerable"?"应无相关资料":c.expected.map(x=>x.title).join("、")}`));
      const actions=this.el("div","quality-actions");actions.append(this.button("核对／编辑",()=>this.openCase(c)),this.button(c.active?"停用":"启用",async e=>this.safe(e.currentTarget,async()=>{await this.api(`/api/quality/cases/${c.id}`,{method:"PATCH",body:{expected_version:c.version,active:!c.active}});await this.load();})));row.append(actions);target.append(row);
    });
  }
  async openCase(value=null,feedback=null) {
    try {await this.load();}catch(e){this.showError("quality-error",e);return;}
    this.currentCase=value;this.fromFeedback=feedback;
    this.n("quality-case-name").value=value?.title||feedback?.question.slice(0,100)||"";const question=value?.query||feedback?.question||"";
    this.n("quality-case-query").value=question.slice(0,300);this.n("quality-query-note").textContent=question.length>300?"原问题较长，请核对并缩短检索问题。":"参考答案只用于人工核对。";
    const select=this.n("quality-case-project");select.replaceChildren();const all=this.el("option","","全部资料");all.value="";select.append(all);
    this.data.projects.forEach(p=>{const o=this.el("option","",p.name);o.value=p.id;select.append(o);});select.value=value?.project_id||feedback?.project_id||"";select.disabled=Boolean(feedback);
    this.n("quality-case-kind").value=value?.kind||"answerable";this.n("quality-case-answer").value=value?.reference_answer||"";this.n("quality-case-active").checked=value?.active!==false;
    this.populateSources(value?.expected.map(x=>x.id)||[]);this.showError("quality-case-error",null);this.n("quality-case-dialog").showModal();
  }
  populateSources(selected=[]) {
    const select=this.n("quality-case-sources"),project=this.n("quality-case-project").value;select.replaceChildren();
    this.data.source_choices.filter(s=>!project||s.project_id===project).forEach(s=>{const o=this.el("option","",s.title);o.value=s.id;o.selected=selected.includes(s.id);select.append(o);});
    select.disabled=this.n("quality-case-kind").value==="unanswerable";
    if(selected.some(id=>![...select.options].some(o=>o.value===id)))this.n("quality-query-note").textContent="原用例有资料已删除或移出范围，请重新核对。";
  }
  renderEvaluation(run) {
    this.latest=run||null;this.n("quality-export").disabled=!run;const target=this.n("quality-evaluations");target.replaceChildren();
    if(!run){target.append(this.el("p","library-empty","暂无评测结果。添加用例后运行，不会自动填充演示数据。"));return;}
    target.append(this.el("p","field-help",`${new Date(run.created_at).toLocaleString("zh-CN")} · Top ${run.top_k} · ${run.stale?"资料或用例已变化，请重新运行":"运行时资料与用例的固定快照"}`));
    const wrap=this.el("div","quality-table-wrap"),table=this.el("table","table is-fullwidth");const head=this.el("tr","");
    ["检索方法","命中率","资料覆盖率","首条相关排名得分","无资料空结果率","通过／总数"].forEach(t=>head.append(this.el("th","",t)));const thead=this.el("thead","");thead.append(head);table.append(thead);
    const tbody=this.el("tbody","");const percent=x=>x===null?"暂无样本":`${(x*100).toFixed(1)}%`;
    for(const [name,label] of [["weighted-keyword-v1","原检索"],["chunks-bm25-v1","分段检索"]]){const m=run.engines[name].metrics,r=this.el("tr","");[label,percent(m.hit_at_k),percent(m.recall_at_k),m.mrr===null?"暂无样本":m.mrr.toFixed(3),percent(m.empty_result_rate),`${m.passed} / ${m.count}`].forEach(t=>r.append(this.el("td","",t)));tbody.append(r);}table.append(tbody);wrap.append(table);target.append(wrap);
    target.append(this.el("p","field-help","命中率按可回答用例计算；覆盖率为预期资料覆盖比例；排名得分为首条正确资料排名倒数的均值。无资料用例单独计算。耗时仅为检索计算，不含索引准备、模型或网络。"));
    for(const c of run.engines["chunks-bm25-v1"].cases){const d=this.el("details","quality-row");d.append(this.el("summary","",`${c.passed?"通过":"未通过"} · ${c.title}`),this.el("p","",c.query),this.el("p","field-help",`预期：${c.expected.map(x=>x.title).join("、")||"无相关资料"} · ${c.milliseconds} 毫秒`));
      if(c.reference_answer)d.append(this.el("p","quality-question","参考答案（人工核对）："+c.reference_answer));
      if(!c.retrieved.length)d.append(this.el("p","field-help","本次没有检索结果。"));
      c.retrieved.forEach(s=>{d.append(this.button(s.title,()=>this.citation(s)),this.el("p","source-excerpt",s.excerpt));});target.append(d);
    }
  }
  bind() {
    this.n("quality-refresh").addEventListener("click",e=>this.safe(e.currentTarget,()=>this.load()));
    this.n("quality-run-history").addEventListener("change",()=>this.renderEvaluation(this.data.evaluations.find(run=>run.id===this.n("quality-run-history").value)));
    this.n("quality-add-case").addEventListener("click",()=>this.openCase());
    this.n("quality-case-project").addEventListener("change",()=>this.populateSources());
    this.n("quality-case-kind").addEventListener("change",()=>{this.n("quality-case-sources").disabled=this.n("quality-case-kind").value==="unanswerable";});
    this.n("quality-case-form").addEventListener("submit",e=>{e.preventDefault();this.safe(this.n("quality-case-save"),async()=>{
      const old=this.currentCase;const kind=this.n("quality-case-kind").value;
      const body={title:this.n("quality-case-name").value,query:this.n("quality-case-query").value,kind,project_id:this.n("quality-case-project").value||null,
        expected_source_ids:kind==="unanswerable"?[]:[...this.n("quality-case-sources").selectedOptions].map(o=>o.value),reference_answer:this.n("quality-case-answer").value,active:this.n("quality-case-active").checked,
        feedback_id:this.fromFeedback?.id||old?.feedback_id||null,...(old?{expected_version:old.version}:{})};
      await this.api(old?`/api/quality/cases/${old.id}`:"/api/quality/cases",{method:old?"PATCH":"POST",body});this.n("quality-case-dialog").close();await this.load();this.toast("用例已保存，预期资料已按当前内容确认");
    },"quality-case-error");});
    this.n("feedback-form").addEventListener("submit",e=>{e.preventDefault();this.safe(this.n("feedback-save"),async()=>{
      const {message,sessionId,existing}=this.answer;const result=await this.api("/api/quality/feedback",{method:"POST",body:{session_id:sessionId,message_id:message.id,rating:"issue",category:this.n("feedback-category").value,note:this.n("feedback-note").value,...(existing?{expected_version:existing.version}:{})}});
      this.onFeedback(result.feedback);this.n("feedback-dialog").close();this.toast("问题已记录，可到评测与反馈中处理");
    },"feedback-error");});
    this.n("quality-run-form").addEventListener("submit",e=>{e.preventDefault();this.safe(this.n("quality-run"),async()=>{await this.api("/api/quality/evaluations",{method:"POST",body:{top_k:Number(this.n("quality-top-k").value)}});await this.load();this.toast("检索评测已完成");});});
    this.n("quality-export").addEventListener("click",()=>{if(!this.latest)return;const link=document.createElement("a"),url=URL.createObjectURL(new Blob([JSON.stringify(this.latest,null,2)],{type:"application/json"}));link.href=url;link.download="retrieval-evaluation.json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
    this.n("citation-open").addEventListener("click",async()=>{try{const {source}=await this.api(`/api/knowledge/sources/${encodeURIComponent(this.cited.id)}`);if(source.version!==this.cited.version)this.toast("当前原文已更新，引用摘录保持不变");this.n("citation-dialog").close();await this.openSource(source.id);}catch(e){this.showError("citation-error",e);}});
  }
}
