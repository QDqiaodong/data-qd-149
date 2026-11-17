"""Offline redundant-service outage credit settlement ledger (schema v3).

This application settles service-credit liabilities from component downtime
facts and a static service dependency graph.  It deliberately knows nothing
about incident responders, shift schedules or candidate SLA clocks: the only
inputs are component outage intervals, the threshold DAG that maps component
health onto services, and per-contract billing cycles with rate ladders.

Responsibilities implemented here:

* Fault impact: every batch fully replaces the downtime facts of the listed
  components.  Exact service-unavailable intervals are derived by sweeping
  the normalized half-open intervals along the k-of-n threshold DAG, so a
  component shared by several sub-services is correlated by construction and
  never counted twice; nothing is expanded per second.
* Settlement ledger: cycles start unclosed.  ``close`` in a batch posts one
  immutable ``close`` entry at the liability computed after that batch's
  corrections.  Already-closed cycles only receive ``adjustment`` entries
  equal to ``current_L - sum(existing entries)``, so cumulative postings
  always converge to the current liability of the cycle (negative amounts
  claw money back).  Entries are append-only and keyed by
  ``(seq, contract, cycle)``.
* Audit recovery: facts, cycle states, entries and the batch's audit view
  commit in one SQLite transaction.  The rendered view is stored next to the
  batch id and its source SHA-256, so a crash between commit and report
  replacement is healed by retrying the same batch bytes, which returns the
  original report without posting twice.  Historical views are read back
  verbatim and never reinterpreted with newer facts.

Only the Python standard library is used; the ledger assumes a single writer
and does not promise multi-writer coordination or recovery from a corrupted
database file.
"""
import json
import sqlite3
from pathlib import Path

from .common import check_paths as _check_paths
from .common import ident, read, require, rows, save
from .sla import merge

SCHEMA_VERSION = 3
TIME_MAX = 4102444800
FEE_MAX = 10 ** 12
RATE_MAX = 10000
MAX_COMPONENTS = 64
MAX_SERVICES = 64
MAX_CONTRACTS = 100
MAX_CYCLES_PER_CONTRACT = 24
MAX_TIERS_PER_CONTRACT = 1000
MAX_REPLACEMENTS = 64
MAX_INTERVALS_PER_COMPONENT = 20000
MAX_INTERVALS_TOTAL = 20000
MAX_CLOSE_REFS = MAX_CONTRACTS * MAX_CYCLES_PER_CONTRACT

SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE components (
  id TEXT PRIMARY KEY
);
CREATE TABLE services (
  id TEXT PRIMARY KEY,
  required INTEGER NOT NULL
);
CREATE TABLE service_children (
  service TEXT NOT NULL,
  child TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (service, child)
);
CREATE TABLE contracts (
  id TEXT PRIMARY KEY,
  service TEXT NOT NULL
);
CREATE TABLE cycles (
  contract TEXT NOT NULL,
  id TEXT NOT NULL,
  start_sec INTEGER NOT NULL,
  end_sec INTEGER NOT NULL,
  fee_cents INTEGER NOT NULL,
  PRIMARY KEY (contract, id)
);
CREATE TABLE tiers (
  contract TEXT NOT NULL,
  after_seconds INTEGER NOT NULL,
  rate_bps INTEGER NOT NULL,
  PRIMARY KEY (contract, after_seconds)
);
CREATE TABLE facts (
  component TEXT NOT NULL,
  start_sec INTEGER NOT NULL,
  end_sec INTEGER NOT NULL,
  PRIMARY KEY (component, start_sec, end_sec)
);
CREATE TABLE batches (
  seq INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL UNIQUE,
  source_sha256 TEXT NOT NULL,
  report_json TEXT NOT NULL
);
CREATE TABLE cycle_state (
  contract TEXT NOT NULL,
  cycle TEXT NOT NULL,
  closed INTEGER NOT NULL,
  close_seq INTEGER,
  PRIMARY KEY (contract, cycle)
);
CREATE TABLE entries (
  seq INTEGER NOT NULL,
  contract TEXT NOT NULL,
  cycle TEXT NOT NULL,
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  PRIMARY KEY (seq, contract, cycle)
);
CREATE TABLE audit_cycles (
  seq INTEGER NOT NULL,
  contract TEXT NOT NULL,
  cycle TEXT NOT NULL,
  closed INTEGER NOT NULL,
  outage_intervals TEXT NOT NULL,
  downtime_seconds INTEGER NOT NULL,
  liability_cents INTEGER NOT NULL,
  posted_cents INTEGER NOT NULL,
  PRIMARY KEY (seq, contract, cycle)
);
"""


# ---------------------------------------------------------------------------
# validation helpers


def _int(value, name, minimum, maximum):
    # bool is a subclass of int; the contract forbids booleans as integers.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer in %d..%d' % (name, minimum, maximum))
    return value


def _seconds(value, name):
    return _int(value, name, 0, TIME_MAX)


def validate_config(data):
    """Validate a CONFIG document; any structural flaw rejects all of it."""
    require(data, 'components services contracts')

    component_ids = []
    seen = set()
    for raw in rows(data['components'], MAX_COMPONENTS):
        cid = ident(raw)
        if cid in seen:
            raise ValueError('duplicate component id: ' + cid)
        seen.add(cid)
        component_ids.append(cid)

    service_rows = rows(data['services'], MAX_SERVICES)
    service_ids = set()
    for row in service_rows:
        require(row, 'id children required')
        sid = ident(row['id'])
        if sid in seen:
            raise ValueError('id reused across components/services: ' + sid)
        seen.add(sid)
        service_ids.add(sid)

    services = {}
    known = set(component_ids) | service_ids
    for row in service_rows:
        sid = row['id']
        children_raw = row['children']
        if not isinstance(children_raw, list) or not children_raw:
            raise ValueError('children must be a non-empty array')
        children = []
        for child in children_raw:
            cid = ident(child)
            if cid in children:
                raise ValueError('duplicate child in service ' + sid)
            if cid not in known:
                raise ValueError('unknown child reference: ' + cid)
            children.append(cid)
        required = _int(row['required'], 'required', 1, len(children))
        services[sid] = {'children': children, 'required': required}

    _reject_service_cycles(services)

    contracts = []
    contract_ids = set()
    for row in rows(data['contracts'], MAX_CONTRACTS):
        require(row, 'id service cycles tiers')
        cid = ident(row['id'])
        if cid in contract_ids:
            raise ValueError('duplicate contract id: ' + cid)
        contract_ids.add(cid)
        service = ident(row['service'])
        if service not in service_ids:
            raise ValueError('contract references unknown service: ' + service)
        cycles = []
        cycle_ids = set()
        for raw_cycle in rows(row['cycles'], MAX_CYCLES_PER_CONTRACT):
            require(raw_cycle, 'id start end fee_cents')
            yid = ident(raw_cycle['id'])
            if yid in cycle_ids:
                raise ValueError('duplicate cycle id in contract ' + cid)
            cycle_ids.add(yid)
            start = _seconds(raw_cycle['start'], 'cycle.start')
            end = _seconds(raw_cycle['end'], 'cycle.end')
            if end <= start:
                raise ValueError('cycle end must exceed start')
            fee = _int(raw_cycle['fee_cents'], 'fee_cents', 0, FEE_MAX)
            cycles.append({'id': yid, 'start': start, 'end': end, 'fee_cents': fee})
        ordered = sorted(cycles, key=lambda c: (c['start'], c['end']))
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt['start'] < prev['end']:
                raise ValueError('cycles overlap in contract ' + cid)
        tiers_raw = row['tiers']
        if not isinstance(tiers_raw, list) or not tiers_raw:
            raise ValueError('tiers must be a non-empty array')
        tiers = []
        for raw_tier in rows(tiers_raw, MAX_TIERS_PER_CONTRACT):
            require(raw_tier, 'after_seconds rate_bps')
            tiers.append({
                'after_seconds': _seconds(raw_tier['after_seconds'], 'after_seconds'),
                'rate_bps': _int(raw_tier['rate_bps'], 'rate_bps', 0, RATE_MAX),
            })
        head = tiers[0]
        if head['after_seconds'] != 0 or head['rate_bps'] != 0:
            raise ValueError('tiers must start at {after_seconds:0, rate_bps:0}')
        for prev, nxt in zip(tiers, tiers[1:]):
            if nxt['after_seconds'] <= prev['after_seconds']:
                raise ValueError('after_seconds must strictly increase')
            if nxt['rate_bps'] < prev['rate_bps']:
                raise ValueError('rate_bps must be non-decreasing')
        contracts.append({'id': cid, 'service': service,
                          'cycles': cycles, 'tiers': tiers})

    return {'components': component_ids, 'services': services,
            'contracts': contracts}


def _reject_service_cycles(services):
    """Depth-first three-colour cycle check over service->service edges."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in services}
    for root in services:
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        stack = [(root, iter(services[root]['children']))]
        while stack:
            node, pending = stack[-1]
            descended = False
            for child in pending:
                if child not in services:
                    continue
                if color[child] == GRAY:
                    raise ValueError('service dependency cycle at ' + child)
                if color[child] == WHITE:
                    color[child] = GRAY
                    stack.append((child, iter(services[child]['children'])))
                    descended = True
                    break
            if not descended:
                color[node] = BLACK
                stack.pop()


def validate_batch(data, spec):
    """Validate a BATCH document against the immutable config spec."""
    require(data, 'batch_id replacements close')
    batch_id = ident(data['batch_id'])

    replacements = []
    seen_components = set()
    total_intervals = 0
    for row in rows(data['replacements'], MAX_REPLACEMENTS):
        require(row, 'component intervals')
        component = ident(row['component'])
        if component not in spec['component_set']:
            raise ValueError('unknown component in batch: ' + component)
        if component in seen_components:
            raise ValueError('duplicate component in batch: ' + component)
        seen_components.add(component)
        raw_intervals = rows(row['intervals'], MAX_INTERVALS_PER_COMPONENT)
        total_intervals += len(raw_intervals)
        if total_intervals > MAX_INTERVALS_TOTAL:
            raise ValueError('total intervals exceed %d' % MAX_INTERVALS_TOTAL)
        pairs = []
        for pair in raw_intervals:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError('interval must be a [start, end] pair')
            start = _seconds(pair[0], 'interval start')
            end = _seconds(pair[1], 'interval end')
            if end <= start:
                raise ValueError('interval end must exceed start')
            pairs.append((start, end))
        replacements.append((component, merge(pairs)))

    closes = []
    seen_close = set()
    for row in rows(data['close'], MAX_CLOSE_REFS):
        require(row, 'contract cycle')
        contract = ident(row['contract'])
        cycle = ident(row['cycle'])
        if (contract, cycle) in seen_close:
            raise ValueError('duplicate close reference: %s/%s' % (contract, cycle))
        seen_close.add((contract, cycle))
        known = spec['cycles_by_contract'].get(contract)
        if known is None or cycle not in known:
            raise ValueError('close references unknown cycle: %s/%s' % (contract, cycle))
        closes.append((contract, cycle))

    return {'batch_id': batch_id, 'replacements': replacements, 'close': closes}


# ---------------------------------------------------------------------------
# exact interval propagation over the threshold DAG


def combine_children(child_interval_sets, child_count, required):
    """Canonical intervals where fewer than ``required`` children are up.

    A service is unavailable exactly while at least
    ``child_count - required + 1`` of its children are simultaneously
    unavailable.  The sweep consumes canonical half-open interval sets and
    emits the canonical union of the segments whose unavailable-child level
    meets that threshold; endpoint ordering at one instant is resolved by
    half-open semantics, never by per-second expansion.
    """
    threshold = child_count - required + 1
    events = {}
    for intervals in child_interval_sets:
        for start, end in intervals:
            events[start] = events.get(start, 0) + 1
            events[end] = events.get(end, 0) - 1
    down = []
    level = 0
    previous = None
    for moment in sorted(events):
        if previous is not None and previous < moment and level >= threshold:
            down.append((previous, moment))
        level += events[moment]
        previous = moment
    return merge(down)


def evaluate_services(spec, facts):
    """Map every service id to its canonical unavailable intervals.

    ``facts`` maps component ids to canonical downtime intervals.  Services
    are evaluated children-first exactly once (shared subgraphs are computed
    one time and reused as interval sets, so correlated children stay
    correlated); the config was proven acyclic at init.
    """
    services = spec['services']
    indegree = {sid: 0 for sid in services}
    dependents = {sid: [] for sid in services}
    for sid, service in services.items():
        for child in service['children']:
            if child in services:
                indegree[sid] += 1
                dependents[child].append(sid)
    ready = sorted(sid for sid in services if indegree[sid] == 0)
    memo = {}
    while ready:
        sid = ready.pop()
        service = services[sid]
        child_sets = [memo[child] if child in services
                      else facts.get(child, [])
                      for child in service['children']]
        memo[sid] = combine_children(child_sets, len(service['children']),
                                     service['required'])
        for parent in dependents[sid]:
            indegree[parent] -= 1
            if indegree[parent] == 0:
                ready.append(parent)
    if len(memo) != len(services):
        raise ValueError('service dependency cycle survived init')
    return memo


def clip_intervals(intervals, low, high):
    """Canonical intervals of ``intervals`` restricted to [low, high)."""
    return [(max(a, low), min(b, high)) for a, b in intervals
            if max(a, low) < min(b, high)]


def liability_cents(cycle, tiers, downtime_seconds):
    """Current liability L: one floor at the cycle total, never per event."""
    rate = 0
    for tier in tiers:
        if tier['after_seconds'] <= downtime_seconds:
            rate = tier['rate_bps']
        else:
            break
    return cycle['fee_cents'] * rate // 10000


# ---------------------------------------------------------------------------
# SQLite ledger


def _connect(db_path):
    try:
        return sqlite3.connect(str(db_path), isolation_level=None)
    except sqlite3.Error as error:
        raise ValueError('cannot open database: %s' % error)


def _rollback_quietly(conn):
    try:
        conn.execute('ROLLBACK')
    except sqlite3.Error:
        pass


def _remove_partial_db(db_path):
    """Best-effort cleanup so a failed init never bricks a later retry."""
    for suffix in ('', '-journal', '-wal'):
        try:
            Path(str(db_path) + suffix).unlink()
        except OSError:
            pass


def _load_spec(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None or row[0] != str(SCHEMA_VERSION):
        raise ValueError('not a schema v3 credit ledger database')
    components = [r[0] for r in conn.execute('SELECT id FROM components ORDER BY id')]
    services = {}
    for sid, required in conn.execute('SELECT id, required FROM services'):
        children = [r[0] for r in conn.execute(
            'SELECT child FROM service_children WHERE service=? ORDER BY position',
            (sid,))]
        services[sid] = {'children': children, 'required': required}
    contracts = []
    for cid, service in conn.execute('SELECT id, service FROM contracts ORDER BY id'):
        cycles = [{'id': r[0], 'start': r[1], 'end': r[2], 'fee_cents': r[3]}
                  for r in conn.execute(
                      'SELECT id, start_sec, end_sec, fee_cents FROM cycles '
                      'WHERE contract=? ORDER BY id', (cid,))]
        tiers = [{'after_seconds': r[0], 'rate_bps': r[1]}
                 for r in conn.execute(
                     'SELECT after_seconds, rate_bps FROM tiers '
                     'WHERE contract=? ORDER BY after_seconds', (cid,))]
        contracts.append({'id': cid, 'service': service,
                          'cycles': cycles, 'tiers': tiers})
    return {
        'components': components,
        'component_set': set(components),
        'services': services,
        'contracts': contracts,
        'cycles_by_contract': {c['id']: {y['id'] for y in c['cycles']}
                               for c in contracts},
    }


def init_db(config_path, db_path):
    """Create a new ledger database; the config is immutable afterwards."""
    _check_paths([config_path], db_path, None)
    db_path = Path(db_path)
    if db_path.exists():
        raise ValueError('db already exists: init only creates a new database')
    data, digest = read(config_path)
    spec = validate_config(data)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        try:
            conn.executescript(SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            canonical = json.dumps(data, sort_keys=True, ensure_ascii=False,
                                   allow_nan=False)
            conn.execute('INSERT INTO meta VALUES (?,?)',
                         ('schema_version', str(SCHEMA_VERSION)))
            conn.execute('INSERT INTO meta VALUES (?,?)', ('config_sha256', digest))
            conn.execute('INSERT INTO meta VALUES (?,?)', ('config_json', canonical))
            conn.executemany('INSERT INTO components VALUES (?)',
                             [(c,) for c in spec['components']])
            conn.executemany(
                'INSERT INTO services VALUES (?,?)',
                [(sid, s['required']) for sid, s in spec['services'].items()])
            conn.executemany(
                'INSERT INTO service_children VALUES (?,?,?)',
                [(sid, child, position)
                 for sid, s in spec['services'].items()
                 for position, child in enumerate(s['children'])])
            conn.executemany('INSERT INTO contracts VALUES (?,?)',
                             [(c['id'], c['service']) for c in spec['contracts']])
            conn.executemany(
                'INSERT INTO cycles VALUES (?,?,?,?,?)',
                [(c['id'], y['id'], y['start'], y['end'], y['fee_cents'])
                 for c in spec['contracts'] for y in c['cycles']])
            conn.executemany(
                'INSERT INTO tiers VALUES (?,?,?)',
                [(c['id'], t['after_seconds'], t['rate_bps'])
                 for c in spec['contracts'] for t in c['tiers']])
            conn.executemany(
                'INSERT INTO cycle_state VALUES (?,?,0,NULL)',
                [(c['id'], y['id']) for c in spec['contracts'] for y in c['cycles']])
            conn.execute('COMMIT')
        except BaseException:
            _rollback_quietly(conn)
            raise
    except sqlite3.Error as error:
        _remove_partial_db(db_path)
        raise ValueError('database error during init: %s' % error)
    except BaseException:
        _remove_partial_db(db_path)
        raise
    finally:
        conn.close()
    return {
        'schema_version': SCHEMA_VERSION,
        'config_sha256': digest,
        'components': len(spec['components']),
        'services': len(spec['services']),
        'contracts': len(spec['contracts']),
        'cycles': sum(len(c['cycles']) for c in spec['contracts']),
    }


def _load_facts(conn):
    facts = {}
    for component, start, end in conn.execute(
            'SELECT component, start_sec, end_sec FROM facts '
            'ORDER BY component, start_sec'):
        facts.setdefault(component, []).append((start, end))
    return facts


def _commit_batch(conn, spec, batch, digest):
    """Apply one validated batch and return its audit view (pre-commit)."""
    for component, intervals in batch['replacements']:
        conn.execute('DELETE FROM facts WHERE component=?', (component,))
        conn.executemany('INSERT INTO facts VALUES (?,?,?)',
                         [(component, a, b) for a, b in intervals])

    facts = _load_facts(conn)
    service_down = evaluate_services(spec, facts)

    seq = conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM batches').fetchone()[0]

    states = {(contract, cycle): closed for contract, cycle, closed in conn.execute(
        'SELECT contract, cycle, closed FROM cycle_state')}
    close_set = set(batch['close'])
    for contract, cycle in sorted(close_set):
        if states[(contract, cycle)]:
            raise ValueError('cycle already closed: %s/%s' % (contract, cycle))

    posted = {(contract, cycle): total for contract, cycle, total in conn.execute(
        'SELECT contract, cycle, SUM(amount_cents) FROM entries '
        'GROUP BY contract, cycle')}

    new_entries = []
    cycle_rows = []
    audit_rows = []
    for contract in spec['contracts']:
        down = service_down[contract['service']]
        for cycle in contract['cycles']:
            key = (contract['id'], cycle['id'])
            clipped = clip_intervals(down, cycle['start'], cycle['end'])
            downtime = sum(b - a for a, b in clipped)
            liability = liability_cents(cycle, contract['tiers'], downtime)
            if key in close_set:
                amount = liability
                new_entries.append((seq, key[0], key[1], 'close', amount))
                posted[key] = posted.get(key, 0) + amount
                closed_now = True
            elif states[key]:
                adjustment = liability - posted.get(key, 0)
                if adjustment != 0:
                    new_entries.append((seq, key[0], key[1], 'adjustment', adjustment))
                    posted[key] = posted.get(key, 0) + adjustment
                closed_now = True
            else:
                closed_now = False
            posted_now = posted.get(key, 0)
            interval_json = json.dumps([[a, b] for a, b in clipped])
            cycle_rows.append({
                'contract': key[0],
                'cycle': key[1],
                'closed': closed_now,
                'outage_intervals': [[a, b] for a, b in clipped],
                'downtime_seconds': downtime,
                'liability_cents': liability,
                'posted_cents': posted_now,
            })
            audit_rows.append((seq, key[0], key[1], 1 if closed_now else 0,
                               interval_json, downtime, liability, posted_now))

    conn.executemany(
        'UPDATE cycle_state SET closed=1, close_seq=? WHERE contract=? AND cycle=?',
        [(seq, contract, cycle) for contract, cycle in sorted(close_set)])
    conn.executemany('INSERT INTO entries VALUES (?,?,?,?,?)', new_entries)

    entries_view = [
        {'seq': r[0], 'contract': r[1], 'cycle': r[2], 'kind': r[3],
         'amount_cents': r[4]}
        for r in conn.execute(
            'SELECT seq, contract, cycle, kind, amount_cents FROM entries '
            'WHERE seq<=? ORDER BY seq, contract, cycle', (seq,))]

    report = {
        'schema_version': SCHEMA_VERSION,
        'seq': seq,
        'batch_id': batch['batch_id'],
        'source_sha256': digest,
        'cycles': cycle_rows,
        'entries': entries_view,
    }
    conn.execute(
        'INSERT INTO batches VALUES (?,?,?,?)',
        (seq, batch['batch_id'], digest,
         json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False)))
    conn.executemany('INSERT INTO audit_cycles VALUES (?,?,?,?,?,?,?,?)', audit_rows)
    return report


def apply_batch(batch_path, db_path, output_path):
    """Commit one batch (or replay an identical committed one) and publish."""
    _check_paths([batch_path], db_path, output_path)
    db_path = Path(db_path)
    if not db_path.exists():
        raise ValueError('db does not exist: run --credit-init first')
    data, digest = read(batch_path)
    conn = _connect(db_path)
    try:
        try:
            spec = _load_spec(conn)
            batch = validate_batch(data, spec)
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute(
                    'SELECT source_sha256, report_json FROM batches WHERE batch_id=?',
                    (batch['batch_id'],)).fetchone()
                if row is not None:
                    if row[0] != digest:
                        raise ValueError(
                            'batch_id already committed with different bytes: '
                            + batch['batch_id'])
                    report = json.loads(row[1])
                else:
                    report = _commit_batch(conn, spec, batch, digest)
                conn.execute('COMMIT')
            except BaseException:
                _rollback_quietly(conn)
                raise
        except sqlite3.Error as error:
            raise ValueError('database error during apply: %s' % error)
    finally:
        conn.close()
    save(output_path, report)
    return report


def view_report(seq, db_path, output_path):
    """Read back the permanent audit view of one committed batch."""
    _check_paths([], db_path, output_path)
    if type(seq) is not int or not 1 <= seq <= 2 ** 63 - 1:
        raise ValueError('seq must be a positive integer')
    db_path = Path(db_path)
    if not db_path.exists():
        raise ValueError('db does not exist: run --credit-init first')
    conn = _connect(db_path)
    try:
        try:
            _load_spec(conn)
            row = conn.execute(
                'SELECT report_json FROM batches WHERE seq=?', (seq,)).fetchone()
            if row is None:
                raise ValueError('no committed batch with seq %d' % seq)
            report = json.loads(row[0])
        except sqlite3.Error as error:
            raise ValueError('database error during view: %s' % error)
    finally:
        conn.close()
    save(output_path, report)
    return report
