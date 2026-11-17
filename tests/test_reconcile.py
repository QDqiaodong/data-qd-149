"""Schema v4 cross-system telemetry reconciliation tests.

The oracle below is deliberately naive and independent of the production
solver: it enumerates every integer offset in a tiny box and every candidate
assignment.  Production code is required to be exact at 2e9-second offset
spans, so it must never enumerate offsets; the differential tests therefore
only feed the oracle inputs whose offset boxes are a handful of integers wide.
"""
import hashlib
import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from incident_sla.model import analyze as analyze_v1, validate_lifecycle
from incident_sla.reconcile import run, validate

ROOT = Path(__file__).resolve().parents[1]
TIME_MAX = 4102444800
PHASES = ('opened', 'first_response', 'resolved')


# ---------------------------------------------------------------------------
# Independent oracle: brute-force integer offsets + assignments.
# ---------------------------------------------------------------------------
def oracle(data):
    sources = data['sources']
    syncs = data['syncs']
    incidents = sorted(data['incidents'])
    events = data['events']
    event_by_id = {e['id']: e for e in events}
    phase_events = {phase: [e for e in events if e['phase'] == phase]
                    for phase in PHASES}
    source_ids = [s['id'] for s in sources]
    ranges = [range(s['min_offset'], s['max_offset'] + 1) for s in sources]

    phase_options = {}
    for phase in PHASES:
        options = []
        for order in itertools.permutations(incidents):
            if all(incident in event['candidates'] for event, incident
                   in zip(phase_events[phase], order)):
                options.append({event['id']: incident
                                for event, incident in zip(phase_events[phase], order)})
        phase_options[phase] = options

    def utc_for(event, offsets):
        return event['local'] + offsets[event['source']]

    def chosen(assignment, incident, phase):
        return next(event_by_id[k] for k, v in assignment.items()
                    if v == incident and event_by_id[k]['phase'] == phase)

    # Every (integer offset vector, full event assignment) explanation.
    explanations = []
    for combo in itertools.product(*ranges):
        offsets = dict(zip(source_ids, combo))
        if any(not sync['min_delta'] <= offsets[sync['right']] - offsets[sync['left']]
               <= sync['max_delta'] for sync in syncs):
            continue
        if any(not 0 <= utc_for(event, offsets) <= TIME_MAX for event in events):
            continue
        for om, rm, sm in itertools.product(*(phase_options[p] for p in PHASES)):
            assignment = {}
            assignment.update(om)
            assignment.update(rm)
            assignment.update(sm)
            if all(utc_for(chosen(assignment, iid, 'opened'), offsets)
                   <= utc_for(chosen(assignment, iid, 'first_response'), offsets)
                   <= utc_for(chosen(assignment, iid, 'resolved'), offsets)
                   for iid in incidents):
                explanations.append((offsets, assignment))

    if not explanations:
        return {'status': 'infeasible', 'sources': [], 'events': [], 'incidents': []}

    result = {'status': 'feasible', 'sources': [], 'events': [], 'incidents': []}
    for source in sorted(sources, key=lambda s: s['id']):
        values = [offsets[source['id']] for offsets, _ in explanations]
        result['sources'].append({'id': source['id'],
                                  'offset_min': min(values),
                                  'offset_max': max(values)})
    for event in sorted(events, key=lambda e: e['id']):
        supported = sorted({assignment[event['id']] for _, assignment in explanations})
        result['events'].append({'id': event['id'],
                                 'possible_incidents': supported,
                                 'forced_incident': supported[0] if len(supported) == 1 else None})
    for incident in incidents:
        times = {phase: [] for phase in PHASES}
        response_elapsed = []
        resolution_elapsed = []
        for offsets, assignment in explanations:
            utc = {phase: utc_for(chosen(assignment, incident, phase), offsets)
                   for phase in PHASES}
            for phase in PHASES:
                times[phase].append(utc[phase])
            response_elapsed.append(utc['first_response'] - utc['opened'])
            resolution_elapsed.append(utc['resolved'] - utc['opened'])
        result['incidents'].append({
            'id': incident,
            'opened': {'min': min(times['opened']), 'max': max(times['opened'])},
            'first_response': {'min': min(times['first_response']),
                               'max': max(times['first_response'])},
            'resolved': {'min': min(times['resolved']), 'max': max(times['resolved'])},
            'response_elapsed': {'min': min(response_elapsed), 'max': max(response_elapsed)},
            'resolution_elapsed': {'min': min(resolution_elapsed),
                                   'max': max(resolution_elapsed)},
        })
    return result


# ---------------------------------------------------------------------------
# Document builders.
# ---------------------------------------------------------------------------
def make_doc(sources, syncs, incidents, events):
    return {'sources': sources, 'syncs': syncs,
            'incidents': incidents, 'events': events}


def event(eid, source, local, phase, candidates):
    return {'id': eid, 'source': source, 'local': local,
            'phase': phase, 'candidates': list(candidates)}


def simple(incident, source, opened, response, resolved, candidates=None):
    candidates = candidates or [incident]
    return [event('e-open', source, opened, 'opened', candidates),
            event('e-resp', source, response, 'first_response', candidates),
            event('e-done', source, resolved, 'resolved', candidates)]


class Structure(unittest.TestCase):
    def base(self):
        return make_doc([{'id': 'A', 'min_offset': 0, 'max_offset': 0}], [],
                        ['I1'], simple('I1', 'A', 0, 10, 20))

    def bad(self, data):
        with self.assertRaises((ValueError, TypeError)):
            validate(data)

    def test_unknown_and_missing_fields(self):
        d = self.base(); d['extra'] = 1; self.bad(d)
        d = self.base(); del d['syncs']; self.bad(d)
        d = self.base(); d['sources'][0]['extra'] = 0; self.bad(d)
        d = self.base(); d['events'][0]['extra'] = 0; self.bad(d)

    def test_boolean_is_not_integer(self):
        d = self.base(); d['sources'][0]['min_offset'] = True; self.bad(d)
        d = self.base(); d['sources'][0]['max_offset'] = False; self.bad(d)
        d = self.base(); d['events'][0]['local'] = True; self.bad(d)
        d = self.base(); d['syncs'] = [{'id': 's', 'left': 'A', 'right': 'A',
                                        'min_delta': False, 'max_delta': 0}]; self.bad(d)

    def test_reference_and_unique_checks(self):
        d = self.base(); d['events'][0]['source'] = 'unknown'; self.bad(d)
        d = self.base(); d['events'][0]['candidates'] = ['I9']; self.bad(d)
        d = self.base(); d['events'][0]['candidates'] = []; self.bad(d)
        d = self.base(); d['events'][0]['candidates'] = ['I1', 'I1']; self.bad(d)
        d = self.base(); d['events'][1]['id'] = d['events'][0]['id']; self.bad(d)
        d = self.base(); d['sources'].append(dict(d['sources'][0])); self.bad(d)
        d = self.base(); d['incidents'] = ['I1', 'I1']; self.bad(d)
        d = self.base(); d['incidents'] = []; self.bad(d)

    def test_phase_counts(self):
        d = self.base(); d['events'][2]['phase'] = 'opened'; self.bad(d)
        d = self.base(); d['events'].pop(); self.bad(d)
        d = self.base(); d['events'][0]['phase'] = 'closed'; self.bad(d)

    def test_bounds(self):
        d = self.base(); d['sources'][0]['min_offset'] = -10 ** 9 - 1; self.bad(d)
        d = self.base(); d['sources'][0]['max_offset'] = 10 ** 9 + 1; self.bad(d)
        d = self.base(); d['sources'][0]['min_offset'] = 5; d['sources'][0]['max_offset'] = 4; self.bad(d)
        d = self.base(); d['events'][0]['local'] = TIME_MAX + 1; self.bad(d)
        d = self.base(); d['events'][0]['local'] = -1; self.bad(d)
        d = self.base(); d['syncs'] = [{'id': 's', 'left': 'A', 'right': 'A',
                                        'min_delta': -(2 * 10 ** 9) - 1, 'max_delta': 0}]; self.bad(d)

    def test_candidate_types(self):
        d = self.base(); d['events'][0]['candidates'] = [1]; self.bad(d)
        d = self.base(); d['incidents'] = ['I1', 2]; self.bad(d)
        d = self.base(); d['events'][0]['phase'] = 1; self.bad(d)
        d = self.base(); d['events'][0]['candidates'] = 'I1'; self.bad(d)

    def test_array_limits(self):
        d = self.base()
        d['sources'] = [{'id': f'S{i}', 'min_offset': 0, 'max_offset': 0}
                        for i in range(9)]
        self.bad(d)
        d = self.base()
        d['syncs'] = [{'id': f's{i}', 'left': 'A', 'right': 'A',
                       'min_delta': 0, 'max_delta': 0} for i in range(33)]
        self.bad(d)
        d = self.base(); d['incidents'] = [f'I{i}' for i in range(5)]
        d['events'] = [event(f'e{i}', 'A', i, PHASES[i % 3], d['incidents'])
                       for i in range(15)]
        self.bad(d)
        d = self.base(); d['incidents'] = ['I1', 'I2']
        d['events'] = [event('e0', 'A', 0, 'opened', ['I1']),
                       event('e1', 'A', 1, 'opened', ['I2']),
                       event('e2', 'A', 2, 'first_response', ['I1']),
                       event('e3', 'A', 3, 'first_response', ['I2']),
                       event('e4', 'A', 4, 'resolved', ['I1'])]
        self.bad(d)


class ClockEvidence(unittest.TestCase):
    def test_negative_offsets_and_equality_sync(self):
        data = make_doc(
            [{'id': 'A', 'min_offset': -10, 'max_offset': -10},
             {'id': 'B', 'min_offset': 0, 'max_offset': 0}],
            [{'id': 's', 'left': 'A', 'right': 'B', 'min_delta': 10, 'max_delta': 10}],
            ['I1'], simple('I1', 'A', 1010, 1090, 1290))
        result = run(data)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual([(r['offset_min'], r['offset_max']) for r in result['sources']],
                         [(-10, -10), (0, 0)])
        self.assertEqual(result['incidents'][0]['opened'], {'min': 1000, 'max': 1000})

    def test_multi_hop_correlation_narrows(self):
        # oC = oA + 2 must equal 3, so the whole chain pins A to 1 and B to 2.
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 10},
             {'id': 'B', 'min_offset': 0, 'max_offset': 10},
             {'id': 'C', 'min_offset': 3, 'max_offset': 3}],
            [{'id': 's1', 'left': 'A', 'right': 'B', 'min_delta': 1, 'max_delta': 1},
             {'id': 's2', 'left': 'B', 'right': 'C', 'min_delta': 1, 'max_delta': 1}],
            ['I1'], simple('I1', 'A', 0, 1, 2))
        result = run(data)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual([(r['offset_min'], r['offset_max']) for r in result['sources']],
                         [(1, 1), (2, 2), (3, 3)])

    def test_contradictory_ring(self):
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 0},
             {'id': 'B', 'min_offset': 0, 'max_offset': 0},
             {'id': 'C', 'min_offset': 0, 'max_offset': 0}],
            [{'id': 's1', 'left': 'A', 'right': 'B', 'min_delta': 1, 'max_delta': 1},
             {'id': 's2', 'left': 'B', 'right': 'C', 'min_delta': 1, 'max_delta': 1},
             {'id': 's3', 'left': 'C', 'right': 'A', 'min_delta': -1, 'max_delta': -1}],
            ['I1'], simple('I1', 'A', 0, 1, 2))
        result = run(data)
        self.assertEqual(result, {'status': 'infeasible', 'sources': [],
                                  'events': [], 'incidents': []})

    def test_self_reference_contradiction(self):
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 0}],
            [{'id': 's', 'left': 'A', 'right': 'A', 'min_delta': 1, 'max_delta': 1}],
            ['I1'], simple('I1', 'A', 0, 1, 2))
        self.assertEqual(run(data)['status'], 'infeasible')
        # A zero-width self reference is independent evidence and harmless.
        data['syncs'][0].update(min_delta=0, max_delta=0)
        self.assertEqual(run(data)['status'], 'feasible')

    def test_duplicate_pairs_are_independent_evidence(self):
        # Two zero-width constraints on the same pair pin the difference; a
        # wider duplicate does not weaken the tighter one.
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 10},
             {'id': 'B', 'min_offset': 0, 'max_offset': 10}],
            [{'id': 'tight', 'left': 'A', 'right': 'B', 'min_delta': 2, 'max_delta': 2},
             {'id': 'wide', 'left': 'A', 'right': 'B', 'min_delta': 0, 'max_delta': 9}],
            ['I1'], simple('I1', 'A', 0, 1, 2))
        result = run(data)
        sources = {r['id']: r for r in result['sources']}
        # B - A == 2 in every explanation; the shared UTC window lifts both
        # floors and caps A at 8, so B spans [2, 10].  The wider duplicate
        # cannot relax the wide evidence away.
        self.assertEqual((sources['A']['offset_min'], sources['A']['offset_max']), (0, 8))
        self.assertEqual((sources['B']['offset_min'], sources['B']['offset_max']), (2, 10))

    def test_two_billion_second_span(self):
        # oB = 0, so oA must satisfy -2e9 <= -oA <= 0, i.e. oA in [0, 2e9],
        # intersected with the declared box [-1e9, 1e9] -> [0, 1e9].
        data = make_doc(
            [{'id': 'A', 'min_offset': -10 ** 9, 'max_offset': 10 ** 9},
             {'id': 'B', 'min_offset': 0, 'max_offset': 0}],
            [{'id': 's', 'left': 'A', 'right': 'B',
              'min_delta': -2 * 10 ** 9, 'max_delta': 0}],
            ['I1'], simple('I1', 'B', 0, 1, 2))
        result = run(data)
        self.assertEqual(result['status'], 'feasible')
        self.assertEqual([(r['offset_min'], r['offset_max']) for r in result['sources']],
                         [(0, 10 ** 9), (0, 0)])

    def test_utc_boundaries(self):
        data = make_doc(
            [{'id': 'A', 'min_offset': -5, 'max_offset': 5}], [],
            ['I1'], simple('I1', 'A', 0, 0, 0))
        # opened at local 0 forces offset >= 0; everything else stays free.
        self.assertEqual([(r['offset_min'], r['offset_max']) for r in run(data)['sources']],
                         [(0, 5)])
        data = make_doc(
            [{'id': 'A', 'min_offset': -5, 'max_offset': 5}], [],
            ['I1'], simple('I1', 'A', TIME_MAX, TIME_MAX, TIME_MAX))
        self.assertEqual([(r['offset_min'], r['offset_max']) for r in run(data)['sources']],
                         [(-5, 0)])
        # One event at each boundary pins the offset to exactly zero.
        data = make_doc(
            [{'id': 'A', 'min_offset': -5, 'max_offset': 5}], [],
            ['I1'], simple('I1', 'A', 0, 5, TIME_MAX))
        self.assertEqual([(r['offset_min'], r['offset_max']) for r in run(data)['sources']],
                         [(0, 0)])


class Attribution(unittest.TestCase):
    def test_multiple_candidates_forced_vs_possible(self):
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 0}], [],
            ['I1', 'I2'],
            [event('o1', 'A', 0, 'opened', ['I1']),
             event('o2', 'A', 100, 'opened', ['I1', 'I2']),
             event('r1', 'A', 10, 'first_response', ['I1', 'I2']),
             event('r2', 'A', 110, 'first_response', ['I1', 'I2']),
             event('d1', 'A', 20, 'resolved', ['I1', 'I2']),
             event('d2', 'A', 120, 'resolved', ['I1', 'I2'])])
        result = run(data)
        self.assertEqual(result['status'], 'feasible')
        rows = {e['id']: e for e in result['events']}
        self.assertEqual(rows['o1']['forced_incident'], 'I1')
        self.assertEqual(rows['o1']['possible_incidents'], ['I1'])
        # o1 can only be I1, so the phase bijection forces o2 to I2.
        self.assertEqual(rows['o2']['possible_incidents'], ['I2'])
        self.assertEqual(rows['o2']['forced_incident'], 'I2')

    def test_hall_conflict_is_infeasible(self):
        # Both opened events can only be I1; no bijection exists.
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 0}], [],
            ['I1', 'I2'],
            [event('o1', 'A', 0, 'opened', ['I1']),
             event('o2', 'A', 100, 'opened', ['I1']),
             event('r1', 'A', 10, 'first_response', ['I1', 'I2']),
             event('r2', 'A', 110, 'first_response', ['I1', 'I2']),
             event('d1', 'A', 20, 'resolved', ['I1', 'I2']),
             event('d2', 'A', 120, 'resolved', ['I1', 'I2'])])
        self.assertEqual(run(data)['status'], 'infeasible')

    def test_equal_lifecycle_endpoints(self):
        data = make_doc([{'id': 'A', 'min_offset': 0, 'max_offset': 0}], [],
                        ['I1'], simple('I1', 'A', 7, 7, 7))
        result = run(data)
        row = result['incidents'][0]
        self.assertEqual(row['opened'], {'min': 7, 'max': 7})
        self.assertEqual(row['response_elapsed'], {'min': 0, 'max': 0})
        self.assertEqual(row['resolution_elapsed'], {'min': 0, 'max': 0})

    def test_inverted_lifecycle_is_infeasible(self):
        data = make_doc([{'id': 'A', 'min_offset': 0, 'max_offset': 0}], [],
                        ['I1'], simple('I1', 'A', 0, 10, 5))
        self.assertEqual(run(data)['status'], 'infeasible')

    def test_same_phase_exchange_does_not_change_time_ranges(self):
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 0}], [], ['I1', 'I2'],
            [event('o1', 'A', 0, 'opened', ['I1', 'I2']),
             event('o2', 'A', 1, 'opened', ['I1', 'I2']),
             event('r1', 'A', 2, 'first_response', ['I1', 'I2']),
             event('r2', 'A', 3, 'first_response', ['I1', 'I2']),
             event('d1', 'A', 4, 'resolved', ['I1', 'I2']),
             event('d2', 'A', 5, 'resolved', ['I1', 'I2'])])
        result = run(data)
        first, second = result['incidents']
        first.pop('id'); second.pop('id')
        self.assertEqual(first, second)


class LifecycleCapability(unittest.TestCase):
    """The shared validator must keep v1's acceptance/rejection behaviour."""

    def test_four_presence_combinations(self):
        validate_lifecycle(10, None, None)            # open, unresolved
        validate_lifecycle(10, 10, None)              # responded, unresolved
        validate_lifecycle(10, 10, 10)                # responded, resolved
        validate_lifecycle(10, 20, 30)
        with self.assertRaises(ValueError):
            validate_lifecycle(10, 5, None)           # response before opened
        with self.assertRaises(ValueError):
            validate_lifecycle(10, None, 20)          # resolution without response
        with self.assertRaises(ValueError):
            validate_lifecycle(10, 20, 15)            # resolved before response
        with self.assertRaises(ValueError):
            validate_lifecycle(10, 20, 5)             # resolved before opened

    def test_v1_behaviour_preserved(self):
        base = {'targets': {'response_seconds': 300, 'resolution_seconds': 3600},
                'incidents': [{'id': 'I', 'opened': 10, 'first_response': None,
                               'resolved': None}], 'pauses': []}
        analyze_v1(base)
        base['incidents'][0].update(first_response=10)
        analyze_v1(base)
        base['incidents'][0].update(resolved=10)
        analyze_v1(base)
        base['incidents'][0].update(resolved=5)
        with self.assertRaises(ValueError):
            analyze_v1(base)
        base['incidents'][0].update(first_response=None, resolved=10)
        with self.assertRaises(ValueError):
            analyze_v1(base)


class JointLinkage(unittest.TestCase):
    def test_joint_constraints_exclude_matchings_and_tighten_offsets(self):
        # Synchronization evidence alone leaves A in [4,12] and B in [2,9] and
        # admits a candidate-only perfect matching in every phase.  Lifetimes
        # are only satisfiable for some of those matchings, and the surviving
        # ones tighten B to [2,3] and A to [11,12].
        data = make_doc(
            [{'id': 'A', 'min_offset': 4, 'max_offset': 12},
             {'id': 'B', 'min_offset': 2, 'max_offset': 9}],
            [{'id': 's', 'left': 'A', 'right': 'B', 'min_delta': -9, 'max_delta': 12}],
            ['I1', 'I2'],
            [event('e00', 'A', 2, 'opened', ['I1', 'I2']),
             event('e01', 'B', 28, 'opened', ['I1', 'I2']),
             event('e10', 'A', 19, 'first_response', ['I1', 'I2']),
             event('e11', 'B', 13, 'first_response', ['I2']),
             event('e20', 'A', 29, 'resolved', ['I1', 'I2']),
             event('e21', 'B', 21, 'resolved', ['I1', 'I2'])])
        result = run(data)
        self.assertEqual(result['status'], 'feasible')
        sources = {s['id']: s for s in result['sources']}
        self.assertEqual((sources['A']['offset_min'], sources['A']['offset_max']), (11, 12))
        self.assertEqual((sources['B']['offset_min'], sources['B']['offset_max']), (2, 3))
        events = {e['id']: e for e in result['events']}
        # Every event is fully determined by the joint evidence.
        for eid, incident in (('e00', 'I2'), ('e01', 'I1'), ('e10', 'I1'),
                              ('e11', 'I2'), ('e20', 'I1'), ('e21', 'I2')):
            self.assertEqual(events[eid]['forced_incident'], incident)
        self.assertEqual(result, oracle(data))

    def test_wide_ranges_fixed_difference_duration(self):
        # oA and oB have wide, independent-looking ranges, but the sync fixes
        # oB - oA exactly, so every duration is fixed even though timestamps
        # are wide.  Independent range subtraction would wrongly widen them.
        data = make_doc(
            [{'id': 'A', 'min_offset': 0, 'max_offset': 1000},
             {'id': 'B', 'min_offset': 0, 'max_offset': 1000}],
            [{'id': 's', 'left': 'A', 'right': 'B', 'min_delta': 0, 'max_delta': 0}],
            ['I1'],
            [event('o', 'A', 100, 'opened', ['I1']),
             event('r', 'B', 150, 'first_response', ['I1']),
             event('d', 'B', 200, 'resolved', ['I1'])])
        result = run(data)
        row = result['incidents'][0]
        self.assertEqual(row['opened'], {'min': 100, 'max': 1100})
        self.assertEqual(row['first_response'], {'min': 150, 'max': 1150})
        self.assertEqual(row['resolved'], {'min': 200, 'max': 1200})
        self.assertEqual(row['response_elapsed'], {'min': 50, 'max': 50})
        self.assertEqual(row['resolution_elapsed'], {'min': 100, 'max': 100})
        self.assertEqual(result, oracle(data))


# ---------------------------------------------------------------------------
# Differential testing against the brute-force oracle.
# ---------------------------------------------------------------------------
def random_document(rng):
    source_count = rng.randint(1, 3)
    sources = []
    for i in range(source_count):
        low = rng.randint(-3, 1)
        high = rng.randint(low, 3)
        sources.append({'id': f'S{i}', 'min_offset': low, 'max_offset': high})
    syncs = []
    for i in range(rng.randint(0, 3)):
        left = rng.choice(sources)['id']
        right = rng.choice(sources)['id']
        low = rng.randint(-4, 2)
        high = rng.randint(low, 4)
        syncs.append({'id': f'y{i}', 'left': left, 'right': right,
                      'min_delta': low, 'max_delta': high})
    incident_count = rng.randint(1, 2)
    incidents = [f'I{i}' for i in range(incident_count)]
    events = []
    counter = 0
    for phase_index, phase in enumerate(PHASES):
        for _ in range(incident_count):
            candidates = [iid for iid in incidents if rng.random() < 0.8]
            if not candidates:
                candidates = [rng.choice(incidents)]
            base = phase_index * 8
            local = max(0, rng.randint(base - 2, base + 4))
            events.append({'id': f'e{counter}', 'source': rng.choice(sources)['id'],
                           'local': local, 'phase': phase,
                           'candidates': candidates})
            counter += 1
    return make_doc(sources, syncs, incidents, events)


class OracleDifferential(unittest.TestCase):
    def test_random_small_documents(self):
        rng = random.Random(20261117)
        for _ in range(250):
            data = random_document(rng)
            self.assertEqual(run(data), oracle(data), msg=json.dumps(data))

    def test_random_three_source_three_incident(self):
        rng = random.Random(7717)
        feasible = 0
        for _ in range(80):
            source_count = rng.randint(1, 3)
            sources = [{'id': f'S{i}',
                        'min_offset': rng.randint(-2, 0),
                        'max_offset': rng.randint(1, 3)}
                       for i in range(source_count)]
            syncs = [{'id': f'y{j}',
                      'left': rng.choice(sources)['id'],
                      'right': rng.choice(sources)['id'],
                      'min_delta': rng.randint(-3, 0),
                      'max_delta': rng.randint(0, 3)}
                     for j in range(rng.randint(0, 4))]
            incident_count = rng.randint(1, 3)
            incidents = [f'I{i}' for i in range(incident_count)]
            events = []
            counter = 0
            for phase_index, phase in enumerate(PHASES):
                for _ in range(incident_count):
                    candidates = [iid for iid in incidents if rng.random() < 0.8]
                    candidates = candidates or [rng.choice(incidents)]
                    events.append(event(f'e{counter}', rng.choice(sources)['id'],
                                        rng.randint(phase_index * 10, phase_index * 10 + 5),
                                        phase, candidates))
                    counter += 1
            data = make_doc(sources, syncs, incidents, events)
            result = run(data)
            self.assertEqual(result, oracle(data), msg=json.dumps(data))
            feasible += result['status'] == 'feasible'
        self.assertGreater(feasible, 0)


# ---------------------------------------------------------------------------
# CLI behaviour.
# ---------------------------------------------------------------------------
class Command(unittest.TestCase):
    def call(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'incident_sla.cli',
             '--reconcile-source', str(source), '--output', str(output)],
            capture_output=True, text=True,
            env={**__import__('os').environ, 'PYTHONPATH': str(ROOT / 'src')})

    def test_sample_and_byte_stability(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'out.json'
            src = ROOT / 'fixtures/reconcile_input.json'
            first = self.call(src, out)
            self.assertEqual(first.returncode, 0, first.stderr)
            payload = json.loads(out.read_bytes())
            self.assertEqual(payload['schema_version'], 4)
            self.assertEqual(payload['source_sha256'],
                             hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertEqual(payload['result']['status'], 'feasible')
            self.assertEqual(self.call(src, out).returncode, 0)
            first_bytes = out.read_bytes()
            self.assertEqual(self.call(src, out).returncode, 0)
            self.assertEqual(first_bytes, out.read_bytes())

    def test_infeasible_is_success(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            out = Path(td) / 'out.json'
            data = make_doc([{'id': 'A', 'min_offset': 0, 'max_offset': 0}],
                            [{'id': 's', 'left': 'A', 'right': 'A',
                              'min_delta': 1, 'max_delta': 1}],
                            ['I1'], simple('I1', 'A', 0, 1, 2))
            src.write_text(json.dumps(data))
            run_result = self.call(src, out)
            self.assertEqual(run_result.returncode, 0, run_result.stderr)
            result = json.loads(out.read_bytes())['result']
            self.assertEqual(result, {'status': 'infeasible', 'sources': [],
                                      'events': [], 'incidents': []})

    def test_duplicate_json_key_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'dup.json'
            out = Path(td) / 'out.json'
            body = ('{"sources":[{"id":"A","min_offset":0,"max_offset":0}],'
                    '"syncs":[],"incidents":["I1"],"incidents":["I2"],"events":[]}')
            src.write_text(body)
            self.assertEqual(self.call(src, out).returncode, 2)
            self.assertFalse(out.exists())

    def test_failure_preserves_output_and_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'bad.json'
            out = Path(td) / 'out.json'
            out.write_text('prior')
            for body in ['{}', '{"a":1,"a":2}', 'NaN', '[]']:
                src.write_text(body)
                self.assertEqual(self.call(src, out).returncode, 2)
                self.assertEqual(out.read_text(), 'prior')
            good = Path(td) / 'good.json'
            good.write_bytes((ROOT / 'fixtures/reconcile_input.json').read_bytes())
            before = good.read_bytes()
            self.assertEqual(self.call(good, good).returncode, 2)
            self.assertEqual(good.read_bytes(), before)
            link = Path(td) / 'link.json'
            link.symlink_to(good)
            self.assertEqual(self.call(good, link).returncode, 2)
            self.assertEqual(good.read_bytes(), before)

    def test_mode_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'out.json'
            result = subprocess.run(
                [sys.executable, '-m', 'incident_sla.cli',
                 '--source', str(ROOT / 'fixtures/input.json'),
                 '--reconcile-source', str(ROOT / 'fixtures/reconcile_input.json'),
                 '--output', str(out)],
                capture_output=True, text=True,
                env={**__import__('os').environ, 'PYTHONPATH': str(ROOT / 'src')})
            self.assertEqual(result.returncode, 2)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
