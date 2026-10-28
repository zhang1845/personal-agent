/* Date-only task views: callers supply "today" in the user's selected timezone. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TaskLists = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  function select(tasks, filter, today) {
    return tasks.filter((task) => {
      if (filter === "deleted") return Boolean(task.deleted_at);
      if (task.deleted_at) return false;
      if (filter === "completed") return task.completed;
      if (task.completed) return false;
      if (filter === "today") return task.due_date === today;
      if (filter === "overdue") return Boolean(task.due_date && task.due_date < today);
      return true;
    });
  }
  function sort(tasks, order = "due") {
    return tasks.slice().sort((a, b) => {
      if (order === "due") {
        const due = (a.due_date || "9999-99-99").localeCompare(b.due_date || "9999-99-99");
        if (due) return due;
      }
      const created = (a.created_at || "").localeCompare(b.created_at || "");
      return (order === "oldest" ? created : -created) || String(a.id).localeCompare(String(b.id));
    });
  }
  function dueLabel(task, today) {
    if (!task.due_date) return "";
    if (!task.completed && task.due_date < today) return "已逾期 · " + task.due_date;
    if (!task.completed && task.due_date === today) return "今天到期";
    return "到期 " + task.due_date;
  }
  return { select, sort, dueLabel };
});
