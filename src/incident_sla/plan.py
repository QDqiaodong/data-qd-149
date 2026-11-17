"""Shared-responder work planning over fixed 60-second slots (schema v2).

The plan input only contains incidents that have not yet been responded to.
For every candidate selection the SLA clocks are rebuilt from the actually
placed response slot: the response clock runs opened->R against explicit
pauses, while the resolution clock runs opened->F against the union of
explicit pauses and the automatic customer-wait pause [R, customer_ready).
The search is an exhaustive branch-and-bound proof; no greedy, timeout or
approximation result is ever accepted as optimal.
"""
import itertools

from .common import require, ident, rows
from .sla import union

HORIZON_MAX = 4102444800
SLOT_SECONDS = 60
MAX_SLOTS = 32
MAX_INCIDENTS = 8
MAX_RESPONDERS = 3
MAX_PAUSES = 200
TARGET_BOUND = 10 ** 12
PHASE_RESPONSE = 'response'
PHASE_RESOLUTION = 'resolution'


def _int(value, name):
    # bool is a subclass of int; the contract forbids booleans as integers.
    if type(value) is not int:
        raise ValueError(name + ' must be an integer')
    return value


def _nonneg(value, name):
    _int(value, name)
    if not 0 <= value <= TARGET_BOUND:
        raise ValueError(name + ' outside allowed bounds')
    return value


def _slot_second(value, name):
    _int(value, name)
    if value % SLOT_SECONDS:
        raise ValueError(name + ' must be a multiple of 60')
    return value


def validate_plan(data):
    """Validate the whole plan document; any structural flaw rejects all."""
    require(data, 'slot_seconds horizon targets incidents pauses responders')
    if _int(data['slot_seconds'], 'slot_seconds') != SLOT_SECONDS:
        raise ValueError('slot_seconds must be 60')

    horizon = data['horizon']
    require(horizon, 'start end')
    start = _slot_second(horizon['start'], 'horizon.start')
    end = _slot_second(horizon['end'], 'horizon.end')
    if start < 0 or end > HORIZON_MAX or start >= end:
        raise ValueError('horizon must satisfy 0 <= start < end <= 4102444800')
    slots = (end - start) // SLOT_SECONDS
    if not 1 <= slots <= MAX_SLOTS:
        raise ValueError('horizon must span 1 to 32 slots')

    targets = data['targets']
    require(targets, 'response_seconds resolution_seconds')
    target_resp = _nonneg(targets['response_seconds'], 'response_seconds')
    target_res = _nonneg(targets['resolution_seconds'], 'resolution_seconds')

    responder_rows = rows(data['responders'], MAX_RESPONDERS)
    responders = []
    responder_ids = set()
    for row in responder_rows:
        require(row, 'id available')
        rid = ident(row['id'])
        if rid in responder_ids:
            raise ValueError('duplicate responder id: ' + rid)
        available_raw = row['available']
        if not isinstance(available_raw, list):
            raise ValueError('available must be an array of slot numbers')
        available = set()
        for slot in available_raw:
            _int(slot, 'available slot')
            if not 0 <= slot < slots:
                raise ValueError('available slot outside horizon')
            if slot in available:
                raise ValueError('duplicate available slot for responder ' + rid)
            available.add(slot)
        responder_ids.add(rid)
        responders.append({'id': rid, 'available': available})

    incident_rows = rows(data['incidents'], MAX_INCIDENTS)
    incidents = []
    incident_ids = set()
    for row in incident_rows:
        require(row, 'id opened customer_ready resolution_slots value '
                     'response_responders resolution_responders')
        iid = ident(row['id'])
        if iid in incident_ids:
            raise ValueError('duplicate incident id: ' + iid)
        opened = _slot_second(row['opened'], 'opened')
        if opened < 0 or opened > start:
            raise ValueError('opened must be in [0, horizon.start]')
        ready = _slot_second(row['customer_ready'], 'customer_ready')
        if not opened <= ready <= end:
            raise ValueError('customer_ready must be within [opened, horizon.end]')
        resolution_slots = _int(row['resolution_slots'], 'resolution_slots')
        if not 1 <= resolution_slots <= 4:
            raise ValueError('resolution_slots must be 1..4')
        value = _int(row['value'], 'value')
        if not 1 <= value <= 100:
            raise ValueError('value must be 1..100')
        rosters = {}
        for field in ('response_responders', 'resolution_responders'):
            roster_raw = row[field]
            if not isinstance(roster_raw, list) or not roster_raw:
                raise ValueError(field + ' must be a non-empty array')
            roster = []
            for member in roster_raw:
                member_id = ident(member)
                if member_id in roster:
                    raise ValueError('duplicate responder in ' + field + ' of ' + iid)
                if member_id not in responder_ids:
                    raise ValueError('unknown responder reference: ' + member_id)
                roster.append(member_id)
            rosters[field] = roster
        incident_ids.add(iid)
        incidents.append({
            'id': iid, 'opened': opened, 'customer_ready': ready,
            'resolution_slots': resolution_slots, 'value': value,
            'response_responders': rosters['response_responders'],
            'resolution_responders': rosters['resolution_responders'],
        })

    pause_rows = rows(data['pauses'], MAX_PAUSES)
    pauses = {iid: [] for iid in incident_ids}
    for row in pause_rows:
        require(row, 'incident start end')
        iid = ident(row['incident'])
        if iid not in incident_ids:
            raise ValueError('pause references unknown incident: ' + iid)
        pause_start = _slot_second(row['start'], 'pause.start')
        pause_end = _slot_second(row['end'], 'pause.end')
        if pause_end <= pause_start:
            raise ValueError('pause must satisfy end > start')
        pauses[iid].append((pause_start, pause_end))

    return {
        'start': start, 'end': end, 'slots': slots,
        'targets': (target_resp, target_res),
        'responders': responders, 'incidents': incidents, 'pauses': pauses,
    }


def _clipped_union(intervals, lo, hi):
    clipped = []
    for a, b in intervals:
        a = max(a, lo)
        b = min(b, hi)
        if a < b:
            clipped.append((a, b))
    return union(clipped)


class Planner:
    """Exact optimizer over at most 8 incidents / 32 slots / 3 responders."""

    def __init__(self, spec):
        self.spec = spec
        self.start = spec['start']
        self.end = spec['end']
        self.slots = spec['slots']
        self.target_resp, self.target_res = spec['targets']
        self.responders = spec['responders']
        self.responder_count = len(self.responders)
        self.responder_index = {r['id']: j for j, r in enumerate(self.responders)}
        self.availability = [0] * self.slots
        for j, responder in enumerate(self.responders):
            for slot in responder['available']:
                self.availability[slot] |= 1 << j
        self.incidents = spec['incidents']
        self.incident_count = len(self.incidents)
        self._precompute()

    def slot_end(self, slot):
        return self.start + SLOT_SECONDS * (slot + 1)

    def slot_start(self, slot):
        return self.start + SLOT_SECONDS * slot

    def _precompute(self):
        per_incident = []
        for incident in self.incidents:
            iid = incident['id']
            opened = incident['opened']
            ready = incident['customer_ready']
            explicit = self.spec['pauses'][iid]
            pause_slot = [False] * self.slots
            for slot in range(self.slots):
                lo = self.slot_start(slot)
                hi = self.slot_end(slot)
                for a, b in explicit:
                    if a < hi and b > lo:
                        pause_slot[slot] = True
                        break
            resp_bits = [0] * self.slots
            res_bits = [0] * self.slots
            resp_roster = 0
            for member in incident['response_responders']:
                resp_roster |= 1 << self.responder_index[member]
            res_roster = 0
            for member in incident['resolution_responders']:
                res_roster |= 1 << self.responder_index[member]
            resp_sla_ok = [False] * self.slots
            for slot in range(self.slots):
                if not pause_slot[slot]:
                    resp_bits[slot] = resp_roster & self.availability[slot]
                    res_bits[slot] = res_roster & self.availability[slot]
                response_end = self.slot_end(slot)
                active = ((response_end - opened)
                          - _clipped_union(explicit, opened, response_end))
                if resp_bits[slot] != 0 and active <= self.target_resp:
                    resp_sla_ok[slot] = True

            # For each feasible response slot r derive the resolution window.
            lo_by_r = [None] * self.slots
            hi_by_r = [None] * self.slots
            eligible_res_by_r = [None] * self.slots
            eligible_mask_by_r = [0] * self.slots
            # Earliest achievable completion F for each count of resolution
            # slots already placed (given the used-slot mask), is derived in
            # the search; here keep the ordered eligible slot list.
            earliest_completion = [None] * self.slots
            need = incident['resolution_slots']
            ready_slot = -(-(ready - self.start) // SLOT_SECONDS)  # ceil div
            for r in range(self.slots):
                if not resp_sla_ok[r]:
                    continue
                response_end = self.slot_end(r)
                lo = max(r + 1, ready_slot)
                auto = []
                if response_end < ready:
                    auto.append((response_end, ready))
                # Resolution active seconds are non-decreasing in F.
                hi = None
                for f_slot in range(self.slots):
                    finish = self.slot_end(f_slot)
                    active = ((finish - opened)
                              - _clipped_union(explicit + auto, opened, finish))
                    if active <= self.target_res:
                        hi = f_slot
                eligible = [slot for slot in range(lo, self.slots)
                            if hi is not None and slot <= hi and res_bits[slot] != 0]
                if len(eligible) >= need:
                    lo_by_r[r] = lo
                    hi_by_r[r] = hi
                    eligible_res_by_r[r] = eligible
                    mask = 0
                    for slot in eligible:
                        mask |= 1 << slot
                    eligible_mask_by_r[r] = mask
                    earliest_completion[r] = self.slot_end(eligible[need - 1])
                else:
                    # The response clock alone passed, but the resolution
                    # workload cannot finish within its SLA from this R.
                    resp_sla_ok[r] = False
            per_incident.append({
                'pause_slot': pause_slot,
                'resp_bits': resp_bits,
                'res_bits': res_bits,
                'resp_sla_ok': resp_sla_ok,
                'lo_by_r': lo_by_r,
                'hi_by_r': hi_by_r,
                'eligible_res_by_r': eligible_res_by_r,
                'eligible_mask_by_r': eligible_mask_by_r,
                'earliest_completion': earliest_completion,
                'feasible_r': [r for r in range(self.slots)
                               if eligible_res_by_r[r] is not None],
                'global_earliest_f': min(
                    (f for f in earliest_completion if f is not None),
                    default=None),
            })
        self.per_incident = per_incident

    # -- exhaustive search for one fixed subset -----------------------------

    def solve_subset(self, positions):
        """Return (minimum completion_sum, lex-min work sequence) or None.

        Exact slot-sweep dynamic program with no incumbent-as-answer, timeout
        or approximation:

        * A state is (slot k, per-incident (response slot, resolution count)).
          The already-used resolution slots are not part of the key: at most
          one work item lands per incident per slot and resolution slots are
          mutually distinct, so the minimum future completion sum from a given
          (response slot, count) is independent of the slots consumed to reach
          it. Feasibility counts eligible slots still lying ahead.
        * For each slot, every placeable set of work items (at most one per
          incident and at most ``m`` items) is checked by a bipartite match
          against the responders eligible and available in that slot.
          Responder identity never enters the memoized state.
        * Phase A memoizes the minimum future completion sum. Phase B keeps,
          on that minimum-F frontier, the lexicographically smallest work
          sequence, resolving each slot to its canonical responder tuple.
        """
        positions = list(positions)
        if not positions:
            return 0, ()
        info = self.per_incident
        needs = [self.incidents[i]['resolution_slots'] for i in positions]
        m = self.responder_count
        infinity = 10 ** 18

        # Equivalence classes of responders relative to this subset.
        def responder_signature(j):
            roster_bits = []
            for incident in positions:
                row = self.incidents[incident]
                in_response = self.responders[j]['id'] in row['response_responders']
                in_resolution = self.responders[j]['id'] in row['resolution_responders']
                roster_bits.append((in_response, in_resolution))
            return (frozenset(self.responders[j]['available']), tuple(roster_bits))

        signatures = [responder_signature(j) for j in range(m)]
        class_of = [0] * m
        classes = {}
        for j in sorted(range(m), key=lambda x: self.responders[x]['id']):
            sig = signatures[j]
            classes.setdefault(sig, []).append(j)
        class_members = list(classes.values())
        rank_in_class = {}
        for cls, members in enumerate(class_members):
            for rank, j in enumerate(members):
                class_of[j] = cls
                rank_in_class[j] = rank

        def forward_check(k, states):
            total_left = 0
            for idx, i in enumerate(positions):
                r, count = states[idx]
                if r == -1:
                    if not any(info[i]['resp_sla_ok'][slot]
                               for slot in range(k, self.slots)):
                        return False
                    total_left += 1 + needs[idx]
                else:
                    total_left += needs[idx] - count
                    if count < needs[idx]:
                        free = info[i]['eligible_mask_by_r'][r] & ~((1 << k) - 1)
                        if free.bit_count() < needs[idx] - count:
                            return False
            return total_left <= m * (self.slots - k)

        def slot_candidates(k, states):
            items = []
            for idx, i in enumerate(positions):
                r, count = states[idx]
                if r == -1:
                    if info[i]['resp_sla_ok'][k]:
                        items.append((idx, PHASE_RESPONSE, info[i]['resp_bits'][k]))
                elif (count < needs[idx]
                      and info[i]['lo_by_r'][r] <= k <= info[i]['hi_by_r'][r]):
                    bits = info[i]['res_bits'][k]
                    if bits:
                        items.append((idx, PHASE_RESOLUTION, bits))
            return items

        def match_exists(items, chosen):
            bits = [items[ci][2] for ci in chosen]

            def backtrack(ci, used):
                if ci == len(bits):
                    return True
                available = bits[ci] & ~used
                while available:
                    low = available & -available
                    if backtrack(ci + 1, used | low):
                        return True
                    available -= low
                return False

            return backtrack(0, 0)

        def feasible_item_sets(items):
            sets = []
            for size in range(min(m, len(items)), -1, -1):
                for combo in itertools.combinations(range(len(items)), size):
                    if size == 0 or match_exists(items, combo):
                        sets.append(combo)
            return sets

        def canonical_assignment(k, items, combo):
            """Lex-smallest distinct responders for the chosen items."""
            assignment = [None] * len(combo)

            def backtrack(ci, used):
                if ci == len(combo):
                    return True
                eligible = []
                bits = items[combo[ci]][2] & ~used
                while bits:
                    low = bits & -bits
                    eligible.append(low.bit_length() - 1)
                    bits -= low
                # Prefer the lowest-rank free responder of each class first.
                eligible.sort(key=lambda j: (class_of[j], rank_in_class[j], j))
                for responder in eligible:
                    assignment[ci] = responder
                    if backtrack(ci + 1, used | (1 << responder)):
                        return True
                return False

            if not backtrack(0, 0):
                raise ValueError('matching disappeared during reconstruction')
            return tuple(sorted(
                (k, self.responders[assignment[ci]]['id'],
                 self.incidents[positions[items[combo[ci]][0]]]['id'],
                 items[combo[ci]][1])
                for ci in range(len(combo))))

        def apply_items(k, states, items, combo):
            next_states = list(states)
            added_f = 0
            for ci in combo:
                idx, phase, _ = items[ci]
                r, count = next_states[idx]
                if phase == PHASE_RESPONSE:
                    next_states[idx] = (k, count)
                else:
                    count += 1
                    next_states[idx] = (r, count)
                    if count == needs[idx]:
                        added_f += self.slot_end(k)
            return tuple(next_states), added_f

        # Phase A: exact forward sweep over slots. Each layer maps a reachable
        # per-incident state tuple to its minimum accumulated completion sum.
        # The full layers are retained (no dominance pruning) so that phase B
        # can recover the lexicographically smallest minimum-F sequence.
        initial = tuple((-1, 0) for _ in positions)
        frontier = {initial: 0}
        layers = [dict(frontier)]
        for k in range(self.slots):
            next_layer = {}
            for states, accumulated in frontier.items():
                if not forward_check(k, states):
                    continue
                items = slot_candidates(k, states)
                for combo in feasible_item_sets(items):
                    next_states, added_f = apply_items(k, states, items, combo)
                    value = accumulated + added_f
                    old = next_layer.get(next_states)
                    if old is None or value < old:
                        next_layer[next_states] = value
            # Exact layers are retained so lex reconstruction sees every
            # minimum-F path; pruning could discard a lex-relevant state.
            frontier = next_layer
            layers.append(dict(frontier))

        minimum = infinity
        for states, accumulated in frontier.items():
            if all(r != -1 and count == needs[idx]
                   for idx, (r, count) in enumerate(states)):
                minimum = min(minimum, accumulated)
        if minimum >= infinity:
            return None

        # Phase B: lex-smallest full sequence among completions whose total
        # equals the proven minimum. The walk tracks the accumulated F and only
        # follows transitions whose resulting state carries the layer's minimum
        # accumulated value on a path that can still reach the global minimum.
        lex_memo = {}

        def lex_dp(k, states, accumulated):
            key = (k, states, accumulated)
            if key in lex_memo:
                return lex_memo[key]
            if k == self.slots:
                lex_memo[key] = () if (
                    accumulated == minimum and all(
                        r != -1 and count == needs[idx]
                        for idx, (r, count) in enumerate(states))) else None
                return lex_memo[key]
            items = slot_candidates(k, states)
            best_suffix = None
            for combo in feasible_item_sets(items):
                next_states, added_f = apply_items(k, states, items, combo)
                next_accumulated = accumulated + added_f
                # Stay only on a minimum-accumulated path to next_states.
                if layers[k + 1].get(next_states) != next_accumulated:
                    continue
                suffix = lex_dp(k + 1, next_states, next_accumulated)
                if suffix is None:
                    continue
                prefix = canonical_assignment(k, items, combo)
                candidate = prefix + suffix
                if best_suffix is None or candidate < best_suffix:
                    best_suffix = candidate
            lex_memo[key] = best_suffix
            return best_suffix

        sequence = lex_dp(0, initial, 0)
        if sequence is None:
            raise ValueError('lex reconstruction failed')
        return minimum, sequence

    def optimize(self):
        subsets = []
        for mask in range(1 << self.incident_count):
            positions = [i for i in range(self.incident_count) if mask & (1 << i)]
            value = sum(self.incidents[i]['value'] for i in positions)
            subsets.append((value, len(positions), mask, positions))
        # Primary tiers: maximize value, then count. The first feasible tier
        # is globally optimal on both; lower tiers are never considered.
        subsets.sort(key=lambda item: (-item[0], -item[1]))
        best = None  # (completion_sum, sequence, mask), compared within one tier
        current_level = None
        for value, count, mask, positions in subsets:
            if current_level is None:
                current_level = (value, count)
            elif (value, count) != current_level:
                if best is not None:
                    break  # the previous tier was feasible, so it wins
                current_level = (value, count)
            if any(not self.per_incident[i]['feasible_r'] for i in positions):
                continue
            result = self.solve_subset(positions)
            if result is None:
                continue
            candidate = (result[0], result[1], mask)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        return best

    def build_report(self):
        completion_sum, sequence, mask = self.optimize()
        selected_positions = [i for i in range(self.incident_count)
                              if mask & (1 << i)]
        selected_set = {self.incidents[i]['id'] for i in selected_positions}
        schedule = [
            {'slot': slot, 'responder': responder,
             'incident': incident, 'phase': phase}
            for slot, responder, incident, phase in sequence
        ]
        verified = verify_schedule(self.spec, selected_set, schedule)
        value_total = sum(self.incidents[i]['value'] for i in selected_positions)
        if value_total != verified['value'] or completion_sum != verified['completion_sum']:
            raise ValueError('internal plan verification failed')
        selected = []
        for i in sorted(selected_positions, key=lambda pos: self.incidents[pos]['id']):
            iid = self.incidents[i]['id']
            selected.append({
                'id': iid,
                'first_response': verified['clock'][iid]['first_response'],
                'resolved': verified['clock'][iid]['resolved'],
                'response_active_seconds':
                    verified['clock'][iid]['response_active_seconds'],
                'resolution_active_seconds':
                    verified['clock'][iid]['resolution_active_seconds'],
            })
        excluded = sorted(self.incidents[i]['id']
                          for i in range(self.incident_count)
                          if not (mask & (1 << i)))
        return {
            'objective': {
                'value': value_total,
                'count': len(selected_positions),
                'completion_sum': completion_sum,
            },
            'schedule': schedule,
            'selected': selected,
            'excluded': excluded,
        }


def verify_schedule(spec, selected_set, schedule):
    """Rebuild every reported fact from the work items and reject inconsistency."""
    start = spec['start']
    slots = spec['slots']
    responder_index = {r['id']: j for j, r in enumerate(spec['responders'])}
    availability = [set(r['available']) for r in spec['responders']]
    incidents = {i['id']: i for i in spec['incidents']}

    ordered_key = lambda item: (item['slot'], item['responder'],
                                item['incident'], item['phase'])
    if [ordered_key(item) for item in schedule] != sorted(ordered_key(item) for item in schedule):
        raise ValueError('schedule is not in canonical order')

    responder_slot_busy = set()
    incident_slot_busy = set()
    by_incident = {iid: {'response': [], 'resolution': []} for iid in selected_set}
    for item in schedule:
        if set(item) != {'slot', 'responder', 'incident', 'phase'}:
            raise ValueError('malformed schedule item')
        slot = item['slot']
        if type(slot) is not int or not 0 <= slot < slots:
            raise ValueError('schedule slot outside horizon')
        rid = item['responder']
        iid = item['incident']
        phase = item['phase']
        if iid not in selected_set:
            raise ValueError('work scheduled for an unselected incident')
        if rid not in responder_index:
            raise ValueError('schedule references unknown responder')
        if phase not in (PHASE_RESPONSE, PHASE_RESOLUTION):
            raise ValueError('unknown schedule phase')
        if (rid, slot) in responder_slot_busy:
            raise ValueError('responder assigned twice in one slot')
        if (iid, slot) in incident_slot_busy:
            raise ValueError('incident assigned twice in one slot')
        responder_slot_busy.add((rid, slot))
        incident_slot_busy.add((iid, slot))
        incident = incidents[iid]
        roster = (incident['response_responders'] if phase == PHASE_RESPONSE
                  else incident['resolution_responders'])
        if rid not in roster or slot not in availability[responder_index[rid]]:
            raise ValueError('work outside roster or availability')
        slot_lo = start + SLOT_SECONDS * slot
        slot_hi = slot_lo + SLOT_SECONDS
        for a, b in spec['pauses'][iid]:
            if a < slot_hi and b > slot_lo:
                raise ValueError('work intersects an explicit pause')
        by_incident[iid][phase].append(slot)

    clock = {}
    completion_sum = 0
    for iid in sorted(selected_set):
        incident = incidents[iid]
        work = by_incident[iid]
        if len(work[PHASE_RESPONSE]) != 1:
            raise ValueError('selected incident needs exactly one response slot')
        if len(work[PHASE_RESOLUTION]) != incident['resolution_slots']:
            raise ValueError('selected incident has incomplete resolution work')
        r = work[PHASE_RESPONSE][0]
        resolution_slots = sorted(work[PHASE_RESOLUTION])
        if any(slot <= r for slot in resolution_slots):
            raise ValueError('resolution work must follow the response')
        response_end = start + SLOT_SECONDS * (r + 1)
        ready = incident['customer_ready']
        earliest_start = max(response_end, ready)
        for slot in resolution_slots:
            if start + SLOT_SECONDS * slot < earliest_start:
                raise ValueError('resolution started before max(R, customer_ready)')
        finish = start + SLOT_SECONDS * (resolution_slots[-1] + 1)
        opened = incident['opened']
        explicit = spec['pauses'][iid]
        response_active = ((response_end - opened)
                           - _clipped_union(explicit, opened, response_end))
        automatic = []
        if response_end < ready:
            automatic.append((response_end, ready))
        resolution_active = ((finish - opened)
                             - _clipped_union(explicit + automatic, opened, finish))
        if response_active > spec['targets'][0]:
            raise ValueError('response SLA violated by rebuilt schedule')
        if resolution_active > spec['targets'][1]:
            raise ValueError('resolution SLA violated by rebuilt schedule')
        completion_sum += finish
        clock[iid] = {
            'first_response': response_end,
            'resolved': finish,
            'response_active_seconds': response_active,
            'resolution_active_seconds': resolution_active,
        }
    if schedule and set(by_incident) != selected_set:
        raise ValueError('selected incident without work')
    return {
        'clock': clock,
        'completion_sum': completion_sum,
        'value': sum(incidents[iid]['value'] for iid in selected_set),
    }


def plan(data):
    spec = validate_plan(data)
    if not spec['incidents']:
        return {
            'objective': {'value': 0, 'count': 0, 'completion_sum': 0},
            'schedule': [], 'selected': [], 'excluded': [],
        }
    return Planner(spec).build_report()
