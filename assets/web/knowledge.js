"use strict";

// UI uses the workbench's existing request, modal and status helpers.
class KnowledgeUI {
  constructor(services) { Object.assign(this, services); this.projectId = null; this.projects = []; this.request = 0; this.source = null; this.project = null; this.workstyleVersion = 1; }
  node(id) { return document.getElementById(id); }
  button(label, callback, cls = "button is-ghost is-small") { const node = this.el("button", cls, label); node.type = "button"; node.addEventListener("click", callback); return node; }
  selectProjects(id, value, firstLabel) {
    const select = this.node(id); select.replaceChildren(); const first = this.el("option", "", firstLabel); first.value = ""; select.append(first);
    this.projects.forEach(p => { const option = this.el("option", "", p.name); option.value = p.id; select.append(option); }); select.value = value || "";
  }
  async load() {
    const request = ++this.request;
    const [projects, style] = await Promise.all([this.api("/api/knowledge/projects"), this.api("/api/knowledge/workstyle")]);
    if (request !== this.request) return;
    this.projects = projects.projects;
    if (!this.projects.some(p => p.id === this.projectId)) this.projectId = null;
    this.onProjects(this.projects);
    if (document.activeElement !== this.node("workstyle-content")) { this.node("workstyle-content").value = style.workstyle.content; this.workstyleVersion = style.workstyle.version; }
    this.renderProjects(); await this.search();
  }
  renderProjects() {
    const list = this.node("project-list"); list.replaceChildren();
    const all = this.button("全部资料", () => this.chooseProject(null), "project-choice" + (!this.projectId ? " is-active" : "")); list.append(all);
    this.projects.forEach(project => { const row = this.button(project.name + " · " + project.source_count, () => this.chooseProject(project.id), "project-choice" + (this.projectId === project.id ? " is-active" : "")); row.dataset.projectId = project.id; list.append(row); });
    const context = this.node("project-context"); context.replaceChildren(); context.hidden = !this.projectId;
    const project = this.projects.find(p => p.id === this.projectId);
    if (project) {
      context.append(this.el("h2", "", project.name), this.el("p", "project-background", project.background || "尚未填写项目背景。"));
      const actions = this.el("div", "project-actions"); actions.append(this.button("编辑背景", () => this.editProject(project)), this.button("讨论这个项目", () => this.onAsk(project.id, "这个项目有哪些资料？")), this.button("删除项目", () => this.confirmOperation("删除项目", "请先移出项目中的资料；删除项目不会删除日程和待办。", "删除项目", async () => { await this.api(`/api/knowledge/projects/${project.id}`, {method:"DELETE",body:{expected_version:project.version}}); this.projectId = null; await this.load(); this.toast("项目已删除"); }))); context.append(actions);
    }
  }
  async chooseProject(id) { this.projectId = id; this.node("knowledge-query").value = ""; this.renderProjects(); await this.search(); }
  async search() {
    const request = ++this.searchRequest; this.showError("knowledge-error", null);
    const params = new URLSearchParams({query:this.node("knowledge-query").value.trim(),limit:"100"}); if (this.projectId) params.set("project_id",this.projectId);
    try {
      const result = await this.api(`/api/knowledge/sources?${params}`); if (request !== this.searchRequest) return;
      const list = this.node("source-list"); list.replaceChildren();
      this.node("knowledge-search-note").textContent = `${result.total} 条资料${result.total > result.sources.length ? `，显示前 ${result.sources.length} 条` : ""}`;
      if (!result.sources.length) { list.append(this.el("p", "library-empty", result.query ? "没有找到相关资料，换个关键词试试。" : "暂无资料，添加一条试试。")); return; }
      result.sources.forEach(source => {
        const row = this.el("article", "source-row"); const heading = this.button(source.title, () => this.openSource(source.id), "source-title"); heading.dataset.sourceId = source.id;
        row.append(heading, this.el("p", "source-meta", `${this.kindLabel(source.kind)} · ${source.project_name || "未关联项目"}`), this.el("p", "source-excerpt", source.excerpt));
        if (source.reflection_excerpt) row.append(this.el("p", "source-reflection", "我的想法：" + source.reflection_excerpt));
        list.append(row);
      });
    } catch (error) { if (request === this.searchRequest) this.showError("knowledge-error", error); }
  }
  kindLabel(kind) { return {material:"材料",idea:"想法",decision:"决策记录"}[kind] || kind; }
  editProject(project = null) {
    this.project = project; this.node("project-form").reset(); this.node("project-dialog-title").textContent = project ? "编辑项目" : "新建项目";
    this.node("project-name").value = project ? project.name : ""; this.node("project-background").value = project ? project.background : "";
    this.showError("project-error",null); this.node("project-dialog").showModal(); this.node("project-name").focus();
  }
  editSource(source = null) {
    this.importGeneration=(this.importGeneration||0)+1;
    this.source = source; this.node("source-form").reset(); this.node("source-dialog-title").textContent = source ? "编辑资料" : "保存资料";
    this.selectProjects("source-project", source ? source.project_id : this.projectId, "不关联项目");
    for (const key of ["title","kind","url","content","reflection"]) this.node("source-"+key).value = source ? source[key] : (key === "kind" ? "material" : "");
    this.node("source-file-name").textContent = ""; this.node("source-import-preview-note").hidden=true; this.node("source-truncated-control").hidden=true; this.node("source-truncated-confirm").checked=false; this.node("save-source").disabled=false; this.showError("source-error",null); this.node("source-dialog").showModal(); this.node("source-title").focus();
  }
  async openSource(id) {
    try {
      const {source} = await this.api(`/api/knowledge/sources/${encodeURIComponent(id)}`); this.node("source-view-title").textContent = source.title;
      const body = this.node("source-view-body"); body.replaceChildren(this.el("p", "source-meta", `${this.kindLabel(source.kind)} · ${source.project_name || "未关联项目"} · 当前内容`));
      if (source.url) { const link = this.el("a", "source-origin", "打开原始链接 ↗"); link.href = source.url; link.target = "_blank"; link.rel = "noopener noreferrer"; body.append(link); }
      body.append(this.el("h3", "source-section-title", "原文或记录"), this.el("div", "source-fulltext", source.content));
      if (source.reflection) body.append(this.el("h3", "source-section-title", "我的想法"), this.el("div", "source-fulltext source-personal", source.reflection));
      const actions = this.node("source-view-actions"); actions.replaceChildren();
      actions.append(this.button("编辑资料", () => { this.node("source-view-dialog").close(); this.editSource(source); }, "button is-light"), this.button("在助手中讨论", () => { this.node("source-view-dialog").close(); this.onAsk(source.project_id, "搜索资料："+source.title); }, "button is-primary"), this.button("删除", () => { this.node("source-view-dialog").close(); this.confirmOperation("删除资料", "删除后无法恢复。已有对话中的引用摘录会保留。", "删除资料", async () => { await this.api(`/api/knowledge/sources/${source.id}`, {method:"DELETE",body:{expected_version:source.version}}); await this.load(); this.toast("资料已删除"); }); }));
      // Can be opened over the assistant dialog; closing restores its focus.
      this.node("source-view-dialog").showModal();
    } catch (error) { this.toast(error.message || "无法读取资料", true); }
  }
  applyImport(preview) {
    this.node("source-title").value=preview.title;this.node("source-content").value=preview.content;this.node("source-url").value=preview.url||"";
    this.node("source-file-name").textContent=preview.filename||"网页正文预览";
    const note=this.node("source-import-preview-note");note.replaceChildren();note.hidden=false;
    note.append(this.el("p","",`共 ${preview.characters} 字，确认后保存。${preview.truncated?"内容过长，当前仅预览前 12000 字。":""}`));
    (preview.duplicates||[]).forEach(source=>{note.append(this.el("p","","发现可能重复的资料："+source.title),this.button("查看已有资料",()=>this.openSource(source.id)));});
    this.node("source-truncated-control").hidden=!preview.truncated;this.node("source-truncated-confirm").checked=false;
    this.node("save-source").disabled=Boolean(preview.truncated);
  }
  bind() {
    this.searchRequest = 0;
    this.node("source-dialog").addEventListener("close",()=>{this.importGeneration=(this.importGeneration||0)+1;});
    this.node("source-truncated-confirm").addEventListener("change",event=>{this.node("save-source").disabled=!event.target.checked;});
    this.node("import-article").addEventListener("click",()=>{this.showError("article-import-error",null);this.node("article-import-dialog").showModal();this.node("article-import-url").focus();});
    this.node("article-import-form").addEventListener("submit",async event=>{event.preventDefault();this.showError("article-import-error",null);await this.busy(this.node("preview-article"),async()=>{try {const result=await this.api("/api/imports/article",{method:"POST",body:{url:this.node("article-import-url").value.trim(),public_dns:this.node("article-public-dns").checked}});this.node("article-import-dialog").close();this.editSource();this.applyImport(result.preview);}catch(error){this.showError("article-import-error",error);}});});
    this.node("new-project").addEventListener("click", () => this.editProject()); this.node("new-source").addEventListener("click", () => this.editSource());
    this.node("knowledge-search").addEventListener("submit", e => { e.preventDefault(); this.search(); });
    this.node("project-form").addEventListener("submit", async e => {
      e.preventDefault(); this.showError("project-error",null); const project = this.project;
      await this.busy(this.node("save-project"),async () => { try {
        const body = {name:this.node("project-name").value.trim(),background:this.node("project-background").value}; if (project) body.expected_version = project.version;
        const result = await this.api("/api/knowledge/projects"+(project ? "/"+project.id : ""),{method:project ? "PATCH" : "POST",body}); this.projectId = result.project.id; this.node("project-dialog").close(); await this.load(); this.toast("项目已保存");
      } catch(error) { this.showError("project-error",error); } });
    });
    this.node("source-form").addEventListener("submit", async e => {
      e.preventDefault(); this.showError("source-error",null); const source = this.source;
      await this.busy(this.node("save-source"),async () => { try {
        const body = {}; for (const key of ["title","kind","url","content","reflection"]) body[key] = this.node("source-"+key).value;
        body.project_id = this.node("source-project").value || null; if(source) body.expected_version = source.version;
        await this.api("/api/knowledge/sources"+(source ? "/"+source.id : ""),{method:source ? "PATCH" : "POST",body}); this.projectId = body.project_id; this.node("knowledge-query").value = ""; this.node("source-dialog").close(); await this.load(); this.toast("资料已保存");
      } catch(error) { this.showError("source-error",error); } });
    });
    this.node("source-file").addEventListener("change", async event => {
      const file=event.target.files[0];if(!file)return;const generation=++this.importGeneration;this.showError("source-error",null);this.node("save-source").disabled=true;this.node("source-file-name").textContent="正在读取文件…";
      try {
        if(file.size>8*1024*1024)throw {message:"文件不能超过 8 MB。"};
        const bytes=new Uint8Array(await file.arrayBuffer());let raw="";for(let i=0;i<bytes.length;i+=32768)raw+=String.fromCharCode(...bytes.subarray(i,i+32768));
        const result=await this.api("/api/imports/document",{method:"POST",body:{filename:file.name,data:btoa(raw)}});if(generation===this.importGeneration&&this.node("source-dialog").open)this.applyImport(result.preview);
      } catch(error){if(generation!==this.importGeneration||!this.node("source-dialog").open)return;this.showError("source-error",error);this.node("source-file-name").textContent="导入失败，资料未保存。";this.node("save-source").disabled=!this.node("source-truncated-control").hidden&&!this.node("source-truncated-confirm").checked;}
    });
    this.node("workstyle-form").addEventListener("submit", async e => {
      e.preventDefault(); this.showError("workstyle-error",null);
      await this.busy(this.node("save-workstyle"),async () => { try { const result = await this.api("/api/knowledge/workstyle",{method:"PUT",body:{content:this.node("workstyle-content").value,expected_version:this.workstyleVersion}}); this.workstyleVersion = result.workstyle.version; this.toast("工作方式已保存"); } catch(error) { this.showError("workstyle-error",error); } });
    });
  }
}
