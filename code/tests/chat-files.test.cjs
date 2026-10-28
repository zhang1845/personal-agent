const assert = require('node:assert/strict');
const test = require('node:test');
const C = require('../../assets/web/chat.js');

test('attachment validation accepts Windows names and rejects unsafe formats and limits', () => {
  C.validate([{ name: '方案 评审.PDF', size: C.MAX_BYTES }, { name: '会议记录.docx', size: 1 }]);
  assert.throws(() => C.validate([{ name: 'macro.exe', size: 10 }]));
  assert.throws(() => C.validate([{ name: 'empty.txt', size: 0 }]));
  assert.throws(() => C.validate([{ name: 'large.pdf', size: C.MAX_BYTES + 1 }]));
  assert.throws(() => C.validate([{ name: 'another.md', size: 1 }], 5));
});

test('late uploads cannot enter a different conversation or unlock a newer upload', () => {
  const draft = new C.Draft();
  const previous = draft.begin([{ name: 'old.md', size: 5 }]);
  draft.reset();
  const current = draft.begin([{ name: 'new.md', size: 5 }]);
  assert.equal(draft.accept(previous, { id: 'old', filename: 'old.md' }), false);
  draft.finish(previous);
  assert.equal(draft.uploading, true);
  assert.equal(draft.accept(current, { id: 'new', filename: 'new.md' }), true);
  draft.finish(current);
  assert.equal(draft.uploading, false);
  assert.deepEqual(draft.items.map(item => item.id), ['new']);
});

test('failed sends can retain uploaded IDs while success starts a clean draft', () => {
  const draft = new C.Draft();
  const ticket = draft.begin([{ name: 'notes.txt', size: 10 }]);
  draft.accept(ticket, { id: 'file-1', filename: 'notes.txt' });
  draft.finish(ticket);
  const sent = draft.snapshot();
  assert.equal(C.message('', sent), '请读取附件并整理内容');
  assert.equal(C.message('  根据附件生成待办  ', sent), '根据附件生成待办');
  assert.equal(C.message('', []), '');
  assert.deepEqual(draft.snapshot().map(item => item.id), ['file-1']);
  draft.reset();
  assert.deepEqual(draft.items, []);
  assert.equal(sent[0].id, 'file-1');
});

test('truncated imports require fresh explicit human acknowledgement', () => {
  const action = { tool: 'assistant_save_import', arguments: { needs_truncation_confirmation: true } };
  assert.throws(() => C.confirmation(action, false));
  assert.deepEqual(C.confirmation(action, true), { confirm_truncated: true });
  assert.deepEqual(C.confirmation({ tool: 'assistant_sync_calendar', arguments: {} }, false), {});
});

test('review and attachment history preserve text and deduplicate only stable IDs', () => {
  assert.equal(C.reviewLines({ arguments: { review: { lines: ['同步日期：2030-01-01', '<script>原文</script>', 42] } } }), '同步日期：2030-01-01\n<script>原文</script>');
  const combined = C.merge([{ id: 'a', filename: '旧标题' }], [{ id: 'a', filename: '新标题' }, { id: 'b', filename: '新标题' }]);
  assert.deepEqual(combined.map(item => item.filename), ['新标题', '新标题']);
});
