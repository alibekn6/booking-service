// On-sale rush: BUYERS (default 10 000) buyers wait for a 1000-seat concert to open.
//
//   make loadtest     (= docker compose --profile loadtest run --rm k6 + invariant audit)
//
// Every buyer is an independent async journey; VUS runtimes (default 200) each drive
// BUYERS/VUS of them concurrently with http.asyncRequest. That keeps 10 000 truly
// simultaneous buyers (each with its own requests in flight) without 10 000 JS
// runtimes, which would not fit into memory next to the system under test.
//
// A buyer arrives around the start time, looks at the seat map, holds 1-4 seats (75%
// pick specific seats near the front, 25% "any N in a category"), retries with a fresh
// map when someone was faster, then pays (confirm; 30% double-click), cancels, or walks
// away. Afterwards `python -m app.cli check-invariants` audits the DB.
import http from 'k6/http';
import crypto from 'k6/crypto';
import encoding from 'k6/encoding';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';
import { setTimeout } from 'k6/timers';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const API = `${BASE}/api/v1`;
const SECRET = __ENV.JWT_SECRET || 'dev-only-secret-change-me-0123456789abcdef';
const BUYERS = parseInt(__ENV.BUYERS || '10000', 10);
const VUS = Math.min(parseInt(__ENV.VUS || '200', 10), BUYERS);
const SEATS = parseInt(__ENV.SEATS || '1000', 10);
const SEATS_PER_ROW = parseInt(__ENV.SEATS_PER_ROW || '40', 10);
const SALE_DELAY_S = parseInt(__ENV.SALE_DELAY_S || '20', 10);
const REACTION_S = parseFloat(__ENV.REACTION_S || '3'); // buyers click within N s of opening
const MAX_ATTEMPTS = parseInt(__ENV.MAX_ATTEMPTS || '4', 10);

export const options = {
  setupTimeout: '120s',
  scenarios: {
    rush: { executor: 'per-vu-iterations', vus: VUS, iterations: 1, maxDuration: '10m' },
  },
  thresholds: {
    hold_ms: ['p(95)<300'],
    server_error_rate: ['rate<=0.001'],
    idempotency_violations: ['count==0'],
  },
};

// --- metrics ------------------------------------------------------------------------------

const OPS = ['seatmap', 'hold', 'confirm', 'release'];
const latency = Object.fromEntries(OPS.map((op) => [op, new Trend(`${op}_ms`, true)]));
const OUTCOMES = {
  seatmap: ['200'],
  hold: ['201', 'SEATS_UNAVAILABLE', 'NOT_ENOUGH_SEATS', 'HOLD_LIMIT_EXCEEDED',
    'SALE_NOT_STARTED', 'RATE_LIMITED'],
  confirm: ['200', 'HOLD_EXPIRED'],
  release: ['204'],
};
const outcome = {};
for (const op of OPS) {
  for (const code of [...OUTCOMES[op], 'other_4xx', 'SERVICE_BUSY_503', 'other_5xx', 'network']) {
    outcome[`${op}:${code}`] = new Counter(`out_${op}_${code}`);
  }
}
const serverErrorRate = new Rate('server_error_rate'); // share of 5xx + network errors
const idempotencyViolations = new Counter('idempotency_violations');
const rushRequests = new Counter('rush_requests'); // requests in the first 10 s of the sale
const JOURNEYS = ['bought', 'released', 'abandoned', 'gave_up', 'sold_out'];
const journeys = Object.fromEntries(JOURNEYS.map((j) => [j, new Counter(`journey_${j}`)]));

// --- helpers ------------------------------------------------------------------------------

const sleep = (s) => new Promise((resolve) => setTimeout(resolve, Math.max(0, s * 1000)));

function uuid4() {
  const b = new Uint8Array(crypto.randomBytes(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function jwt(sub, role) {
  const enc = (o) => encoding.b64encode(JSON.stringify(o), 'rawurl');
  const now = Math.floor(Date.now() / 1000);
  const unsigned = `${enc({ alg: 'HS256', typ: 'JWT' })}.${enc({ sub, role, iat: now, exp: now + 3600 })}`;
  return `${unsigned}.${crypto.hmac('sha256', SECRET, unsigned, 'base64rawurl')}`;
}

function errorCode(res) {
  try {
    return res.json('error.code');
  } catch (_) {
    return undefined;
  }
}

function record(op, res, saleStartsAtMs) {
  const sinceOpen = Date.now() - saleStartsAtMs;
  if (sinceOpen >= 0 && sinceOpen < 10000) rushRequests.add(1);
  if (res.status !== 0) latency[op].add(res.timings.duration);
  let key;
  if (res.status === 0) key = 'network';
  else if (res.status >= 500) key = res.status === 503 ? 'SERVICE_BUSY_503' : 'other_5xx';
  else if (res.status < 300) key = String(res.status);
  else key = errorCode(res) || 'other_4xx';
  serverErrorRate.add(res.status === 0 || res.status >= 500);
  (outcome[`${op}:${key}`] || outcome[`${op}:other_4xx`]).add(1);
}

async function call(op, method, url, body, params, saleStartsAtMs) {
  const res = await http.asyncRequest(method, url, body, {
    ...params,
    tags: { op },
    // Business refusals are expected answers, not failures.
    responseCallback: http.expectedStatuses({ min: 200, max: 499 }),
  });
  record(op, res, saleStartsAtMs);
  return res;
}

function pickSeats(free, want) {
  // Most people aim at the front: an anchor biased to the first rows, then its
  // neighbours in the same row, as a seat-map UI would suggest.
  const anchor = free[Math.floor(Math.pow(Math.random(), 2) * free.length)];
  const row = free.filter((s) => s.row === anchor.row).sort((a, b) => a.number - b.number);
  const i = row.findIndex((s) => s.id === anchor.id);
  return row.slice(i, i + want).map((s) => s.id);
}

function wantedSeats() {
  const r = Math.random();
  return r < 0.3 ? 1 : r < 0.7 ? 2 : r < 0.85 ? 3 : 4;
}

// --- lifecycle ----------------------------------------------------------------------------

export function setup() {
  const rows = Math.ceil(SEATS / SEATS_PER_ROW);
  const runs = [];
  for (let r = 1, left = SEATS; r <= rows; r++, left -= SEATS_PER_ROW) {
    const category = r <= rows * 0.2 ? 'VIP' : r <= rows * 0.6 ? 'PARTERRE' : 'BALCONY';
    runs.push({ row: String(r), seat_from: 1, seat_to: Math.min(SEATS_PER_ROW, left), category });
  }
  const saleStartsAt = new Date(Date.now() + SALE_DELAY_S * 1000);
  const res = http.post(
    `${API}/events`,
    JSON.stringify({
      name: `Load test ${new Date().toISOString()}`,
      venue: 'k6 arena',
      starts_at: new Date(Date.now() + 30 * 86400e3).toISOString(),
      sale_starts_at: saleStartsAt.toISOString(),
      categories: [
        { code: 'VIP', name: 'VIP', price: '15000.00' },
        { code: 'PARTERRE', name: 'Parterre', price: '8000.00' },
        { code: 'BALCONY', name: 'Balcony', price: '4000.00' },
      ],
      rows: runs,
    }),
    {
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${jwt(uuid4(), 'organizer')}`,
      },
    },
  );
  if (res.status !== 201) throw new Error(`cannot create event: ${res.status} ${res.body}`);
  const event = res.json();
  console.log(`event ${event.id}: ${SEATS} seats, ${BUYERS} buyers on ${VUS} VUs, sale at ${saleStartsAt.toISOString()}`);
  return { eventId: event.id, saleStartsAtMs: saleStartsAt.getTime() };
}

export default async function (data) {
  const vu = exec.vu.idInTest; // 1..VUS
  const share = Math.floor(BUYERS / VUS) + (vu <= BUYERS % VUS ? 1 : 0);
  await Promise.all(Array.from({ length: share }, () => buyer(data)));
}

async function buyer(data) {
  const id = uuid4();
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${jwt(id, 'buyer')}` };
  const t0 = data.saleStartsAtMs;

  // Everyone is on the page; they click within REACTION_S of opening, a few too early.
  await sleep((t0 - 300 + Math.random() * REACTION_S * 1000 - Date.now()) / 1000);

  const want = wantedSeats();
  const anyMode = Math.random() < 0.25;
  let hold = null;
  for (let attempt = 0, early = 0; attempt < MAX_ATTEMPTS && !hold; attempt++) {
    const mapRes = await call(
      'seatmap', 'GET', `${API}/events/${data.eventId}/seats`, null,
      { headers: { 'Accept-Encoding': 'gzip' } }, t0, // as every browser does
    );
    if (mapRes.status !== 200) {
      await sleep(1);
      continue;
    }
    const map = mapRes.json();
    const free = map.seats.filter((s) => s.status === 'available');
    if (free.length === 0) {
      journeys.sold_out.add(1);
      return;
    }
    let body;
    if (anyMode) {
      const cats = map.categories.filter((c) => c.available_seats > 0);
      const cat = cats[Math.floor(Math.random() * cats.length)];
      body = { category_id: cat.id, quantity: Math.min(want, cat.available_seats) };
    } else {
      body = { seat_ids: pickSeats(free, want) };
    }
    const res = await call(
      'hold', 'POST', `${API}/events/${data.eventId}/holds`, JSON.stringify(body),
      { headers: { ...headers, 'Idempotency-Key': `${id}:${attempt}` } }, t0,
    );
    if (res.status === 201) {
      hold = res.json();
    } else if (errorCode(res) === 'SALE_NOT_STARTED' && early++ < 10) {
      attempt--; // clicked too early: not a real attempt
      await sleep(0.2 + Math.random() * 0.3);
    } else {
      await sleep(0.2 + Math.random() * 0.8); // someone was faster: look at the map again
    }
  }
  if (!hold) {
    journeys.gave_up.add(1);
    return;
  }

  await sleep(2 + Math.random() * 6); // filling in payment details

  const roll = Math.random();
  if (roll < 0.85) {
    const confirm = () => call('confirm', 'POST', `${API}/holds/${hold.id}/confirm`, null, { headers }, t0);
    const responses = await Promise.all(Math.random() < 0.3 ? [confirm(), confirm()] : [confirm()]);
    const ok = responses.filter((r) => r.status === 200);
    if (ok.length > 1 && ok[0].body !== ok[1].body) idempotencyViolations.add(1);
    if (ok.length) journeys.bought.add(1);
  } else if (roll < 0.95) {
    await call('release', 'DELETE', `${API}/holds/${hold.id}`, null, { headers }, t0);
    journeys.released.add(1);
  } else {
    journeys.abandoned.add(1); // the hold expires after 10 minutes
  }
}

export function teardown(data) {
  const map = http.get(`${API}/events/${data.eventId}/seats`).json();
  const by = {};
  map.seats.forEach((s) => (by[s.status] = (by[s.status] || 0) + 1));
  console.log(`event ${data.eventId} final seat states: ${JSON.stringify(by)}`);
}

// --- report -------------------------------------------------------------------------------

const val = (data, name, stat) => (data.metrics[name] ? data.metrics[name].values[stat] : 0);
const fmt = (x, digits = 1) => (typeof x === 'number' ? x.toFixed(digits) : String(x));

export function handleSummary(data) {
  const durationS = data.state.testRunDurationMs / 1000;
  const total = val(data, 'http_reqs', 'count');
  const fiveXX = val(data, 'server_error_rate', 'passes');
  const opCount = (op) => Object.keys(outcome).filter((k) => k.startsWith(`${op}:`))
    .reduce((acc, k) => acc + val(data, outcome[k].name, 'count'), 0);

  const lines = ['# Load test report: on-sale rush', ''];
  lines.push(`- Buyers: **${BUYERS}** concurrent (on ${VUS} k6 VUs), seats: **${SEATS}**, reaction window ${REACTION_S} s`);
  lines.push(`- Duration: ${fmt(durationS)} s, HTTP requests: ${total}`);
  lines.push(`- RPS, whole run: **${fmt(val(data, 'http_reqs', 'rate'))}**; first 10 s of the sale: **${fmt(val(data, 'rush_requests', 'count') / 10)}**`);
  lines.push(`- 5xx + network errors: ${fiveXX} = **${fmt(100 * val(data, 'server_error_rate', 'rate'), 3)}%** (target: <= 0.1%)`);
  lines.push(`- Different answers to a double confirm: **${val(data, 'idempotency_violations', 'count')}**`, '');

  lines.push('## Latency, ms', '', '| endpoint | requests | p50 | p95 | p99 | max |', '|---|---:|---:|---:|---:|---:|');
  for (const op of OPS) {
    const m = data.metrics[`${op}_ms`];
    if (m) {
      const s = m.values;
      lines.push(`| ${op} | ${opCount(op)} | ${fmt(s.med)} | ${fmt(s['p(95)'])} | ${fmt(s['p(99)'])} | ${fmt(s.max)} |`);
    }
  }

  lines.push('', '## Responses by type', '', '| endpoint | outcome | count | share |', '|---|---|---:|---:|');
  for (const op of OPS) {
    const n = opCount(op);
    for (const k of Object.keys(outcome).filter((key) => key.startsWith(`${op}:`))) {
      const c = val(data, outcome[k].name, 'count');
      if (c) lines.push(`| ${op} | ${k.split(':')[1]} | ${c} | ${fmt((100 * c) / n, 2)}% |`);
    }
  }

  lines.push('', '## Buyer journeys', '', '| journey | buyers |', '|---|---:|');
  for (const j of JOURNEYS) lines.push(`| ${j} | ${val(data, `journey_${j}`, 'count')} |`);

  lines.push('', '## Thresholds', '');
  for (const [name, m] of Object.entries(data.metrics)) {
    for (const [expr, res] of Object.entries(m.thresholds || {})) {
      lines.push(`- ${res.ok ? 'PASS' : 'FAIL'} \`${name}: ${expr}\``);
    }
  }
  const report = `${lines.join('\n')}\n`;
  return { stdout: report, 'results/report.md': report, 'results/summary.json': JSON.stringify(data, null, 2) };
}
