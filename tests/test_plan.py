import json, unittest, tempfile, subprocess, sys, hashlib, itertools
from pathlib import Path

from incident_sla.plan import plan, validate_plan, Planner, verify_schedule, SLOT_SECONDS

ROOT = Path(__file__).resolve().parents[1]


def plan_sample():
    return json.loads((ROOT / 'fixtures/plan_input.json').read_text())


def base_doc(**overrides):
    doc = {
        'slot_seconds': 60,
        'horizon': {'start': 0, 'end': 600},
        'targets': {'response_seconds': 300, 'resolution_seconds': 600},
        'incidents': [],
        'pauses': [],
        'responders': [
            {'id': 'R1', 'available': list(range(10))},
            {'id': 'R2', 'available': list(range(10))},
        ],
    }
    doc.update(overrides)
    return doc


def incident(iid, **overrides):
    row = {
        'id': iid, 'opened': 0, 'customer_ready': 0,
        'resolution_slots': 1, 'value': 10,
        'response_responders': ['R1', 'R2'],
        'resolution_responders': ['R1', 'R2'],
    }
    row.update(overrides)
    return row


def merge_length(intervals):
    """Independent reference implementation of union length for the tests."""
    total = 0
    right = None
    left = None
    for a, b in sorted(intervals):
        if left is None:
            left, right = a, b
        elif a <= right:
            right = max(right, b)
        else:
            total += right - left
            left, right = a, b
    if left is not None:
        total += right - left
    return total


def clock_seconds(doc, iid, response_slot, resolution_slots):
    """Rebuild both active-second figures independently from first principles."""
    start = doc['horizon']['start']
    row = next(i for i in doc['incidents'] if i['id'] == iid)
    opened = row['opened']
    ready = row['customer_ready']
    r_end = start + SLOT_SECONDS * (response_slot + 1)
    f_end = start + SLOT_SECONDS * (max(resolution_slots) + 1)
    explicit = [(p['start'], p['end'])
                for p in doc['pauses'] if p['incident'] == iid]

    def clipped(intervals, lo, hi):
        return [(max(a, lo), min(b, hi)) for a, b in intervals
                if max(a, lo) < min(b, hi)]

    response_active = (r_end - opened) - merge_length(clipped(explicit, opened, r_end))
    automatic = [(r_end, ready)] if r_end < ready else []
    resolution_active = ((f_end - opened)
                         - merge_length(clipped(explicit + automatic, opened, f_end)))
    return response_active, resolution_active, r_end, f_end


def brute_force_optimum(doc):
    """Exhaustively enumerate every legal schedule and return the objective key."""
    spec = validate_plan(doc)
    slots = spec['slots']
    responders = [r['id'] for r in spec['responders']]
    incidents = spec['incidents']
    best = None
    best_sequence = None

    for mask in range(1 << len(incidents)):
        chosen = [i for i in range(len(incidents)) if mask & (1 << i)]
        # Enumerate per chosen incident: one response slot and n resolution slots.
        per_incident_choices = []
        feasible = True
        for i in chosen:
            row = incidents[i]
            need = row['resolution_slots']
            choices = []
            for r in range(slots):
                for res_combo in itertools.combinations(range(r + 1, slots), need):
                    resp_active, res_active, r_end, f_end = clock_seconds(
                        doc, row['id'], r, list(res_combo))
                    if resp_active > spec['targets'][0]:
                        continue
                    if res_active > spec['targets'][1]:
                        continue
                    s0 = spec['start'] + SLOT_SECONDS * r
                    s1 = s0 + SLOT_SECONDS
                    blocked_r = any(a < s1 and b > s0
                                    for a, b in spec['pauses'][row['id']])
                    blocked_res = any(
                        any(a < spec['start'] + SLOT_SECONDS * (s + 1)
                            and b > spec['start'] + SLOT_SECONDS * s
                            for a, b in spec['pauses'][row['id']])
                        for s in res_combo)
                    if blocked_r or blocked_res:
                        continue
                    first_resolution_start = spec['start'] + SLOT_SECONDS * res_combo[0]
                    if first_resolution_start < max(r_end, row['customer_ready']):
                        continue
                    # Every resolution slot must start at or after max(R, ready).
                    earliest_allowed = max(r_end, row['customer_ready'])
                    if any(spec['start'] + SLOT_SECONDS * s < earliest_allowed
                           for s in res_combo):
                        continue
                    choices.append((r, res_combo, r_end, f_end))
            if not choices:
                feasible = False
                break
            per_incident_choices.append(choices)
        if not feasible:
            continue
        for combo in itertools.product(*per_incident_choices):
            # Assign a distinct eligible, available responder per work item.
            items = []
            for (i, choice) in zip(chosen, combo):
                r, res_slots, r_end, f_end = choice
                row = incidents[i]
                items.append((r, row['response_responders'], row['id'], 'response'))
                for s in res_slots:
                    items.append((s, row['resolution_responders'], row['id'], 'resolution'))
            # Reject two work items of one incident in the same slot.
            if len({(it[2], it[0]) for it in items}) != len(items):
                continue
            options = []
            for slot, roster, iid, phase in items:
                availability = {r['id']: r['available'] for r in spec['responders']}
                people = [p for p in responders if p in roster and slot in availability[p]]
                if not people:
                    options = None
                    break
                options.append(people)
            if options is None:
                continue
            for assignment in itertools.product(*options):
                busy = set()
                sequence = []
                ok = True
                for (slot, roster, iid, phase), person in zip(items, assignment):
                    if (person, slot) in busy:
                        ok = False
                        break
                    busy.add((person, slot))
                    sequence.append((slot, person, iid, phase))
                if not ok:
                    continue
                value = sum(incidents[i]['value'] for i in chosen)
                f_sum = sum(c[3] for c in combo)
                key = (-value, -len(chosen), f_sum, tuple(sorted(sequence)))
                if best is None or key < best:
                    best = key
                    best_sequence = tuple(sorted(sequence))
    if best is None:
        return (0, 0, 0), ()
    value, neg_count, f_sum, sequence = best
    return (-value, -neg_count, f_sum), sequence


class Validation(unittest.TestCase):
    def bad(self, doc):
        with self.assertRaises((ValueError, TypeError)):
            plan(doc)

    def test_unknown_top_level_field(self):
        doc = plan_sample()
        doc['extra'] = 1
        self.bad(doc)

    def test_missing_top_level_field(self):
        doc = plan_sample()
        del doc['responders']
        self.bad(doc)

    def test_slot_seconds_must_be_60(self):
        doc = plan_sample()
        doc['slot_seconds'] = 30
        self.bad(doc)

    def test_boolean_is_not_integer(self):
        doc = plan_sample()
        doc['slot_seconds'] = True
        self.bad(doc)
        doc = base_doc(incidents=[incident('I1', value=True)])
        self.bad(doc)
        doc = base_doc(incidents=[incident('I1', opened=False)])
        self.bad(doc)

    def test_horizon_rules(self):
        for start, end in ((60, 60), (-60, 60), (0, 61), (0, 60 * 33),
                           (4102444800 - 30, 4102444800), (120, 60)):
            doc = base_doc(horizon={'start': start, 'end': end})
            self.bad(doc)

    def test_32_slots_accepted(self):
        doc = base_doc(horizon={'start': 0, 'end': 60 * 32})
        result = plan(doc)
        self.assertEqual(result['objective'], {'value': 0, 'count': 0, 'completion_sum': 0})

    def test_incident_limits(self):
        rows = [incident('I%d' % i) for i in range(9)]
        self.bad(base_doc(incidents=rows))

    def test_duplicate_incident_id(self):
        self.bad(base_doc(incidents=[incident('I1'), incident('I1')]))

    def test_identifiers(self):
        self.bad(base_doc(incidents=[incident(' spaced ')]))
        self.bad(base_doc(incidents=[incident('')]))

    def test_opened_and_ready_rules(self):
        self.bad(base_doc(incidents=[incident('I1', opened=60)]))  # later than start
        self.bad(base_doc(incidents=[incident('I1', customer_ready=30)]))
        self.bad(base_doc(incidents=[incident('I1', opened=10)]))  # not /60
        self.bad(base_doc(incidents=[incident('I1', customer_ready=601)]))

    def test_value_and_resolution_limits(self):
        self.bad(base_doc(incidents=[incident('I1', value=0)]))
        self.bad(base_doc(incidents=[incident('I1', value=101)]))
        self.bad(base_doc(incidents=[incident('I1', resolution_slots=0)]))
        self.bad(base_doc(incidents=[incident('I1', resolution_slots=5)]))

    def test_roster_rules(self):
        self.bad(base_doc(incidents=[incident('I1', response_responders=[])]))
        self.bad(base_doc(incidents=[incident('I1', resolution_responders=['R1', 'R1'])]))
        self.bad(base_doc(incidents=[incident('I1', response_responders=['GHOST'])]))

    def test_responder_rules(self):
        self.bad(base_doc(responders=[
            {'id': 'R1', 'available': [0, 0]},
            {'id': 'R2', 'available': []},
        ]))
        self.bad(base_doc(responders=[
            {'id': 'R1', 'available': [10]},
            {'id': 'R2', 'available': []},
        ]))
        self.bad(base_doc(responders=[
            {'id': 'R1', 'available': []},
            {'id': 'R1', 'available': []},
        ]))

    def test_pause_rules(self):
        good = incident('I1')
        self.bad(base_doc(incidents=[good],
                          pauses=[{'incident': 'GHOST', 'start': 0, 'end': 60}]))
        self.bad(base_doc(incidents=[good],
                          pauses=[{'incident': 'I1', 'start': 120, 'end': 120}]))
        self.bad(base_doc(incidents=[good],
                          pauses=[{'incident': 'I1', 'start': 0, 'end': 30}]))
        # Pauses may extend beyond the horizon.
        doc = base_doc(
            horizon={'start': 0, 'end': 300},
            responders=[
                {'id': 'R1', 'available': [0, 1, 2, 3, 4]},
                {'id': 'R2', 'available': [0, 1, 2, 3, 4]},
            ],
            incidents=[incident('I1', resolution_slots=1)],
            pauses=[{'incident': 'I1', 'start': 240, 'end': 900}])
        result = plan(doc)
        self.assertEqual(result['objective']['count'], 1)

    def test_empty_plan_is_legal(self):
        result = plan(base_doc())
        self.assertEqual(result, {
            'objective': {'value': 0, 'count': 0, 'completion_sum': 0},
            'schedule': [], 'selected': [], 'excluded': []})

    def test_zero_responders_with_incident_rejected(self):
        doc = base_doc(responders=[])
        doc['incidents'] = [incident('I1')]
        self.bad(doc)

    def test_zero_responders_without_incident_accepted(self):
        doc = base_doc(responders=[])
        self.assertEqual(plan(doc)['objective']['count'], 0)


class ClockRules(unittest.TestCase):
    def test_overlapping_pause_union_on_response_clock(self):
        doc = base_doc(
            horizon={'start': 0, 'end': 600},
            targets={'response_seconds': 120, 'resolution_seconds': 600},
            incidents=[incident('I1', resolution_slots=1)],
            pauses=[
                {'incident': 'I1', 'start': 0, 'end': 180},
                {'incident': 'I1', 'start': 120, 'end': 180},
            ])
        result = plan(doc)
        # Slots 0..2 intersect the explicit pause union [0,180), so they
        # cannot hold work; the response lands in slot 3 (R=240): wall 240
        # minus the clipped union [0,180)=180 -> 60 active seconds.
        selected = {row['id']: row for row in result['selected']}
        self.assertEqual(selected['I1']['first_response'], 240)
        self.assertEqual(selected['I1']['response_active_seconds'], 60)
        active, _, _, _ = clock_seconds(doc, 'I1', 3, [4])
        self.assertEqual(active, 60)

    def test_r_equals_customer_ready_has_no_auto_pause(self):
        # R < customer_ready creates the automatic pause [R, ready); the
        # earliest resolution work starts at max(R, ready).
        doc = base_doc(
            horizon={'start': 0, 'end': 600},
            incidents=[incident('I1', customer_ready=120, resolution_slots=1)])
        row = plan(doc)['selected'][0]
        self.assertEqual(row['first_response'], 60)   # response in slot 0
        self.assertEqual(row['resolved'], 180)        # resolution slot 2
        # F=180 minus automatic pause [60,120)=60 -> 120 resolution active.
        self.assertEqual(row['resolution_active_seconds'], 120)
        self.assertEqual(row['response_active_seconds'], 60)
        # When the response ends exactly at customer_ready there is no
        # automatic pause, so resolution may start in the very next slot.
        doc2 = base_doc(
            horizon={'start': 0, 'end': 600},
            incidents=[incident('I1', customer_ready=60, resolution_slots=1)])
        row2 = plan(doc2)['selected'][0]
        self.assertEqual(row2['first_response'], 60)
        self.assertEqual(row2['resolved'], 120)
        self.assertEqual(row2['resolution_active_seconds'], 120)

    def test_exactly_meets_threshold(self):
        doc = base_doc(
            horizon={'start': 0, 'end': 600},
            targets={'response_seconds': 60, 'resolution_seconds': 120},
            incidents=[incident('I1', customer_ready=0, resolution_slots=1)])
        result = plan(doc)
        row = result['selected'][0]
        # Earliest slots: response ends at 60 (active exactly 60), resolution
        # ends at 120 (active exactly 120): both meet inclusively.
        self.assertEqual(row['response_active_seconds'], 60)
        self.assertEqual(row['resolution_active_seconds'], 120)
        self.assertEqual(result['objective']['count'], 1)
        # One second tighter on resolution makes every candidate fail.
        doc['targets']['resolution_seconds'] = 119
        self.assertEqual(plan(doc)['objective']['count'], 0)

    def test_zero_thresholds(self):
        doc = base_doc(
            targets={'response_seconds': 0, 'resolution_seconds': 0},
            incidents=[incident('I1')])
        result = plan(doc)
        self.assertEqual(result['objective']['count'], 0)
        self.assertEqual(result['excluded'], ['I1'])

    def test_auto_pause_recomputed_per_candidate_r(self):
        # The automatic pause is derived from the actually selected R, so it
        # must be rebuilt rather than carried across candidates. An explicit
        # pause covering slot 0 forces the response into slot 1.
        common = dict(
            horizon={'start': 0, 'end': 600},
            incidents=[incident('I1', customer_ready=180, resolution_slots=1)],
            targets={'response_seconds': 300, 'resolution_seconds': 600})
        early_doc = base_doc(pauses=[], **common)
        early = plan(early_doc)['selected'][0]
        self.assertEqual(early['first_response'], 60)       # R at end of slot 0
        self.assertEqual(early['resolved'], 240)            # resolution slot 3
        # Automatic pause [60,180)=120 -> F 240 minus 120 = 120 active.
        self.assertEqual(early['resolution_active_seconds'], 120)

        later_doc = base_doc(
            pauses=[{'incident': 'I1', 'start': 0, 'end': 60}], **common)
        later = plan(later_doc)['selected'][0]
        self.assertEqual(later['first_response'], 120)      # R at end of slot 1
        self.assertEqual(later['resolved'], 240)
        # The automatic pause shrinks to [120,180)=60; the explicit pause
        # contributes another 60, leaving 240-120 = 120 active seconds.
        self.assertEqual(later['resolution_active_seconds'], 120)
        # First-principles recomputation: moving R from 60 to 120 shortened
        # the automatic pause from 120 to 60.
        _, active_early, _, _ = clock_seconds(early_doc, 'I1', 0, [3])
        _, active_later, _, _ = clock_seconds(later_doc, 'I1', 1, [3])
        self.assertEqual(active_early, 120)
        self.assertEqual(active_later, 120)


class SharedResources(unittest.TestCase):
    def test_responder_contention_lexicographic_tiebreak(self):
        doc = base_doc(
            horizon={'start': 0, 'end': 600},
            responders=[{'id': 'R1', 'available': list(range(10))}],
            incidents=[
                incident('I1', response_responders=['R1'],
                         resolution_responders=['R1']),
                incident('I2', response_responders=['R1'],
                         resolution_responders=['R1']),
            ])
        result = plan(doc)
        self.assertEqual(result['objective']['count'], 2)
        slots_by_incident = {}
        for item in result['schedule']:
            slots_by_incident.setdefault(item['incident'], []).append(item['slot'])
        self.assertEqual(slots_by_incident['I1'], [0, 1])
        self.assertEqual(slots_by_incident['I2'], [2, 3])

    def test_cross_responder_resolution(self):
        doc = base_doc(
            horizon={'start': 0, 'end': 600},
            responders=[
                {'id': 'R1', 'available': [0]},
                {'id': 'R2', 'available': list(range(1, 10))},
            ],
            incidents=[incident('I1', resolution_slots=3)])
        result = plan(doc)
        response_people = {item['responder'] for item in result['schedule']
                           if item['phase'] == 'response'}
        resolution_people = {item['responder'] for item in result['schedule']
                             if item['phase'] == 'resolution'}
        # The response is served by R1 in slot 0; R1 then disappears and the
        # three resolution slots must all be served by R2.
        self.assertEqual(response_people, {'R1'})
        self.assertEqual(resolution_people, {'R2'})
        self.assertEqual(result['selected'][0]['resolved'], 240)

    def test_full_selection_three_level_objective(self):
        result = plan(plan_sample())
        self.assertEqual(result['objective'],
                         {'value': 80, 'count': 2, 'completion_sum': 600})
        self.assertEqual(result['excluded'], [])
        sequence = [(item['slot'], item['responder'], item['incident'], item['phase'])
                    for item in result['schedule']]
        self.assertEqual(sequence, sorted(sequence))

    def test_contention_forces_r_shift_and_changes_set(self):
        # One responder, two incidents. I-A must be answered in slot 0; the
        # contention pushes I-B's response to slot 1, which shrinks its
        # automatic pause to zero and invalidates its resolution SLA.
        doc = {
            'slot_seconds': 60,
            'horizon': {'start': 0, 'end': 300},
            'targets': {'response_seconds': 120, 'resolution_seconds': 120},
            'incidents': [
                {'id': 'I-A', 'opened': 0, 'customer_ready': 0,
                 'resolution_slots': 1, 'value': 100,
                 'response_responders': ['R1'],
                 'resolution_responders': ['R1']},
                {'id': 'I-B', 'opened': 0, 'customer_ready': 120,
                 'resolution_slots': 1, 'value': 10,
                 'response_responders': ['R1'],
                 'resolution_responders': ['R1']},
            ],
            'pauses': [],
            'responders': [{'id': 'R1', 'available': [0, 1, 2, 3, 4]}],
        }
        result = plan(doc)
        self.assertEqual(result['excluded'], ['I-B'])
        self.assertEqual([row['id'] for row in result['selected']], ['I-A'])
        # Independent per-incident view would accept B at R=60: the automatic
        # pause [60,120) makes its resolution active 120 at F=180.
        resp_active, res_active, _, _ = clock_seconds(doc, 'I-B', 0, [2])
        self.assertEqual((resp_active, res_active), (60, 120))
        # Under contention R shifts to slot 1: the automatic pause vanishes
        # and the same completion at F=180 now costs 180 active seconds.
        resp_active2, res_active2, _, _ = clock_seconds(doc, 'I-B', 1, [2])
        self.assertEqual(resp_active2, 120)
        self.assertEqual(res_active2, 180)
        # A 120-second target admits B alone (its original R=0 timing) but
        # rejects the contention-shifted candidate, so the optimum drops B.
        doc['targets']['resolution_seconds'] = 120
        shifted = plan(doc)
        self.assertEqual(shifted['excluded'], ['I-B'])
        alone = dict(doc)
        alone['incidents'] = [doc['incidents'][1]]
        self.assertEqual(plan(alone)['objective']['count'], 1)


class ExhaustiveCrossCheck(unittest.TestCase):
    def test_small_instances_match_independent_enumeration(self):
        cases = [
            base_doc(),
            base_doc(incidents=[incident('I1', resolution_slots=2)]),
            base_doc(
                responders=[{'id': 'R1', 'available': list(range(10))}],
                incidents=[incident('I1', response_responders=['R1'],
                                    resolution_responders=['R1']),
                           incident('I2', response_responders=['R1'],
                                    resolution_responders=['R1'],
                                    customer_ready=120)]),
            base_doc(
                incidents=[incident('I1', customer_ready=180, resolution_slots=2),
                           incident('I2', value=90)],
                pauses=[{'incident': 'I1', 'start': 60, 'end': 120}]),
        ]
        for doc in cases:
            spec = validate_plan(doc)
            result = Planner(spec).build_report()
            expected_objective, expected_sequence = brute_force_optimum(doc)
            self.assertEqual(
                (result['objective']['value'], result['objective']['count'],
                 result['objective']['completion_sum']),
                expected_objective, doc)
            actual_sequence = tuple((item['slot'], item['responder'],
                                     item['incident'], item['phase'])
                                    for item in result['schedule'])
            self.assertEqual(actual_sequence, expected_sequence, doc)

    def test_random_small_instances(self):
        import random
        rng = random.Random(20261116)
        for trial in range(40):
            slots = rng.randint(3, 6)
            doc = base_doc(horizon={'start': 0, 'end': 60 * slots})
            responder_count = rng.randint(1, 2)
            doc['responders'] = [
                {'id': 'R%d' % j,
                 'available': [s for s in range(slots) if rng.random() > 0.2]}
                for j in range(responder_count)]
            incident_count = rng.randint(0, 3)
            rows = []
            for k in range(incident_count):
                roster = ['R%d' % j for j in range(responder_count)
                          if rng.random() > 0.35] or ['R0']
                rows.append(incident(
                    'I%d' % k,
                    customer_ready=min(rng.choice([0, 60, 120]),
                                       doc['horizon']['end']),
                    resolution_slots=rng.randint(1, 2),
                    value=rng.randint(1, 100),
                    response_responders=roster,
                    resolution_responders=roster))
            doc['incidents'] = rows
            doc['targets'] = {'response_seconds': rng.choice([60, 120, 300]),
                              'resolution_seconds': rng.choice([120, 300, 600])}
            doc['pauses'] = []
            if rows and rng.random() > 0.5:
                row = rng.choice(rows)
                a = rng.randrange(0, slots - 1) * 60
                doc['pauses'] = [{'incident': row['id'], 'start': a,
                                  'end': a + 60}]
            spec = validate_plan(doc)
            result = Planner(spec).build_report()
            expected_objective, expected_sequence = brute_force_optimum(doc)
            self.assertEqual(
                (result['objective']['value'], result['objective']['count'],
                 result['objective']['completion_sum']),
                expected_objective, (trial, doc))
            actual_sequence = tuple((item['slot'], item['responder'],
                                     item['incident'], item['phase'])
                                    for item in result['schedule'])
            self.assertEqual(actual_sequence, expected_sequence, (trial, doc))


class BoundaryAndCli(unittest.TestCase):
    def test_32_slot_boundary_schedule(self):
        doc = {
            'slot_seconds': 60,
            'horizon': {'start': 4102444800 - 60 * 32, 'end': 4102444800},
            'targets': {'response_seconds': 10 ** 9, 'resolution_seconds': 10 ** 9},
            'incidents': [incident(
                'I1', opened=4102444800 - 60 * 32,
                customer_ready=4102444800 - 60 * 32,
                resolution_slots=4)],
            'pauses': [],
            'responders': [
                {'id': 'R1', 'available': list(range(32))},
                {'id': 'R2', 'available': list(range(32))},
            ],
        }
        result = plan(doc)
        self.assertEqual(result['objective']['count'], 1)
        last = max(item['slot'] for item in result['schedule'])
        self.assertEqual(last, 4)  # response slot 0 plus 4 resolution slots
        self.assertEqual(result['selected'][0]['resolved'],
                         4102444800 - 60 * 32 + 60 * 5)

    def call(self, flag, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'incident_sla.cli', flag, str(source),
             '--output', str(output)], capture_output=True, text=True)

    def test_plan_cli_replay_stable(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'out.json'
            src = ROOT / 'fixtures/plan_input.json'
            r = self.call('--plan-source', src, out)
            self.assertEqual(r.returncode, 0, r.stderr)
            first = out.read_bytes()
            report = json.loads(first)
            self.assertEqual(report['schema_version'], 2)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertEqual(self.call('--plan-source', src, out).returncode, 0)
            self.assertEqual(first, out.read_bytes())

    def test_modes_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'out.json'
            r = subprocess.run(
                [sys.executable, '-m', 'incident_sla.cli',
                 '--source', str(ROOT / 'fixtures/input.json'),
                 '--plan-source', str(ROOT / 'fixtures/plan_input.json'),
                 '--output', str(out)], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)

    def test_plan_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'bad.json'
            out = Path(td) / 'out.json'
            out.write_text('prior')
            for body in ['{}', '{"slot_seconds":30}', 'NaN',
                         '{"slot_seconds":60,"horizon":{"start":0,"end":60},'
                         '"targets":{"response_seconds":0,"resolution_seconds":0},'
                         '"incidents":[],"pauses":[],"responders":[],"x":1}']:
                src.write_text(body)
                self.assertEqual(self.call('--plan-source', src, out).returncode, 2)
                self.assertEqual(out.read_text(), 'prior')

    def test_plan_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            src.write_bytes((ROOT / 'fixtures/plan_input.json').read_bytes())
            before = src.read_bytes()
            self.assertEqual(self.call('--plan-source', src, src).returncode, 2)
            self.assertEqual(src.read_bytes(), before)
            link = Path(td) / 'link.json'
            link.symlink_to(src)
            self.assertEqual(self.call('--plan-source', src, link).returncode, 2)
            self.assertEqual(src.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
