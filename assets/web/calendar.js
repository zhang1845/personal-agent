"use strict";

// Date-only arithmetic stays in UTC; event display uses the user's IANA timezone.
((root, factory) => {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CalendarDates = factory();
})(globalThis, () => {
  function addDays(value, count) {
    const [y, m, d] = value.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + count)).toISOString().slice(0, 10);
  }
  function weekStart(value) {
    const weekday = new Date(value + "T12:00:00Z").getUTCDay();
    return addDays(value, -(weekday === 0 ? 6 : weekday - 1));
  }
  function monthRange(value) {
    const start = value.slice(0, 7) + "-01";
    const [year, month] = start.split("-").map(Number);
    return { start, end: new Date(Date.UTC(year, month, 1)).toISOString().slice(0, 10) };
  }
  function shiftMonth(value, count) {
    const [year, month, day] = value.split("-").map(Number);
    const target = new Date(Date.UTC(year, month - 1 + count, 1));
    const last = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
    target.setUTCDate(Math.min(day, last));
    return target.toISOString().slice(0, 10);
  }
  function viewRange(value, mode) {
    if (mode === "month") {
      const month = monthRange(value);
      const start = weekStart(month.start);
      const finalWeek = weekStart(addDays(month.end, -1));
      return { start, end: addDays(finalWeek, 7) };
    }
    const start = mode === "week" ? weekStart(value) : value;
    return { start, end: addDays(start, mode === "week" ? 7 : 1) };
  }
  function queryChunks(range) {
    const result = [];
    for (let start = range.start; start < range.end;) {
      // 14 local days remain below the core's 31-day instant limit across DST.
      const end = addDays(start, 14) < range.end ? addDays(start, 14) : range.end;
      result.push({ start, end });
      start = end;
    }
    return result;
  }
  function dateKey(value, timezone) {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(value));
    const get = (name) => parts.find((part) => part.type === name).value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  }
  function coversDay(event, day, timezone) {
    const tz = event.all_day && event.timezone ? event.timezone : timezone;
    const first = dateKey(event.start, tz);
    const endDay = dateKey(event.end, tz);
    const end = new Date(event.end);
    const time = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).format(end);
    const fraction = (String(event.end).match(/\.(\d+)/) || ["", ""])[1];
    // Exact midnight is exclusive. Preserve sub-millisecond fractions when JS Date truncates them.
    const midnight = time === "00:00:00" && end.getUTCMilliseconds() === 0 && !/[1-9]/.test(fraction);
    const last = midnight ? addDays(endDay, -1) : endDay;
    return first <= day && day <= last;
  }
  function eventsForDay(events, day, timezone) {
    return events.filter((event) => coversDay(event, day, timezone)).sort((a, b) => Number(b.all_day) - Number(a.all_day) || Date.parse(a.start) - Date.parse(b.start));
  }
  return { addDays, weekStart, monthRange, shiftMonth, viewRange, queryChunks, dateKey, coversDay, eventsForDay };
});
