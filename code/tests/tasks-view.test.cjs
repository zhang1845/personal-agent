const assert = require('node:assert/strict');
const test = require('node:test');
const T = require('../../assets/web/tasks.js');
const tasks = [
  {id:'past',due_date:'2026-10-25',completed:false,created_at:'2026-10-20'},
  {id:'today',due_date:'2026-10-26',completed:false,created_at:'2026-10-21'},
  {id:'future',due_date:'2026-10-27',completed:false,created_at:'2026-10-22'},
  {id:'none',due_date:null,completed:false,created_at:'2026-10-23'},
  {id:'done',due_date:'2026-10-25',completed:true},
  {id:'deleted',due_date:'2026-10-26',completed:false,deleted_at:'2026-10-26'}
];
test('today and overdue exclude completed, deleted and undated tasks',()=>{
  assert.deepEqual(T.select(tasks,'today','2026-10-26').map(x=>x.id),['today']);
  assert.deepEqual(T.select(tasks,'overdue','2026-10-26').map(x=>x.id),['past']);
  assert.deepEqual(T.select(tasks,'completed','2026-10-26').map(x=>x.id),['done']);
  assert.deepEqual(T.select(tasks,'deleted','2026-10-26').map(x=>x.id),['deleted']);
});
test('task day follows supplied personal timezone day, independently of selected calendar date',()=>{
  assert.deepEqual(T.select(tasks,'today','2026-10-27').map(x=>x.id),['future']);
  assert.deepEqual(T.select(tasks,'overdue','2026-10-27').map(x=>x.id),['past','today']);
});
test('due-date sorting puts undated tasks last without mutating source',()=>{
  const original=tasks.map(x=>x.id);
  assert.deepEqual(T.sort(T.select(tasks,'open','2026-10-26'),'due').map(x=>x.id),['past','today','future','none']);
  assert.deepEqual(tasks.map(x=>x.id),original);
});
test('created sorting and completion labels remain factual',()=>{
  assert.deepEqual(T.sort(tasks.slice(0,4),'newest').map(x=>x.id),['none','future','today','past']);
  assert.deepEqual(T.sort(tasks.slice(0,4),'oldest').map(x=>x.id),['past','today','future','none']);
  assert.equal(T.dueLabel(tasks[0],'2026-10-26'),'已逾期 · 2026-10-25');
  assert.equal(T.dueLabel(tasks[4],'2026-10-26'),'到期 2026-10-25');
});
