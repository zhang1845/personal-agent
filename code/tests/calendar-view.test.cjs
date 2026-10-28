const assert = require('node:assert/strict');
const test = require('node:test');
const C = require('../../assets/web/calendar.js');

test('month navigation clamps the day across leap years and year boundaries', () => {
  assert.equal(C.shiftMonth('2024-01-31', 1), '2024-02-29');
  assert.equal(C.shiftMonth('2026-01-31', 1), '2026-02-28');
  assert.equal(C.shiftMonth('2026-12-31', 1), '2027-01-31');
  assert.equal(C.shiftMonth('2026-03-31', -1), '2026-02-28');
});
test('a six-row month includes leading/trailing dates with Monday first', () => {
  assert.deepEqual(C.viewRange('2026-08-15', 'month'), {start:'2026-07-27',end:'2026-09-07'});
  assert.deepEqual(C.viewRange('2026-10-26', 'month'), {start:'2026-09-28',end:'2026-11-02'});
  assert.deepEqual(C.viewRange('2021-02-14', 'month'), {start:'2021-02-01',end:'2021-03-01'});
  assert.deepEqual(C.viewRange('2026-11-01', 'week'), {start:'2026-10-26',end:'2026-11-02'});
});
test('visible 42-day range is queried completely in disjoint safe intervals', () => {
  const range=C.viewRange('2026-08-31', 'month'), parts=C.queryChunks(range);
  assert.equal(parts[0].start,range.start);
  assert.equal(parts.at(-1).end,range.end);
  assert.equal(parts.length,3);
  parts.forEach((p,i)=>{assert.ok(Date.parse(p.end)-Date.parse(p.start)<=14*86400000);if(i)assert.equal(parts[i-1].end,p.start);});
});
test('midnight ends exclude the next day and cross-midnight events appear twice', () => {
  const event={start:'2026-10-26T15:30:00Z',end:'2026-10-26T16:00:00Z',all_day:false};
  assert.equal(C.coversDay(event,'2026-10-26','Asia/Shanghai'),true);
  assert.equal(C.coversDay(event,'2026-10-27','Asia/Shanghai'),false);
  assert.equal(C.coversDay({...event,end:'2026-10-26T16:30:00Z'},'2026-10-27','Asia/Shanghai'),true);
  assert.equal(C.coversDay({...event,end:'2026-10-26T16:00:00.000001Z'},'2026-10-27','Asia/Shanghai'),true);
});
test('all-day events retain the calendar date in their saved timezone', () => {
  const event={start:'2026-10-25T16:00:00Z',end:'2026-10-27T16:00:00Z',all_day:true,timezone:'Asia/Shanghai'};
  assert.equal(C.coversDay(event,'2026-10-26','America/Los_Angeles'),true);
  assert.equal(C.coversDay(event,'2026-10-27','America/Los_Angeles'),true);
  assert.equal(C.coversDay(event,'2026-10-28','America/Los_Angeles'),false);
});
test('DST dates use personal timezone and all-day events are ordered first', () => {
  const event={id:'timed',start:'2026-11-01T05:30:00Z',end:'2026-11-01T06:30:00Z',all_day:false};
  assert.equal(C.coversDay(event,'2026-11-01','America/New_York'),true);
  assert.equal(C.coversDay(event,'2026-10-31','America/Los_Angeles'),true);
  const allDay={id:'all',start:'2026-11-01T04:00:00Z',end:'2026-11-02T05:00:00Z',all_day:true,timezone:'America/New_York'};
  assert.deepEqual(C.eventsForDay([event,allDay],'2026-11-01','America/New_York').map(e=>e.id),['all','timed']);
});
