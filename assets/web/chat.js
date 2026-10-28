/* Chat attachment state stays local until the server binds IDs to a conversation. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ChatFiles = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  const MAX_FILES = 5;
  const MAX_BYTES = 8 * 1024 * 1024;
  const EXTENSIONS = new Set(["pdf", "docx", "pptx", "xlsx", "txt", "md"]);
  function validate(files, existing = 0) {
    if (!files.length) return;
    if (existing + files.length > MAX_FILES) throw new Error("每次最多添加 5 个文件，请先发送或移除文件。");
    for (const file of files) {
      const extension = String(file.name || "").split(".").pop().toLowerCase();
      if (!EXTENSIONS.has(extension)) throw new Error("支持 PDF、Word、幻灯片、表格、TXT 和 Markdown 文件。");
      if (!Number.isFinite(file.size) || file.size <= 0 || file.size > MAX_BYTES) throw new Error("文件不能为空，且每个文件不能超过 8 MB。");
    }
  }
  class Draft {
    constructor() { this.items = []; this.epoch = 0; this.uploading = false; }
    reset() { this.epoch += 1; this.items = []; this.uploading = false; }
    begin(files) {
      if (this.uploading) throw new Error("请等待文件解析完成。");
      validate(files, this.items.length);
      this.uploading = true;
      return { epoch: this.epoch };
    }
    current(ticket) { return ticket.epoch === this.epoch; }
    accept(ticket, attachment) {
      if (!this.current(ticket)) return false;
      if (!attachment || typeof attachment.id !== "string" || !attachment.id) throw new Error("文件解析结果无效，请重新添加。");
      if (this.items.some(item => item.id === attachment.id)) return false;
      if (this.items.length >= MAX_FILES) throw new Error("每次最多添加 5 个文件。");
      this.items.push(attachment);
      return true;
    }
    finish(ticket) { if (this.current(ticket)) this.uploading = false; }
    remove(id) { this.items = this.items.filter(item => item.id !== id); }
    snapshot() { return this.items.slice(); }
  }
  function message(text, attachments) { return String(text || "").trim() || (attachments.length ? "请读取附件并整理内容" : ""); }
  function reviewLines(action) {
    const review = action && action.arguments && action.arguments.review;
    return review && Array.isArray(review.lines) ? review.lines.filter(line => typeof line === "string").join("\n") : null;
  }
  function requiresTruncation(action) {
    return Boolean(action && action.tool === "assistant_save_import" && action.arguments && action.arguments.needs_truncation_confirmation === true);
  }
  function confirmation(action, checked) {
    if (requiresTruncation(action)) {
      if (!checked) throw new Error("请先核对截选内容。");
      return { confirm_truncated: true };
    }
    return {};
  }
  function merge(saved, incoming) {
    const rows = new Map();
    for (const item of [...saved, ...incoming]) if (item && typeof item.id === "string") rows.set(item.id, item);
    return [...rows.values()];
  }
  return { MAX_FILES, MAX_BYTES, validate, Draft, message, reviewLines, requiresTruncation, confirmation, merge };
});
