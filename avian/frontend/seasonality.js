/* Optional daily occurrence charts. apt.js owns all verified snapshot loading;
   this module has no network calls, source paths or deployment configuration. */
(function (scope) {
  'use strict';
  var DAY_MS = 86400000;
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var own = function (value, key) { return Object.prototype.hasOwnProperty.call(value, key); };
  var object = function (value) { return value && typeof value === 'object' && !Array.isArray(value); };
  var integer = function (value, low, high) { return Number.isSafeInteger(value) && value >= low && value <= high; };
  var text = function (value) { return typeof value === 'string' && value.length >= 1 && value.length <= 200 && !/[\u0000-\u001f\u007f]/.test(value); };
  function keys(value, expected) { return object(value) && JSON.stringify(Object.keys(value).sort()) === JSON.stringify(expected.slice().sort()); }
  function invalid() { throw new Error('Invalid seasonality data'); }

  function civilDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) invalid();
    var parts = value.split('-').map(Number), date = new Date(0);
    if (parts[0] < 1) invalid();
    date.setUTCFullYear(parts[0], parts[1] - 1, parts[2]); date.setUTCHours(0, 0, 0, 0);
    if (date.getUTCFullYear() !== parts[0] || date.getUTCMonth() !== parts[1] - 1 || date.getUTCDate() !== parts[2]) invalid();
    return date;
  }
  function dateKey(date) { return String(date.getUTCFullYear()).padStart(4, '0') + '-' + String(date.getUTCMonth() + 1).padStart(2, '0') + '-' + String(date.getUTCDate()).padStart(2, '0'); }
  function addDays(value, amount) { var date = civilDate(value); date.setUTCDate(date.getUTCDate() + amount); return dateKey(date); }
  // UTC backs civil arithmetic only. Neither DST nor the visitor's timezone
  // can move the recorder-local date represented by a chart position.
  function calendarDays(start, end) {
    var count = Math.round((civilDate(end) - civilDate(start)) / DAY_MS) + 1;
    if (count < 1 || count > 366) invalid();
    return Array.from({ length: count }, function (_, offset) { return addDays(start, offset); });
  }
  function annualStart(end) {
    var date = civilDate(end), month = date.getUTCMonth(), day = date.getUTCDate();
    if (date.getUTCFullYear() <= 1) invalid();
    date.setUTCDate(1); date.setUTCFullYear(date.getUTCFullYear() - 1);
    var last = new Date(date.getTime()); last.setUTCMonth(month + 1, 0);
    date.setUTCDate(Math.min(day, last.getUTCDate()) + 1); return dateKey(date);
  }
  function dateLabel(value, year) { var date = civilDate(value); return MONTHS[date.getUTCMonth()] + ' ' + date.getUTCDate() + (year ? ', ' + date.getUTCFullYear() : ''); }
  function denseCounts(counts, length) {
    if (!Array.isArray(counts) || counts.length > length) invalid();
    var result = Array(length).fill(0), previous = -1;
    counts.forEach(function (entry) {
      if (!Array.isArray(entry) || entry.length !== 2 || !integer(entry[0], 0, length - 1) || entry[0] <= previous || !integer(entry[1], 1, 200000)) invalid();
      previous = entry[0]; result[entry[0]] = entry[1];
    });
    return result;
  }
  function peakOffset(counts) {
    var width = Math.min(28, counts.length), sum = 0, maximum = -1, start = 0;
    for (var i = 0; i < counts.length; i++) { sum += counts[i]; if (i >= width) sum -= counts[i - width]; if (i >= width - 1 && sum > maximum) { maximum = sum; start = i - width + 1; } }
    return start + Math.floor((width - 1) / 2);
  }
  function rankedSpecies(species, length, mode) {
    return species.map(function (bird) { var counts = denseCounts(bird.counts, length); return { sci: bird.sci, com: bird.com, total: bird.total, detectedDays: bird.detected_days, counts: counts, peak: peakOffset(counts) }; }).sort(function (a, b) {
      var order = mode === 'season' ? a.peak - b.peak : b.detectedDays - a.detectedDays;
      return order || b.detectedDays - a.detectedDays || b.total - a.total || a.com.localeCompare(b.com) || a.sci.localeCompare(b.sci);
    });
  }

  function indexEnd(index) {
    if (!index || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$/.test(index.as_of) || !Number.isFinite(Date.parse(index.as_of)) || !text(index.timezone)) invalid();
    var parts;
    try { parts = new Intl.DateTimeFormat('en-CA', { timeZone: index.timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(index.as_of)); } catch (error) { invalid(); }
    var fields = Object.create(null); parts.forEach(function (part) { fields[part.type] = part.value; });
    var local = fields.year.padStart(4, '0') + '-' + fields.month + '-' + fields.day + 'T' + fields.hour + ':' + fields.minute + ':' + fields.second;
    if (local !== index.as_of.slice(0, 19)) invalid();
    civilDate(local.slice(0, 10)); return local.slice(0, 10);
  }

  function validateIndex(index) {
    if (!object(index) || index.schema_version !== 3 || !Array.isArray(index.capabilities) || index.capabilities.length !== 1 || index.capabilities[0] !== 'seasonality-v1' ||
        typeof index.generation !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/.test(index.generation) ||
        typeof index.published_at !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$/.test(index.published_at) || !Number.isFinite(Date.parse(index.published_at)) ||
        !keys(index.compatibility, ['last_hour']) || ['rolling', 'php_today_only'].indexOf(index.compatibility.last_hour) < 0 ||
        !keys(index.routes, ['stats', 'lifelist', 'recent', 'timeseries', 'firstseen', 'species', 'seasonality']) || !object(index.objects)) invalid();
    var end = indexEnd(index); annualStart(end);
    if (!keys(index.routes.recent, ['1', '12', '24', '168', '1000000']) || !keys(index.routes.timeseries, ['30']) || !keys(index.routes.firstseen, ['10']) || !object(index.routes.species) || Object.keys(index.routes.species).length > 500) invalid();
    var references = [index.routes.stats, index.routes.lifelist, index.routes.seasonality];
    ['recent', 'timeseries', 'firstseen', 'species'].forEach(function (action) { Object.keys(index.routes[action]).forEach(function (key) { if (action === 'species' && !text(key)) invalid(); references.push(index.routes[action][key]); }); });
    var inventory = Object.keys(index.objects);
    if (inventory.length < 1 || inventory.length > 512 || references.some(function (path) { return typeof path !== 'string'; }) ||
        JSON.stringify(Array.from(new Set(references)).sort()) !== JSON.stringify(inventory.sort())) invalid();
    var indexBytes = new TextEncoder().encode(JSON.stringify(index) + '\n').length, total = 2 * indexBytes;
    if (indexBytes > 1024 * 1024) invalid();
    inventory.forEach(function (path) { var match = /^objects\/([a-f0-9]{64})\.json$/.exec(path), entry = index.objects[path]; if (!match || !keys(entry, ['sha256', 'bytes']) || entry.sha256 !== match[1] || !integer(entry.bytes, 2, 2 * 1024 * 1024)) invalid(); total += entry.bytes; });
    if (total > 4 * 1024 * 1024) invalid();
    return index;
  }

  function validatePayload(payload, request, index) {
    if (!request || request.action !== 'seasonality' || !object(payload) || payload.format !== 'seasonality-v1') invalid();
    var allowed = ['format', 'start_date', 'end_date', 'coverage', 'species', 'totals', 'excluded_invalid_rows', 'snapshot', 'as_of'];
    if (Object.keys(payload).some(function (key) { return allowed.indexOf(key) < 0; })) invalid();
    var end = indexEnd(index), start = annualStart(end), dates = calendarDays(start, end);
    if (payload.start_date !== start || payload.end_date !== end || !keys(payload.coverage, ['basis', 'default', 'partial_dates', 'first_observation_date']) ||
        payload.coverage.basis !== 'detections_do_not_establish_uptime' || payload.coverage.default !== 'unknown' ||
        !Array.isArray(payload.coverage.partial_dates) || payload.coverage.partial_dates.length !== 1 || payload.coverage.partial_dates[0] !== end ||
        !keys(payload.totals, ['detections']) || !integer(payload.totals.detections, 0, 200000) || !integer(payload.excluded_invalid_rows, 0, 200000) ||
        payload.totals.detections + payload.excluded_invalid_rows > 200000 ||
        !Array.isArray(payload.species) || payload.species.length > 500) invalid();
    var total = 0, first = null, names = new Set();
    payload.species.forEach(function (bird) {
      if (!keys(bird, ['sci', 'com', 'total', 'detected_days', 'counts']) || !text(bird.sci) || !text(bird.com) || names.has(bird.sci) || !integer(bird.total, 1, 200000) || !integer(bird.detected_days, 1, dates.length)) invalid();
      names.add(bird.sci); var dense = denseCounts(bird.counts, dates.length), sum = dense.reduce(function (count, value) { return count + value; }, 0);
      if (sum !== bird.total || bird.counts.length !== bird.detected_days) invalid();
      var observed = dates[bird.counts[0][0]]; if (first === null || observed < first) first = observed;
      total += sum;
    });
    if (total !== payload.totals.detections || payload.coverage.first_observation_date !== first) invalid();
    return payload;
  }

  // One active request plus one newest queued generation. An old response can
  // never replace the selected generation or redraw a closed view.
  function latestTask(loader, onValue, onError) {
    var version = 0, running = false, waiting = null, stopped = false;
    function run(job) {
      running = true;
      Promise.resolve().then(function () { return loader(job.value); }).then(function (value) { if (!stopped && job.version === version) onValue(value, job.value); }, function (error) { if (!stopped && job.version === version) onError(error, job.value); }).finally(function () { running = false; if (!stopped && waiting) { var next = waiting; waiting = null; run(next); } });
    }
    return { request: function (value) { if (stopped) return; var job = { value: value, version: ++version }; if (running) waiting = job; else run(job); }, invalidate: function () { version++; waiting = null; }, destroy: function () { stopped = true; version++; waiting = null; } };
  }

  function create(adapter) {
    if (!adapter || !adapter.root || typeof adapter.getIndex !== 'function' || typeof adapter.loadSeasonality !== 'function') throw new Error('Seasonality requires a snapshot adapter');
    var root = adapter.root, doc = root.ownerDocument, currentIndex = null, dataIndex = null, data = null, active = false, destroyed = false;
    var sort = 'density', selectedDate = null, observer = null, mounted = [], loading = false, failure = false, nextId = 0;
    function node(tag, className, value) { var element = doc.createElement(tag); if (className) element.className = className; if (value !== undefined) element.textContent = value; return element; }
    function button(value) { var element = node('button', 'seasonality-control', value); element.type = 'button'; return element; }
    function clear(element) { while (element.firstChild) element.removeChild(element.firstChild); }
    function same(index) { var live = adapter.getIndex(); return !!(live && index && currentIndex && live.generation === index.generation && live.as_of === index.as_of && currentIndex.generation === index.generation && currentIndex.as_of === index.as_of); }
    function stopObserving() { if (observer) observer.disconnect(); observer = null; mounted = []; }
    function verifiedImage(value) { try { var url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && !url.hash ? url.href : ''; } catch (error) { return ''; } }
    function svg(tag, attributes) { var element = doc.createElementNS('http://www.w3.org/2000/svg', tag); Object.keys(attributes || {}).forEach(function (key) { element.setAttribute(key, attributes[key]); }); return element; }
    function status(value) { var element = node('p', 'seasonality-empty', value); element.setAttribute('role', 'status'); return element; }

    function setDate(value) {
      if (!data || value < data.start_date || value > data.end_date) return;
      try { civilDate(value); } catch (error) { return; }
      selectedDate = value;
      var picker = root.querySelector('input[type="date"]'); if (picker) picker.value = value;
      mounted.forEach(function (chart) { chart.inspect(value); });
    }

    function chartFor(bird, host, dates, readout) {
      var width = 960, height = 170, left = 40, right = 12, top = 15, bottom = 30, span = width - left - right;
      var maximum = Math.max.apply(null, [1].concat(bird.counts));
      var x = function (offset) { return left + offset / Math.max(1, dates.length - 1) * span; };
      var y = function (count) { return top + (1 - count / maximum) * (height - top - bottom); };
      var plot = svg('svg', { viewBox: '0 0 ' + width + ' ' + height, role: 'img', 'aria-label': bird.com + ': daily recorded occurrences, ' + dateLabel(data.start_date, true) + ' through ' + dateLabel(data.end_date, true) + '. Use the inspect-day date field for exact counts.' }); plot.classList.add('seasonality-chart');
      var first = data.coverage.first_observation_date, firstOffset = first ? dates.indexOf(first) : dates.length;
      if (firstOffset > 0) { plot.appendChild(svg('rect', { x: left, y: top, width: Math.max(0, x(Math.min(firstOffset, dates.length - 1)) - left), height: height - top - bottom, 'class': 'seasonality-history' })); }
      [0, Math.round(maximum / 2), maximum].filter(function (value, index, all) { return all.indexOf(value) === index; }).forEach(function (value) { plot.appendChild(svg('line', { x1: left, x2: width - right, y1: y(value), y2: y(value), 'class': 'seasonality-chart-grid' })); var label = svg('text', { x: left - 8, y: y(value) + 3, 'text-anchor': 'end', 'class': 'seasonality-axis-label' }); label.textContent = value.toLocaleString(); plot.appendChild(label); });
      var priorMonth = -1;
      dates.forEach(function (date, offset) { var month = civilDate(date).getUTCMonth(); if (month !== priorMonth) { priorMonth = month; var label = svg('text', { x: x(offset), y: height - 8, 'text-anchor': offset === 0 ? 'start' : 'middle', 'class': 'seasonality-axis-label' }); label.textContent = MONTHS[month]; plot.appendChild(label); } });
      var previous = null;
      bird.counts.forEach(function (count, offset) {
        if (!count) { previous = null; return; }
        var partial = dates[offset] === data.end_date;
        var bar = svg('rect', { x: x(offset) - .8, y: y(count), width: 1.6, height: y(0) - y(count), 'class': 'seasonality-count-bar' + (partial ? ' seasonality-partial' : '') });
        var title = svg('title'); title.textContent = dateLabel(dates[offset], true) + ': ' + count.toLocaleString() + ' recorded occurrences' + (partial ? ' (partial day)' : ''); bar.appendChild(title); plot.appendChild(bar);
        if (previous !== null) plot.appendChild(svg('line', { x1: x(previous), y1: y(bird.counts[previous]), x2: x(offset), y2: y(count), 'class': 'seasonality-count-line' }));
        var point = svg('circle', { cx: x(offset), cy: y(count), r: 1.65, 'class': 'seasonality-count-point' }); plot.appendChild(point); previous = offset;
      });
      var guide = svg('line', { x1: left, x2: left, y1: top, y2: y(0), 'class': 'seasonality-inspect-guide' }); plot.appendChild(guide);
      function inspect(date) { var offset = dates.indexOf(date); if (offset < 0) return; guide.setAttribute('x1', x(offset)); guide.setAttribute('x2', x(offset)); readout.textContent = dateLabel(date) + ' · ' + bird.counts[offset].toLocaleString() + ' recorded' + (date === data.end_date ? ' · partial day' : first && date < first || !first ? ' · no recorded history' : ''); }
      function pointedDate(event) { var bounds = plot.getBoundingClientRect(), position = (event.clientX - bounds.left) / bounds.width * width; if (position < left || position > width - right) return null; return dates[Math.max(0, Math.min(dates.length - 1, Math.round((position - left) / span * (dates.length - 1))))]; }
      plot.addEventListener('pointermove', function (event) { var date = pointedDate(event); if (date) inspect(date); });
      plot.addEventListener('pointerleave', function () { inspect(selectedDate); });
      plot.addEventListener('click', function (event) { var date = pointedDate(event); if (date) setDate(date); });
      host.appendChild(plot); inspect(selectedDate);
      // Mobile charts keep readable daily spacing. Start near the inspected
      // day so a short recorder history is visible without a blank first swipe.
      if (host.clientWidth && plot.clientWidth > host.clientWidth) host.scrollLeft = Math.max(0, x(dates.indexOf(selectedDate)) / width * plot.clientWidth - host.clientWidth / 2);
      return { host: host, inspect: inspect };
    }

    function render() {
      if (!data || destroyed) return; stopObserving(); clear(root);
      var dates = calendarDays(data.start_date, data.end_date);
      if (!selectedDate || selectedDate < data.start_date || selectedDate > data.end_date) selectedDate = data.end_date;
      var controls = node('div', 'seasonality-controls'), order = node('div', 'seasonality-toggle'); order.setAttribute('role', 'group'); order.setAttribute('aria-label', 'Order birds');
      [['density', 'Frequency density'], ['season', 'Seasonal peak']].forEach(function (option) { var choice = button(option[1]); choice.setAttribute('aria-pressed', String(sort === option[0])); choice.onclick = function () { sort = option[0]; render(); }; order.appendChild(choice); }); controls.appendChild(order);
      var inspector = node('div', 'seasonality-inspector'), label = node('label', '', 'Inspect day'); var id = 'seasonalityInspectDate-' + (++nextId); label.htmlFor = id; inspector.appendChild(label);
      var previous = button('←'); previous.setAttribute('aria-label', 'Inspect previous day'); previous.onclick = function () { if (selectedDate > data.start_date) setDate(addDays(selectedDate, -1)); }; inspector.appendChild(previous);
      var picker = node('input', ''); picker.type = 'date'; picker.id = id; picker.min = data.start_date; picker.max = data.end_date; picker.value = selectedDate; picker.addEventListener('change', function () { setDate(picker.value); }); inspector.appendChild(picker);
      var next = button('→'); next.setAttribute('aria-label', 'Inspect next day'); next.onclick = function () { if (selectedDate < data.end_date) setDate(addDays(selectedDate, 1)); }; inspector.appendChild(next); controls.appendChild(inspector); root.appendChild(controls);
      root.appendChild(node('p', 'seasonality-window', dateLabel(data.start_date, true) + ' — ' + dateLabel(data.end_date, true) + ' · ' + dataIndex.timezone));
      root.appendChild(node('p', 'seasonality-caption', 'Occurrences per day, across the last 12 months.'));
      root.appendChild(node('p', 'seasonality-coverage', 'Counts are recorded detections, not individual birds. Empty days mean no detections recorded; recorder coverage is unknown. The shaded beginning has no recorded history, and today is partial. Each bird has its own count scale.'));
      if (!same(dataIndex)) root.appendChild(status((failure ? 'The latest calendar could not be loaded. ' : 'Updating the calendar… ') + 'Showing counts captured ' + dataIndex.as_of + '.'));
      var list = node('div', 'seasonality-list'); root.appendChild(list);
      var ranked = rankedSpecies(data.species, dates.length, sort);
      if (!ranked.length) { list.appendChild(status('No bird occurrences have been recorded in this calendar.')); return; }
      var Observer = doc.defaultView && doc.defaultView.IntersectionObserver;
      if (Observer) observer = new Observer(function (entries) { entries.forEach(function (entry) { var host = entry.target; if (!root.contains(host)) return; if (entry.isIntersecting && !host._seasonChart) { host._seasonChart = chartFor(host._seasonBird, host, dates, host._seasonReadout); mounted.push(host._seasonChart); } else if (!entry.isIntersecting && host._seasonChart) { mounted = mounted.filter(function (chart) { return chart !== host._seasonChart; }); clear(host); host._seasonChart = null; } }); }, { root: root.closest('.view'), rootMargin: '220px 0px' });
      var fallbackPending = [];
      ranked.forEach(function (bird, index) {
        var row = node('article', 'seasonality-bird'), portrait = node('div', 'seasonality-portrait'); var url = verifiedImage(adapter.imageUrl && adapter.imageUrl(bird.sci));
        if (url) { var image = node('img', ''); image.src = url; image.alt = bird.com; image.loading = 'lazy'; image.decoding = 'async'; portrait.appendChild(image); } else portrait.appendChild(node('span', 'seasonality-portrait-empty', 'illustration unavailable')); row.appendChild(portrait);
        var content = node('div', 'seasonality-bird-content'), heading = node('header', 'seasonality-bird-heading'), names = node('div', ''); names.appendChild(node('h3', '', bird.com)); names.appendChild(node('p', 'seasonality-scientific', bird.sci)); heading.appendChild(names);
        heading.appendChild(node('p', 'seasonality-bird-metric', bird.total.toLocaleString() + ' occurrences · ' + bird.detectedDays + '/' + dates.length + ' days')); content.appendChild(heading);
        var readout = node('p', 'seasonality-day-value', ''); content.appendChild(readout);
        var host = node('div', 'seasonality-chart-scroll'); host._seasonBird = bird; host._seasonReadout = readout; content.appendChild(host); row.appendChild(content); list.appendChild(row);
        if (observer) observer.observe(host); else if (index < 10) mounted.push(chartFor(bird, host, dates, readout)); else { row.hidden = true; fallbackPending.push({ row: row, bird: bird, host: host, readout: readout }); }
      });
      if (fallbackPending.length) { var more = button('Show more birds'); more.classList.add('seasonality-more'); more.onclick = function () { fallbackPending.splice(0, 10).forEach(function (entry) { entry.row.hidden = false; mounted.push(chartFor(entry.bird, entry.host, dates, entry.readout)); }); if (!fallbackPending.length) more.remove(); }; list.appendChild(more); }
    }

    var tasks = latestTask(function (index) { return adapter.loadSeasonality(index); }, function (next, index) {
      if (destroyed || !same(index)) return;
      try { validatePayload(next, { action: 'seasonality' }, index); data = next; dataIndex = index; loading = false; failure = false; if (active) render(); } catch (error) { loading = false; failure = true; if (active) { if (data) render(); else { clear(root); root.appendChild(status('The bird calendar is temporarily unavailable.')); } } }
    }, function (_, index) { if (destroyed || !same(index)) return; loading = false; failure = true; if (active) { if (data) render(); else { clear(root); root.appendChild(status('The bird calendar is temporarily unavailable. Try this tab again in a moment.')); } } });
    function load() { if (!active || destroyed || !currentIndex || loading) return; loading = true; failure = false; if (!data) { clear(root); root.appendChild(status('Loading the bird calendar…')); } else render(); tasks.request(currentIndex); }
    return { setIndex: function (index) { if (destroyed) return; var changed = !currentIndex || !index || currentIndex.generation !== index.generation || currentIndex.as_of !== index.as_of; currentIndex = index; if (changed) { tasks.invalidate(); loading = false; if (active) load(); } }, show: function () { if (destroyed) return; active = true; if (data && same(dataIndex)) render(); else load(); }, hide: function () { active = false; stopObserving(); }, destroy: function () { destroyed = true; tasks.destroy(); stopObserving(); clear(root); } };
  }

  var api = { create: create, civilDate: civilDate, addDays: addDays, annualStart: annualStart, calendarDays: calendarDays, denseCounts: denseCounts, peakOffset: peakOffset, rankedSpecies: rankedSpecies, validateIndex: validateIndex, validatePayload: validatePayload, latestTask: latestTask };
  if (typeof module === 'object' && module.exports) module.exports = api; else scope.AvianSeasonality = Object.freeze(api);
})(typeof window === 'object' ? window : globalThis);
