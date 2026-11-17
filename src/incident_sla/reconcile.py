"""Offline cross-system incident telemetry reconciliation (schema v4).

Several collection systems report incident lifecycle events in their own local
clocks.  Each system has one globally shared integer offset ``o`` such that
``utc = local + o``.  Sync observations bound differences between offsets and
carry evidence across systems.  Every event may belong to any incident in its
candidate list; a global explanation assigns every event to exactly one
candidate incident, gives every incident exactly one opened, one
first-response and one resolved event, satisfies the integer clock evidence and
the lifecycle order ``opened <= first_response <= resolved`` in UTC.

The implementation reasons over *all* global explanations at once:

* Clock feasibility for one assignment is a system of integer difference
  constraints ``x_to - x_from <= w`` (offset boxes, sync deltas, event UTC
  window, lifecycle order).  Exact extremes come from shortest paths on that
  constraint graph (Floyd-Warshall, integer weights only) -- offsets are never
  enumerated second by second, no floating-point tolerance is used, and
  correlated offsets are never replaced by a per-source Cartesian product.
* Each candidate assignment is solved jointly with the clock evidence.  A
  lifecycle order that tightens inter-system offset differences is therefore
  present in the graph when the next event assignment is judged, and a sync
  conflict removes every assignment that depends on the excluded clock
  combination.  There is no "align clocks first, match events afterwards"
  decomposition.

Reported intervals are the exact extremes over the union of feasible global
explanations.  Different reported extremes may be realised by different
explanations; the report never claims one explanation attains them together,
and elapsed durations are computed inside a single explanation before taking
extremes (never by subtracting two independently widened time ranges).
"""
from itertools import permutations, product

from .common import ident, require, rows
from .model import validate_lifecycle

SCHEMA_VERSION = 4

TIME_MAX = 4102444800
OFFSET_BOUND = 10 ** 9
DELTA_BOUND = 2 * 10 ** 9

MAX_SOURCES = 8
MAX_SYNCS = 32
MAX_INCIDENTS = 4
PHASES = ('opened', 'first_response', 'resolved')


def _int(value, name):
    # bool is a subclass of int; the contract forbids booleans as integers.
    if type(value) is not int:
        raise ValueError(name + ' must be an integer')
    return value


def _bounded_int(value, name, minimum, maximum):
    _int(value, name)
    if not minimum <= value <= maximum:
        raise ValueError(name + ' outside allowed bounds')
    return value


def _unique_ids(values, name):
    seen = set()
    result = []
    for value in values:
        item = ident(value)
        if item in seen:
            raise ValueError('duplicate ' + name + ': ' + item)
        seen.add(item)
        result.append(item)
    return result


def validate(data):
    """Validate the whole reconciliation document (structure only).

    Structural flaws reject the input (exit 2).  A well-formed document that
    admits no global explanation is a normal ``infeasible`` result instead.
    """
    require(data, 'sources syncs incidents events')

    source_rows = rows(data['sources'], MAX_SOURCES)
    if not source_rows:
        raise ValueError('at least one source is required')
    sources = []
    source_ids = set()
    for row in source_rows:
        require(row, 'id min_offset max_offset')
        sid = ident(row['id'])
        if sid in source_ids:
            raise ValueError('duplicate source id: ' + sid)
        minimum = _bounded_int(row['min_offset'], 'min_offset', -OFFSET_BOUND, OFFSET_BOUND)
        maximum = _bounded_int(row['max_offset'], 'max_offset', -OFFSET_BOUND, OFFSET_BOUND)
        if minimum > maximum:
            raise ValueError('min_offset must not exceed max_offset')
        source_ids.add(sid)
        sources.append({'id': sid, 'min_offset': minimum, 'max_offset': maximum})

    sync_rows = rows(data['syncs'], MAX_SYNCS)
    syncs = []
    sync_ids = set()
    for row in sync_rows:
        require(row, 'id left right min_delta max_delta')
        sid = ident(row['id'])
        if sid in sync_ids:
            raise ValueError('duplicate sync id: ' + sid)
        left = ident(row['left'])
        right = ident(row['right'])
        if left not in source_ids or right not in source_ids:
            raise ValueError('sync references unknown source')
        minimum = _bounded_int(row['min_delta'], 'min_delta', -DELTA_BOUND, DELTA_BOUND)
        maximum = _bounded_int(row['max_delta'], 'max_delta', -DELTA_BOUND, DELTA_BOUND)
        if minimum > maximum:
            raise ValueError('min_delta must not exceed max_delta')
        sync_ids.add(sid)
        syncs.append({'id': sid, 'left': left, 'right': right,
                      'min_delta': minimum, 'max_delta': maximum})

    incident_raw = data['incidents']
    if not isinstance(incident_raw, list) or not 1 <= len(incident_raw) <= MAX_INCIDENTS:
        raise ValueError('incidents must contain 1 to 4 unique incident ids')
    incidents = _unique_ids(incident_raw, 'incident id')
    incident_ids = set(incidents)

    event_rows = rows(data['events'], 3 * MAX_INCIDENTS)
    if len(event_rows) != 3 * len(incidents):
        raise ValueError('events must contain exactly three events per incident')
    events = []
    event_ids = set()
    phase_counts = {phase: 0 for phase in PHASES}
    for row in event_rows:
        require(row, 'id source local phase candidates')
        eid = ident(row['id'])
        if eid in event_ids:
            raise ValueError('duplicate event id: ' + eid)
        source = ident(row['source'])
        if source not in source_ids:
            raise ValueError('event references unknown source: ' + source)
        local = _bounded_int(row['local'], 'local', 0, TIME_MAX)
        phase = row['phase']
        if not isinstance(phase, str) or phase not in PHASES:
            raise ValueError('event phase must be opened, first_response or resolved')
        candidate_raw = row['candidates']
        if not isinstance(candidate_raw, list) or not candidate_raw:
            raise ValueError('candidates must be a non-empty array of incident ids')
        candidates = _unique_ids(candidate_raw, 'candidate id')
        for candidate in candidates:
            if candidate not in incident_ids:
                raise ValueError('candidate references unknown incident: ' + candidate)
        phase_counts[phase] += 1
        event_ids.add(eid)
        events.append({'id': eid, 'source': source, 'local': local,
                       'phase': phase, 'candidates': candidates})
    for phase in PHASES:
        if phase_counts[phase] != len(incidents):
            raise ValueError('each phase must have exactly one event per incident')

    return {'sources': sources, 'syncs': syncs,
            'incidents': incidents, 'events': events}


def _adjacency(node_count, edges):
    inf = None
    dist = [[inf] * node_count for _ in range(node_count)]
    for node in range(node_count):
        dist[node][node] = 0
    for u, v, weight in edges:
        if dist[u][v] is None or weight < dist[u][v]:
            dist[u][v] = weight
    return dist


def _floyd(dist):
    """All-pairs shortest paths; returns None on a negative cycle."""
    size = len(dist)
    for k in range(size):
        dk = dist[k]
        for i in range(size):
            dik = dist[i][k]
            if dik is None:
                continue
            row = dist[i]
            for j in range(size):
                dkj = dk[j]
                if dkj is None:
                    continue
                candidate = dik + dkj
                if row[j] is None or candidate < row[j]:
                    row[j] = candidate
    for i in range(size):
        if dist[i][i] is not None and dist[i][i] < 0:
            return None
    return dist


def reconcile(data):
    """Return the schema-v4 result object for a validated document."""
    sources = data['sources']
    syncs = data['syncs']
    incidents = data['incidents']
    events = data['events']

    index = {source['id']: position for position, source in enumerate(sources)}
    node_count = len(sources) + 1
    ref = node_count - 1

    # Clock evidence shared by every assignment: offset boxes, sync deltas and
    # the event UTC window [0, 4102444800].  Edges encode x_v - x_u <= w.
    base_edges = []
    for position, source in enumerate(sources):
        base_edges.append((ref, position, source['max_offset']))
        base_edges.append((position, ref, -source['min_offset']))
    for sync in syncs:
        left = index[sync['left']]
        right = index[sync['right']]
        base_edges.append((left, right, sync['max_delta']))
        base_edges.append((right, left, -sync['min_delta']))
    for event in events:
        node = index[event['source']]
        base_edges.append((ref, node, TIME_MAX - event['local']))
        base_edges.append((node, ref, event['local']))

    # Per-phase bijections between that phase's events and the incidents,
    # filtered by candidate membership.
    phase_events = {phase: sorted((e for e in events if e['phase'] == phase),
                                  key=lambda e: e['id']) for phase in PHASES}
    ordered_incidents = sorted(incidents)
    phase_options = {}
    for phase in PHASES:
        options = []
        for assignment in permutations(ordered_incidents):
            mapping = {}
            allowed = True
            for event, incident in zip(phase_events[phase], assignment):
                if incident not in event['candidates']:
                    allowed = False
                    break
                mapping[event['id']] = incident
            if allowed:
                options.append(mapping)
        phase_options[phase] = options

    event_by_id = {event['id']: event for event in events}

    offset_min = {source['id']: None for source in sources}
    offset_max = {source['id']: None for source in sources}
    possible = {event['id']: set() for event in events}
    time_bounds = {(iid, phase): [None, None] for iid in incidents for phase in PHASES}
    elapsed_bounds = {iid: {'response_elapsed': [None, None],
                            'resolution_elapsed': [None, None]} for iid in incidents}
    feasible = False

    for opened_map, response_map, resolved_map in product(
            phase_options['opened'], phase_options['first_response'],
            phase_options['resolved']):
        assignment = {}
        assignment.update(opened_map)
        assignment.update(response_map)
        assignment.update(resolved_map)

        adjacency = _adjacency(node_count, base_edges)
        for incident in ordered_incidents:
            opened = event_by_id[_event_for(opened_map, incident)]
            response = event_by_id[_event_for(response_map, incident)]
            resolved = event_by_id[_event_for(resolved_map, incident)]
            # opened <= first_response <= resolved in exact UTC integers.
            _add_order(adjacency, index, opened, response)
            _add_order(adjacency, index, response, resolved)

        dist = _floyd(adjacency)
        if dist is None:
            continue
        # Exact integer difference-constraint semantics: the graph is
        # feasible and the shortest-path closure is tight.
        feasible = True

        # Concrete witness explanation: x_i = dist(ref, i) satisfies every
        # difference constraint and is an actual feasible global explanation.
        # Every complete concrete lifecycle it induces is checked with the
        # same capability v1 uses, so alignment never accepts a lifecycle v1
        # would reject (and keeps v1's missing-state semantics available).
        for incident in ordered_incidents:
            opened = event_by_id[_event_for(opened_map, incident)]
            response = event_by_id[_event_for(response_map, incident)]
            resolved = event_by_id[_event_for(resolved_map, incident)]
            witness = []
            for event in (opened, response, resolved):
                node = index[event['source']]
                witness.append(event['local'] + dist[ref][node])
            validate_lifecycle(*witness)

        for event in events:
            possible[event['id']].add(assignment[event['id']])

        for position, source in enumerate(sources):
            low = -dist[position][ref]
            high = dist[ref][position]
            if offset_min[source['id']] is None or low < offset_min[source['id']]:
                offset_min[source['id']] = low
            if offset_max[source['id']] is None or high > offset_max[source['id']]:
                offset_max[source['id']] = high

        for incident in ordered_incidents:
            opened = event_by_id[_event_for(opened_map, incident)]
            response = event_by_id[_event_for(response_map, incident)]
            resolved = event_by_id[_event_for(resolved_map, incident)]
            for event in (opened, response, resolved):
                node = index[event['source']]
                low = event['local'] - dist[node][ref]
                high = event['local'] + dist[ref][node]
                bounds = time_bounds[(incident, event['phase'])]
                if bounds[0] is None or low < bounds[0]:
                    bounds[0] = low
                if bounds[1] is None or high > bounds[1]:
                    bounds[1] = high

            onode = index[opened['source']]
            rnode = index[response['source']]
            snode = index[resolved['source']]
            # Durations are differences inside one explanation, then widened.
            response_lo = (response['local'] - opened['local']) - dist[rnode][onode]
            response_hi = (response['local'] - opened['local']) + dist[onode][rnode]
            resolution_lo = (resolved['local'] - opened['local']) - dist[snode][onode]
            resolution_hi = (resolved['local'] - opened['local']) + dist[onode][snode]
            _widen(elapsed_bounds[incident]['response_elapsed'], response_lo, response_hi)
            _widen(elapsed_bounds[incident]['resolution_elapsed'], resolution_lo, resolution_hi)

    if not feasible:
        return {'status': 'infeasible', 'sources': [], 'events': [], 'incidents': []}

    source_report = []
    for source in sorted(sources, key=lambda s: s['id']):
        source_report.append({'id': source['id'],
                              'offset_min': offset_min[source['id']],
                              'offset_max': offset_max[source['id']]})

    event_report = []
    for event in sorted(events, key=lambda e: e['id']):
        supported = sorted(possible[event['id']])
        forced = supported[0] if len(supported) == 1 else None
        event_report.append({'id': event['id'],
                             'possible_incidents': supported,
                             'forced_incident': forced})

    incident_report = []
    for incident in sorted(incidents):
        opened = time_bounds[(incident, 'opened')]
        response = time_bounds[(incident, 'first_response')]
        resolved = time_bounds[(incident, 'resolved')]
        elapsed = elapsed_bounds[incident]
        incident_report.append({
            'id': incident,
            'opened': {'min': opened[0], 'max': opened[1]},
            'first_response': {'min': response[0], 'max': response[1]},
            'resolved': {'min': resolved[0], 'max': resolved[1]},
            'response_elapsed': {'min': elapsed['response_elapsed'][0],
                                 'max': elapsed['response_elapsed'][1]},
            'resolution_elapsed': {'min': elapsed['resolution_elapsed'][0],
                                   'max': elapsed['resolution_elapsed'][1]},
        })

    return {'status': 'feasible', 'sources': source_report,
            'events': event_report, 'incidents': incident_report}


def _widen(bounds, low, high):
    """Extend a ``[min, max]`` pair with one explanation's exact interval."""
    if bounds[0] is None or low < bounds[0]:
        bounds[0] = low
    if bounds[1] is None or high > bounds[1]:
        bounds[1] = high


def _event_for(mapping, incident):
    for event_id, assigned in mapping.items():
        if assigned == incident:
            return event_id
    raise AssertionError('phase mapping is not a bijection')


def _add_order(adjacency, index, earlier, later):
    """Add ``utc(earlier) <= utc(later)`` as ``x_later - x_earlier <= w``.

    ``utc = offset + local``, so ``utc(earlier) <= utc(later)`` is
    ``x_earlier - x_later <= local(later) - local(earlier)``: an edge from the
    later source node to the earlier source node carrying that weight.
    """
    earlier_node = index[earlier['source']]
    later_node = index[later['source']]
    weight = later['local'] - earlier['local']
    if adjacency[later_node][earlier_node] is None or weight < adjacency[later_node][earlier_node]:
        adjacency[later_node][earlier_node] = weight


def run(data):
    """Validate then reconcile; structural errors raise, infeasibility reports."""
    return reconcile(validate(data))
