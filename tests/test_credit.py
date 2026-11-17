import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from incident_sla.sla import merge, union
from incident_sla.credit import (
    validate_config, validate_batch, evaluate_services, combine_children,
    clip_intervals, liability_cents, init_db, apply_batch, view_report,
)

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run(
        [sys.executable, '-m', 'incident_sla.cli', *[str(a) for a in args]],
        capture_output=True, text=True)


def diamond_config():
    """One shared component feeding two sub-services below one root."""
    return {
        'components': ['db-shared', 'cache-a', 'store-b'],
        'services': [
            {'id': 'svc-read', 'children': ['db-shared', 'cache-a'],
             'required': 2},
            {'id': 'svc-write', 'children': ['db-shared', 'store-b'],
             'required': 2},
            {'id': 'svc-root', 'children': ['svc-read', 'svc-write'],
             'required': 2},
        ],
        'contracts': [{
            'id': 'gold-2026',
            'service': 'svc-root',
            'cycles': [{'id': '2026-01', 'start': 0, 'end': 1000,
                        'fee_cents': 1000000}],
            'tiers': [
                {'after_seconds': 0, 'rate_bps': 0},
                {'after_seconds': 100, 'rate_bps': 2500},
                {'after_seconds': 500, 'rate_bps': 10000},
            ],
        }],
    }


def batch(batch_id, replacements=None, close=None):
    return {
        'batch_id': batch_id,
        'replacements': replacements or [],
        'close': close or [],
    }


def oracle_downtime(spec, facts, service_id, lo, hi):
    """Independent per-second oracle used only on tiny test domains."""
    services = spec['services']

    def down(node, t):
        if node in services:
            service = services[node]
            available = sum(0 if down(child, t) else 1
                            for child in service['children'])
            return available < service['required']
        return any(a <= t < b for a, b in facts.get(node, []))

    return sum(1 for t in range(lo, hi) if down(service_id, t))


class MergeAndUnion(unittest.TestCase):
    """The shared canonicalizer; union keeps its numeric v1/v2 contract."""

    def test_empty(self):
        self.assertEqual(merge([]), [])
        self.assertEqual(union([]), 0)

    def test_overlapping_adjacent_nested(self):
        self.assertEqual(merge([(0, 10), (5, 15), (15, 20), (3, 4)]),
                         [(0, 20)])
        self.assertEqual(union([(0, 10), (5, 15), (15, 20), (3, 4)]), 20)

    def test_unordered_and_disjoint(self):
        self.assertEqual(merge([(30, 40), (0, 5), (10, 20)]),
                         [(0, 5), (10, 20), (30, 40)])
        self.assertEqual(union([(30, 40), (0, 5), (10, 20)]), 25)

    def test_touching_merges_half_open(self):
        self.assertEqual(merge([(0, 10), (10, 20)]), [(0, 20)])
        self.assertEqual(union([(0, 10), (10, 20)]), 20)

    def test_degenerate_spans_contribute_nothing(self):
        self.assertEqual(merge([(5, 5), (0, 3)]), [(0, 3)])
        self.assertEqual(union([(5, 5), (0, 3)]), 3)

    def test_union_matches_per_second_oracle(self):
        rng = random.Random(20261117)
        for _ in range(200):
            intervals = []
            for _ in range(rng.randint(0, 8)):
                a = rng.randint(0, 60)
                b = rng.randint(a + 1, a + 1 + rng.randint(1, 15))
                intervals.append((a, b))
            covered = set()
            for a, b in intervals:
                covered.update(range(a, b))
            self.assertEqual(union(intervals), len(covered))
            canonical = merge(intervals)
            self.assertEqual(sum(b - a for a, b in canonical), len(covered))
            for (a, b), (c, d) in zip(canonical, canonical[1:]):
                self.assertLess(b, c)  # strictly disjoint, non-touching


class ConfigValidation(unittest.TestCase):
    def bad(self, doc):
        with self.assertRaises((ValueError, TypeError)):
            validate_config(doc)

    def good(self, doc):
        return validate_config(doc)

    def test_unknown_and_missing_fields(self):
        doc = diamond_config()
        doc['extra'] = 1
        self.bad(doc)
        doc = diamond_config()
        del doc['contracts']
        self.bad(doc)

    def test_component_rules(self):
        doc = diamond_config()
        doc['components'] = ['a', 'a']
        self.bad(doc)
        doc = diamond_config()
        doc['components'] = [' spaced ']
        self.bad(doc)
        doc = diamond_config()
        doc['components'] = ['c%d' % i for i in range(65)]
        self.bad(doc)
        doc = diamond_config()
        doc['components'] = ['svc-read']  # id shared with a service
        self.bad(doc)

    def test_service_rules(self):
        doc = diamond_config()
        doc['services'][0]['children'] = []
        self.bad(doc)
        doc = diamond_config()
        doc['services'][0]['children'] = ['db-shared', 'db-shared']
        self.bad(doc)
        doc = diamond_config()
        doc['services'][0]['children'] = ['ghost']
        self.bad(doc)
        doc = diamond_config()
        doc['services'][0]['required'] = 0
        self.bad(doc)
        doc = diamond_config()
        doc['services'][0]['required'] = 3
        self.bad(doc)
        doc = diamond_config()
        doc['services'][0]['required'] = True
        self.bad(doc)
        doc = diamond_config()
        doc['services'].append({'id': 'svc-read', 'children': ['db-shared'],
                                'required': 1})
        self.bad(doc)
        doc = diamond_config()
        doc['services'] = [
            {'id': 's%d' % i, 'children': ['db-shared'], 'required': 1}
            for i in range(65)]
        self.bad(doc)

    def test_service_cycles_rejected(self):
        doc = diamond_config()
        doc['services'][0]['children'] = ['svc-read']  # self loop
        self.bad(doc)
        doc = diamond_config()
        doc['services'] = [
            {'id': 's1', 'children': ['s2'], 'required': 1},
            {'id': 's2', 'children': ['s1'], 'required': 1},
        ]
        self.bad(doc)

    def test_contract_rules(self):
        doc = diamond_config()
        doc['contracts'][0]['service'] = 'db-shared'  # not a service
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'].append(doc['contracts'][0])
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['cycles'].append(
            {'id': '2026-01', 'start': 1, 'end': 2, 'fee_cents': 1})
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['cycles'] = [
            {'id': 'a', 'start': 0, 'end': 100, 'fee_cents': 1},
            {'id': 'b', 'start': 50, 'end': 150, 'fee_cents': 1},
        ]
        self.bad(doc)
        # Touching half-open cycles are disjoint and legal.
        doc = diamond_config()
        doc['contracts'][0]['cycles'] = [
            {'id': 'a', 'start': 0, 'end': 100, 'fee_cents': 1},
            {'id': 'b', 'start': 100, 'end': 150, 'fee_cents': 1},
        ]
        self.good(doc)
        doc = diamond_config()
        doc['contracts'][0]['cycles'][0]['end'] = 0
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['cycles'][0]['fee_cents'] = 10 ** 12 + 1
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['cycles'][0]['start'] = -1
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['cycles'][0]['end'] = 4102444801
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['cycles'][0]['end'] = 4102444800
        self.good(doc)

    def test_tier_rules(self):
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = []
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = [{'after_seconds': 1, 'rate_bps': 0}]
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = [{'after_seconds': 0, 'rate_bps': 5}]
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = [
            {'after_seconds': 0, 'rate_bps': 0},
            {'after_seconds': 100, 'rate_bps': 10},
            {'after_seconds': 100, 'rate_bps': 20},
        ]
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = [
            {'after_seconds': 0, 'rate_bps': 0},
            {'after_seconds': 100, 'rate_bps': 20},
            {'after_seconds': 200, 'rate_bps': 10},
        ]
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = [
            {'after_seconds': 0, 'rate_bps': 0},
            {'after_seconds': 100, 'rate_bps': 10001},
        ]
        self.bad(doc)
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = [
            {'after_seconds': 0, 'rate_bps': 0},
            {'after_seconds': 100, 'rate_bps': False},
        ]
        self.bad(doc)
        # Flat zero ladder is legal.
        doc = diamond_config()
        doc['contracts'][0]['tiers'] = [{'after_seconds': 0, 'rate_bps': 0}]
        self.good(doc)


class BatchValidation(unittest.TestCase):
    def setUp(self):
        self.spec = validate_config(diamond_config())
        self.spec['component_set'] = set(self.spec['components'])
        self.spec['cycles_by_contract'] = {
            c['id']: {y['id'] for y in c['cycles']} for c in self.spec['contracts']}

    def bad(self, doc):
        with self.assertRaises((ValueError, TypeError)):
            validate_batch(doc, self.spec)

    def test_unknown_fields(self):
        doc = batch('B1')
        doc['extra'] = 1
        self.bad(doc)
        doc = batch('B1')
        del doc['close']
        self.bad(doc)

    def test_component_rules(self):
        self.bad(batch('B1', [{'component': 'ghost', 'intervals': []}]))
        self.bad(batch('B1', [{'component': 'svc-read', 'intervals': []}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': []},
                              {'component': 'db-shared', 'intervals': []}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [[5, 5]]}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [[9, 5]]}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [[-1, 5]]}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [[0, 4102444801]]}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [[0, True]]}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [[0, 1, 2]]}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [[0]]}]))
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': [{'start': 0, 'end': 1}]}]))

    def test_interval_limits(self):
        many = [[i, i + 1] for i in range(0, 20001)]
        self.bad(batch('B1', [{'component': 'db-shared', 'intervals': many}]))
        half = [[i * 4, i * 4 + 1] for i in range(10000)]
        doc = batch('B1', [{'component': 'db-shared', 'intervals': half},
                           {'component': 'cache-a',
                            'intervals': half + [[4102444799, 4102444800]]}])
        self.bad(doc)  # 20001 in total

    def test_close_rules(self):
        self.bad(batch('B1', close=[{'contract': 'ghost', 'cycle': '2026-01'}]))
        self.bad(batch('B1', close=[{'contract': 'gold-2026', 'cycle': 'ghost'}]))
        self.bad(batch('B1', close=[{'contract': 'gold-2026', 'cycle': '2026-01'},
                                    {'contract': 'gold-2026', 'cycle': '2026-01'}]))

    def test_replacement_normalizes(self):
        parsed = validate_batch(
            batch('B1', [{'component': 'db-shared',
                          'intervals': [[10, 20], [0, 5], [3, 8], [20, 30]]}]),
            self.spec)
        self.assertEqual(parsed['replacements'],
                         [('db-shared', [(0, 8), (10, 30)])])


class FaultImpact(unittest.TestCase):
    """Exact interval propagation over the threshold DAG."""

    def evaluate(self, config, facts):
        spec = validate_config(config)
        return evaluate_services(spec, facts)

    def test_diamond_shared_component_counted_once(self):
        down = self.evaluate(diamond_config(), {'db-shared': [(0, 100)]})
        # The shared component takes both sub-services and therefore the root
        # down for exactly [0,100): correlated, never 200 seconds.
        self.assertEqual(down['svc-read'], [(0, 100)])
        self.assertEqual(down['svc-write'], [(0, 100)])
        self.assertEqual(down['svc-root'], [(0, 100)])

    def test_one_of_two_and_two_of_two(self):
        config = {
            'components': ['a', 'b'],
            'services': [
                {'id': 'any-down', 'children': ['a', 'b'], 'required': 2},
                {'id': 'both-down', 'children': ['a', 'b'], 'required': 1},
            ],
            'contracts': [],
        }
        facts = {'a': [(0, 50)], 'b': [(25, 75)]}
        down = self.evaluate(config, facts)
        self.assertEqual(down['any-down'], [(0, 75)])   # 2-of-2
        self.assertEqual(down['both-down'], [(25, 50)])  # 1-of-2

    def test_same_instant_endpoints_half_open(self):
        config = {
            'components': ['a', 'b'],
            'services': [
                {'id': 'any-down', 'children': ['a', 'b'], 'required': 2},
                {'id': 'both-down', 'children': ['a', 'b'], 'required': 1},
            ],
            'contracts': [],
        }
        # a hands over to b at t=10 with no overlap and no gap.
        facts = {'a': [(0, 10)], 'b': [(10, 20)]}
        down = self.evaluate(config, facts)
        self.assertEqual(down['any-down'], [(0, 20)])
        self.assertEqual(down['both-down'], [])

    def test_nested_and_adjacent_intervals(self):
        config = {
            'components': ['a'],
            'services': [{'id': 's', 'children': ['a'], 'required': 1}],
            'contracts': [],
        }
        facts = {'a': [(0, 10), (5, 15), (15, 20), (2, 3), (40, 50)]}
        down = self.evaluate(config, facts)
        self.assertEqual(down['s'], [(0, 20), (40, 50)])

    def test_deep_shared_subgraph(self):
        # x reaches the root through two independent mid services; a naive
        # sum of sub-service seconds would double count.
        config = {
            'components': ['x', 'p', 'q'],
            'services': [
                {'id': 'left', 'children': ['x', 'p'], 'required': 2},
                {'id': 'right', 'children': ['x', 'q'], 'required': 2},
                {'id': 'root', 'children': ['left', 'right'], 'required': 1},
            ],
            'contracts': [],
        }
        down = self.evaluate(config, {'x': [(0, 60)]})
        self.assertEqual(down['root'], [(0, 60)])
        # root needs only one child; with x up but p down, left is down and
        # right stays up, so root stays up.
        down = self.evaluate(config, {'p': [(0, 60)]})
        self.assertEqual(down['root'], [])

    def test_clip_and_cross_cycle_split(self):
        intervals = [(50, 150)]
        self.assertEqual(clip_intervals(intervals, 0, 100), [(50, 100)])
        self.assertEqual(clip_intervals(intervals, 100, 200), [(100, 150)])
        self.assertEqual(clip_intervals(intervals, 200, 300), [])
        self.assertEqual(clip_intervals([(0, 10), (20, 30)], 5, 25),
                         [(5, 10), (20, 25)])

    def test_tier_selection_and_single_floor(self):
        cycle = {'fee_cents': 999, 'start': 0, 'end': 1000}
        tiers = [{'after_seconds': 0, 'rate_bps': 0},
                 {'after_seconds': 100, 'rate_bps': 3333},
                 {'after_seconds': 500, 'rate_bps': 10000}]
        self.assertEqual(liability_cents(cycle, tiers, 99), 0)
        self.assertEqual(liability_cents(cycle, tiers, 100), 999 * 3333 // 10000)
        self.assertEqual(liability_cents(cycle, tiers, 499), 999 * 3333 // 10000)
        self.assertEqual(liability_cents(cycle, tiers, 500), 999)
        # The floor lands once on the cycle total: 999*3333/10000 = 332.9667
        self.assertEqual(liability_cents(cycle, tiers, 100), 332)

    def test_per_second_oracle_fuzz(self):
        rng = random.Random(1490003)
        for trial in range(120):
            component_count = rng.randint(2, 5)
            components = ['c%d' % i for i in range(component_count)]
            services = []
            available_ids = list(components)
            for i in range(rng.randint(1, 5)):
                child_count = rng.randint(1, min(3, len(available_ids)))
                children = rng.sample(available_ids, child_count)
                sid = 's%d' % i
                services.append({'id': sid, 'children': children,
                                 'required': rng.randint(1, child_count)})
                available_ids.append(sid)
            horizon = 40
            cycle_start = rng.randint(0, horizon - 5)
            cycle_end = rng.randint(cycle_start + 1, horizon)
            config = {
                'components': components,
                'services': services,
                'contracts': [{
                    'id': 'ct', 'service': services[-1]['id'],
                    'cycles': [{'id': 'y', 'start': cycle_start,
                                'end': cycle_end, 'fee_cents': 1000}],
                    'tiers': [{'after_seconds': 0, 'rate_bps': 0}],
                }],
            }
            spec = validate_config(config)
            facts = {}
            for component in components:
                spans = []
                for _ in range(rng.randint(0, 4)):
                    a = rng.randint(0, horizon - 1)
                    b = rng.randint(a + 1, horizon)
                    spans.append((a, b))
                facts[component] = merge(spans)
            down = evaluate_services(spec, facts)
            for service in services:
                sid = service['id']
                clipped = clip_intervals(down[sid], cycle_start, cycle_end)
                production = sum(b - a for a, b in clipped)
                expected = oracle_downtime(spec, facts, sid, cycle_start, cycle_end)
                self.assertEqual(production, expected, (trial, sid, config, facts))


class LedgerCase(unittest.TestCase):
    """Function-level ledger behaviour over temporary databases."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)
        self.db = self.dir / 'ledger.db'
        self.config_path = self.dir / 'config.json'
        self.config_path.write_text(json.dumps(diamond_config()))
        init_db(self.config_path, self.db)

    def write_batch(self, name, doc):
        path = self.dir / name
        path.write_text(json.dumps(doc))
        return path

    def apply(self, doc, name='batch.json'):
        path = self.write_batch(name, doc)
        out = self.dir / (name + '.out.json')
        return apply_batch(path, self.db, out), out

    def test_close_uses_post_correction_liability(self):
        report, _ = self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}],
            close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]))
        row = report['cycles'][0]
        self.assertEqual(report['seq'], 1)
        self.assertTrue(row['closed'])
        self.assertEqual(row['outage_intervals'], [[0, 120]])
        self.assertEqual(row['downtime_seconds'], 120)
        self.assertEqual(row['liability_cents'], 250000)
        self.assertEqual(row['posted_cents'], 250000)
        self.assertEqual(report['entries'], [
            {'seq': 1, 'contract': 'gold-2026', 'cycle': '2026-01',
             'kind': 'close', 'amount_cents': 250000}])

    def test_zero_amount_close_is_recorded(self):
        report, _ = self.apply(batch('B1', [], [
            {'contract': 'gold-2026', 'cycle': '2026-01'}]))
        self.assertEqual(report['cycles'][0]['liability_cents'], 0)
        self.assertEqual(report['entries'], [
            {'seq': 1, 'contract': 'gold-2026', 'cycle': '2026-01',
             'kind': 'close', 'amount_cents': 0}])

    def test_unclosed_cycle_posts_nothing(self):
        report, _ = self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 600]]}]))
        row = report['cycles'][0]
        self.assertFalse(row['closed'])
        self.assertEqual(row['liability_cents'], 1000000)
        self.assertEqual(row['posted_cents'], 0)
        self.assertEqual(report['entries'], [])

    def test_repeat_close_in_later_batch_rejected_and_rolls_back(self):
        self.apply(batch('B1', [], [
            {'contract': 'gold-2026', 'cycle': '2026-01'}]), 'b1.json')
        with self.assertRaises(ValueError):
            self.apply(batch('B2', [
                {'component': 'db-shared', 'intervals': [[0, 600]]}],
                close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]), 'b2.json')
        # The rejected batch must not leave its fact replacement behind.
        report, _ = self.apply(batch('B3'), 'b3.json')
        self.assertEqual(report['seq'], 2)  # no seq was consumed by B2
        self.assertEqual(report['cycles'][0]['downtime_seconds'], 0)
        self.assertEqual(report['entries'], [
            {'seq': 1, 'contract': 'gold-2026', 'cycle': '2026-01',
             'kind': 'close', 'amount_cents': 0}])

    def test_topup_clawback_restore_converges_to_current_liability(self):
        self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}],
            close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]), 'b1.json')
        r2, _ = self.apply(batch('B2', [
            {'component': 'db-shared', 'intervals': [[0, 600]]}]), 'b2.json')
        self.assertEqual(r2['entries'][-1]['amount_cents'], 750000)
        r3, _ = self.apply(batch('B3', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}]), 'b3.json')
        self.assertEqual(r3['entries'][-1],
                         {'seq': 3, 'contract': 'gold-2026', 'cycle': '2026-01',
                          'kind': 'adjustment', 'amount_cents': -750000})
        r4, _ = self.apply(batch('B4', [
            {'component': 'db-shared', 'intervals': [[0, 600]]}]), 'b4.json')
        # Restore tops up exactly the previously clawed-back amount, and the
        # cumulative entries equal the current liability again.
        self.assertEqual(r4['entries'][-1]['amount_cents'], 750000)
        total = sum(e['amount_cents'] for e in r4['entries'])
        self.assertEqual(total, r4['cycles'][0]['liability_cents'])
        self.assertEqual(total, 1000000)

    def test_revoke_to_empty_produces_full_clawback(self):
        self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 600]]}],
            close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]), 'b1.json')
        r2, _ = self.apply(batch('B2', [
            {'component': 'db-shared', 'intervals': []}]), 'b2.json')
        self.assertEqual(r2['cycles'][0]['liability_cents'], 0)
        self.assertEqual(r2['entries'][-1]['amount_cents'], -1000000)
        self.assertEqual(r2['cycles'][0]['posted_cents'], 0)

    def test_zero_difference_adjustment_not_recorded(self):
        self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}],
            close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]), 'b1.json')
        # A batch that changes nothing relevant posts no adjustment.
        r2, _ = self.apply(batch('B2', [
            {'component': 'cache-a', 'intervals': [[0, 10]]}]), 'b2.json')
        self.assertEqual(len(r2['entries']), 1)  # still only the close entry
        self.assertEqual(r2['cycles'][0]['posted_cents'], 250000)

    def test_same_bytes_replay_returns_original_without_double_posting(self):
        path = self.write_batch('b1.json', batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}],
            close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]))
        out1 = self.dir / 'out1.json'
        first = apply_batch(path, self.db, out1)
        # Later batches intervene before the replay.
        self.apply(batch('B2', [
            {'component': 'db-shared', 'intervals': [[0, 600]]}]), 'b2.json')
        out2 = self.dir / 'out2.json'
        replayed = apply_batch(path, self.db, out2)
        self.assertEqual(replayed, first)
        self.assertEqual(replayed['seq'], 1)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        # The replay did not add entries: the current view shows exactly one
        # close and one adjustment.
        current = view_report(2, self.db, self.dir / 'view2.json')
        self.assertEqual([e['kind'] for e in current['entries']],
                         ['close', 'adjustment'])

    def test_conflicting_batch_id_rejected(self):
        doc = batch('B1', [{'component': 'db-shared', 'intervals': [[0, 120]]}])
        path = self.write_batch('b1.json', doc)
        apply_batch(path, self.db, self.dir / 'o1.json')
        # Same id, different content: rejected entirely.
        conflict = self.write_batch('b1b.json', batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 121]]}]))
        with self.assertRaises(ValueError):
            apply_batch(conflict, self.db, self.dir / 'o2.json')
        # Same id, same semantics but different bytes: also rejected.
        restyled = self.dir / 'b1c.json'
        restyled.write_text(json.dumps(doc, indent=2, sort_keys=True))
        with self.assertRaises(ValueError):
            apply_batch(restyled, self.db, self.dir / 'o3.json')
        # The identical bytes replay to the original report.
        replay = apply_batch(path, self.db, self.dir / 'o4.json')
        self.assertEqual(replay['seq'], 1)

    def test_historical_views_are_frozen(self):
        self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}],
            close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]), 'b1.json')
        self.apply(batch('B2', [
            {'component': 'db-shared', 'intervals': [[0, 600]]}]), 'b2.json')
        view1 = view_report(1, self.db, self.dir / 'v1.json')
        self.assertEqual(view1['cycles'][0]['downtime_seconds'], 120)
        self.assertEqual(view1['cycles'][0]['liability_cents'], 250000)
        self.assertEqual(len(view1['entries']), 1)
        view2 = view_report(2, self.db, self.dir / 'v2.json')
        self.assertEqual(view2['cycles'][0]['downtime_seconds'], 600)
        self.assertEqual(len(view2['entries']), 2)
        # Re-reading a view is byte stable.
        again = self.dir / 'v1b.json'
        view_report(1, self.db, again)
        self.assertEqual((self.dir / 'v1.json').read_bytes(), again.read_bytes())

    def test_facts_are_fully_replaced(self):
        self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 10], [20, 30]]}]), 'b1.json')
        self.apply(batch('B2', [
            {'component': 'db-shared', 'intervals': [[5, 6]]}]), 'b2.json')
        conn = sqlite3.connect(str(self.db))
        try:
            rows = conn.execute(
                'SELECT start_sec, end_sec FROM facts WHERE component=?',
                ('db-shared',)).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(5, 6)])
        # Empty array revokes all facts of the component.
        self.apply(batch('B3', [
            {'component': 'db-shared', 'intervals': []}]), 'b3.json')
        conn = sqlite3.connect(str(self.db))
        try:
            count = conn.execute(
                'SELECT COUNT(*) FROM facts WHERE component=?',
                ('db-shared',)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_outage_split_across_cycles_of_one_contract(self):
        config = diamond_config()
        config['contracts'] = [{
            'id': 'split', 'service': 'svc-root',
            'cycles': [
                {'id': 'p1', 'start': 0, 'end': 100, 'fee_cents': 1000},
                {'id': 'p2', 'start': 100, 'end': 200, 'fee_cents': 2000},
            ],
            'tiers': [{'after_seconds': 0, 'rate_bps': 0},
                      {'after_seconds': 50, 'rate_bps': 10000}],
        }]
        db = self.dir / 'split.db'
        cfg = self.dir / 'split.json'
        cfg.write_text(json.dumps(config))
        init_db(cfg, db)
        path = self.write_batch('s1.json', batch('S1', [
            {'component': 'db-shared', 'intervals': [[50, 150]]}],
            close=[{'contract': 'split', 'cycle': 'p1'},
                   {'contract': 'split', 'cycle': 'p2'}]))
        report = apply_batch(path, db, self.dir / 's1.out.json')
        rows = {row['cycle']: row for row in report['cycles']}
        # The [50,150) outage is cut exactly at the cycle boundary t=100.
        self.assertEqual(rows['p1']['outage_intervals'], [[50, 100]])
        self.assertEqual(rows['p1']['downtime_seconds'], 50)
        self.assertEqual(rows['p1']['liability_cents'], 1000)
        self.assertEqual(rows['p2']['outage_intervals'], [[100, 150]])
        self.assertEqual(rows['p2']['downtime_seconds'], 50)
        self.assertEqual(rows['p2']['liability_cents'], 2000)
        self.assertEqual(
            {(e['cycle'], e['amount_cents']) for e in report['entries']},
            {('p1', 1000), ('p2', 2000)})

    def test_invalid_batch_changes_nothing(self):
        self.apply(batch('B1', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}],
            close=[{'contract': 'gold-2026', 'cycle': '2026-01'}]), 'b1.json')
        for bad_doc in [
                batch('B2', [{'component': 'ghost', 'intervals': [[0, 1]]}]),
                batch('B2', [{'component': 'db-shared', 'intervals': [[5, 5]]}]),
                batch('B2', [], [{'contract': 'gold-2026', 'cycle': 'ghost'}]),
                {'batch_id': 'B2', 'replacements': []},
        ]:
            with self.assertRaises((ValueError, TypeError)):
                self.apply(bad_doc, 'bad.json')
        view = view_report(1, self.db, self.dir / 'v.json')
        self.assertEqual(view['cycles'][0]['downtime_seconds'], 120)
        conn = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0], 1)
        finally:
            conn.close()

    def test_init_rejects_existing_db_and_keeps_config_immutable(self):
        with self.assertRaises(ValueError):
            init_db(self.config_path, self.db)
        # A second init with a different config cannot rewrite the ledger.
        other = self.dir / 'other.json'
        changed = diamond_config()
        changed['contracts'][0]['cycles'][0]['fee_cents'] = 7
        other.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            init_db(other, self.db)

    def test_apply_and_view_require_existing_db(self):
        missing = self.dir / 'missing.db'
        path = self.write_batch('b.json', batch('B1'))
        with self.assertRaises(ValueError):
            apply_batch(path, missing, self.dir / 'o.json')
        with self.assertRaises(ValueError):
            view_report(1, missing, self.dir / 'o.json')

    def test_view_requires_committed_seq(self):
        with self.assertRaises(ValueError):
            view_report(1, self.db, self.dir / 'v.json')
        with self.assertRaises(ValueError):
            view_report(0, self.db, self.dir / 'v.json')

    def test_not_a_ledger_db_rejected(self):
        foreign = self.dir / 'foreign.db'
        foreign.write_text('{"not":"sqlite"}')
        path = self.write_batch('b.json', batch('B1'))
        with self.assertRaises(ValueError):
            apply_batch(path, foreign, self.dir / 'o.json')
        with self.assertRaises(ValueError):
            view_report(1, foreign, self.dir / 'o.json')

    def test_multiple_contracts_sorted_views_and_mixed_entries(self):
        config = diamond_config()
        config['contracts'] = [
            {'id': 'c-b', 'service': 'svc-read',
             'cycles': [{'id': 'y2', 'start': 0, 'end': 100, 'fee_cents': 1000},
                        {'id': 'y1', 'start': 100, 'end': 200, 'fee_cents': 2000}],
             'tiers': [{'after_seconds': 0, 'rate_bps': 0},
                       {'after_seconds': 10, 'rate_bps': 5000}]},
            {'id': 'c-a', 'service': 'svc-root',
             'cycles': [{'id': 'q', 'start': 0, 'end': 1000, 'fee_cents': 4000}],
             'tiers': [{'after_seconds': 0, 'rate_bps': 0},
                       {'after_seconds': 50, 'rate_bps': 2500}]},
        ]
        db = self.dir / 'multi.db'
        cfg = self.dir / 'multi.json'
        cfg.write_text(json.dumps(config))
        init_db(cfg, db)
        b1 = self.write_batch('m1.json', batch('M1', [
            {'component': 'db-shared', 'intervals': [[0, 120]]}],
            close=[{'contract': 'c-a', 'cycle': 'q'}]))
        first = apply_batch(b1, db, self.dir / 'm1.out.json')
        # cycles sorted by (contract, cycle): c-a/q, c-b/y1, c-b/y2.
        self.assertEqual([(r['contract'], r['cycle']) for r in first['cycles']],
                         [('c-a', 'q'), ('c-b', 'y1'), ('c-b', 'y2')])
        # c-a/q closed at D=120 -> 2500bps of 4000 = 1000.
        self.assertEqual(first['entries'], [
            {'seq': 1, 'contract': 'c-a', 'cycle': 'q',
             'kind': 'close', 'amount_cents': 1000}])
        # Batch 2 widens the outage but c-a/q stays on the same tier, so the
        # zero-difference adjustment is not recorded; c-b/y2 closes here.
        b2 = self.write_batch('m2.json', batch('M2', [
            {'component': 'db-shared', 'intervals': [[0, 160]]}],
            close=[{'contract': 'c-b', 'cycle': 'y2'}]))
        second = apply_batch(b2, db, self.dir / 'm2.out.json')
        self.assertEqual(second['entries'], [
            {'seq': 1, 'contract': 'c-a', 'cycle': 'q',
             'kind': 'close', 'amount_cents': 1000},
            {'seq': 2, 'contract': 'c-b', 'cycle': 'y2',
             'kind': 'close', 'amount_cents': 500}])
        # The unclosed c-b/y1 shows its clipped impact but posts nothing.
        y1 = second['cycles'][1]
        self.assertEqual((y1['contract'], y1['cycle']), ('c-b', 'y1'))
        self.assertEqual(y1['outage_intervals'], [[100, 160]])
        self.assertEqual(y1['downtime_seconds'], 60)
        self.assertEqual(y1['liability_cents'], 1000)
        self.assertEqual(y1['posted_cents'], 0)
        self.assertFalse(y1['closed'])


class CliAndRecovery(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)
        self.db = self.dir / 'ledger.db'
        self.config = ROOT / 'fixtures/credit_config.json'
        result = cli('--credit-init', self.config, '--db', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)

    def apply_fixture(self, name, output):
        return cli('--credit-apply', ROOT / 'fixtures' / name,
                   '--db', self.db, '--output', output)

    def test_fixture_walk_and_view_bytes(self):
        reports = []
        for i, expect in enumerate([250000, 1000000, 250000, 1000000], start=1):
            out = self.dir / ('r%d.json' % i)
            result = self.apply_fixture('credit_batch%d.json' % i, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_bytes())
            self.assertEqual(report['seq'], i)
            self.assertEqual(report['cycles'][0]['liability_cents'], expect)
            reports.append(out.read_bytes())
        # Every historical view reproduces the original report bytes.
        for i, expected_bytes in enumerate(reports, start=1):
            view = self.dir / ('v%d.json' % i)
            result = cli('--credit-view', i, '--db', self.db, '--output', view)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(view.read_bytes(), expected_bytes)

    def test_crash_between_commit_and_report_is_retriable(self):
        # The output path is an existing directory: the report write fails
        # with exit 2 after the batch has already committed.
        blocker = self.dir / 'blocked'
        blocker.mkdir()
        result = self.apply_fixture('credit_batch1.json', blocker)
        self.assertEqual(result.returncode, 2)
        # Same-id retry with a writable output recovers the original report.
        out = self.dir / 'recovered.json'
        result = self.apply_fixture('credit_batch1.json', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.read_bytes())
        self.assertEqual(report['seq'], 1)
        self.assertEqual(report['cycles'][0]['liability_cents'], 250000)
        # A following batch proves the first committed exactly once.
        out2 = self.dir / 'r2.json'
        result = self.apply_fixture('credit_batch2.json', out2)
        self.assertEqual(result.returncode, 0, result.stderr)
        report2 = json.loads(out2.read_bytes())
        self.assertEqual(report2['seq'], 2)
        closes = [e for e in report2['entries'] if e['kind'] == 'close']
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]['amount_cents'], 250000)

    def test_exit2_preserves_previous_report(self):
        out = self.dir / 'report.json'
        out.write_text('prior')
        bad = self.dir / 'bad.json'
        bad.write_text(json.dumps(batch('BX', [
            {'component': 'ghost', 'intervals': []}])))
        result = cli('--credit-apply', bad, '--db', self.db, '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_text(), 'prior')

    def test_modes_mutually_exclusive_and_flag_rules(self):
        out = self.dir / 'o.json'
        combos = [
            ['--source', str(ROOT / 'fixtures/input.json'),
             '--credit-view', '1', '--db', str(self.db), '--output', str(out)],
            ['--credit-init', str(self.config), '--credit-view', '1',
             '--db', str(self.db)],
            ['--credit-view', '1', '--db', str(self.db)],           # no output
            ['--credit-apply', str(ROOT / 'fixtures/credit_batch1.json'),
             '--db', str(self.db)],                                  # no output
            ['--credit-init', str(self.config), '--db', str(self.db),
             '--output', str(out)],                                  # output forbidden
            ['--source', str(ROOT / 'fixtures/input.json'),
             '--db', str(self.db), '--output', str(out)],            # db forbidden
            ['--credit-view', '1'],                                  # db required
        ]
        for args in combos:
            result = cli(*args)
            self.assertEqual(result.returncode, 2, args)

    def test_path_safety(self):
        out = self.dir / 'o.json'
        batch_path = ROOT / 'fixtures/credit_batch1.json'
        # batch == db, db == output, batch == output
        self.assertEqual(cli('--credit-apply', self.db, '--db', self.db,
                             '--output', out).returncode, 2)
        self.assertEqual(cli('--credit-apply', batch_path, '--db', out,
                             '--output', out).returncode, 2)
        self.assertEqual(cli('--credit-apply', batch_path, '--db', self.db,
                             '--output', batch_path).returncode, 2)
        # symlinked db rejected
        link = self.dir / 'link.db'
        link.symlink_to(self.db)
        self.assertEqual(cli('--credit-apply', batch_path, '--db', link,
                             '--output', out).returncode, 2)
        # symlinked output rejected
        real = self.dir / 'real.json'
        real.write_text('x')
        out_link = self.dir / 'outlink.json'
        out_link.symlink_to(real)
        self.assertEqual(cli('--credit-apply', batch_path, '--db', self.db,
                             '--output', out_link).returncode, 2)
        self.assertEqual(real.read_text(), 'x')
        # symlinked input rejected
        in_link = self.dir / 'inlink.json'
        in_link.symlink_to(batch_path)
        self.assertEqual(cli('--credit-apply', in_link, '--db', self.db,
                             '--output', out).returncode, 2)
        # init: config == db rejected, existing db rejected
        self.assertEqual(cli('--credit-init', self.config,
                             '--db', self.config).returncode, 2)
        self.assertEqual(cli('--credit-init', self.config,
                             '--db', self.db).returncode, 2)

    def test_view_does_not_allocate_seq(self):
        out = self.dir / 'r1.json'
        self.assertEqual(self.apply_fixture('credit_batch1.json', out).returncode, 0)
        view = self.dir / 'v1.json'
        self.assertEqual(cli('--credit-view', 1, '--db', self.db,
                             '--output', view).returncode, 0)
        out2 = self.dir / 'r2.json'
        self.assertEqual(self.apply_fixture('credit_batch2.json', out2).returncode, 0)
        self.assertEqual(json.loads(out2.read_bytes())['seq'], 2)

    def test_stdout_and_report_shape(self):
        out = self.dir / 'r1.json'
        result = self.apply_fixture('credit_batch1.json', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = json.loads(result.stdout)
        self.assertEqual(printed, json.loads(out.read_bytes()))
        self.assertEqual(printed['schema_version'], 3)
        self.assertEqual(set(printed), {'schema_version', 'seq', 'batch_id',
                                        'source_sha256', 'cycles', 'entries'})


class IntegrationScenario(unittest.TestCase):
    """The shared-component tier-crossing settlement walk from the contract."""

    def test_shared_correction_close_topup_clawback_restore(self):
        with tempfile.TemporaryDirectory() as td:
            dirpath = Path(td)
            db = dirpath / 'ledger.db'
            self.assertEqual(cli('--credit-init', ROOT / 'fixtures/credit_config.json',
                                 '--db', db).returncode, 0)
            views = []
            for i in range(1, 5):
                out = dirpath / ('r%d.json' % i)
                result = cli('--credit-apply',
                             ROOT / 'fixtures' / ('credit_batch%d.json' % i),
                             '--db', db, '--output', out)
                self.assertEqual(result.returncode, 0, result.stderr)
                views.append(json.loads(out.read_bytes()))

            # seq1: the shared component outage crosses the 100s tier once
            # (not twice via both sub-services) and closes at 250000.
            self.assertEqual(views[0]['cycles'][0]['outage_intervals'], [[0, 120]])
            self.assertEqual(views[0]['cycles'][0]['downtime_seconds'], 120)
            self.assertEqual(views[0]['cycles'][0]['posted_cents'], 250000)
            self.assertEqual([e['amount_cents'] for e in views[0]['entries']],
                             [250000])
            # seq2: the correction crosses the 500s tier; top-up 750000.
            self.assertEqual(views[1]['cycles'][0]['downtime_seconds'], 600)
            self.assertEqual(views[1]['entries'][-1]['amount_cents'], 750000)
            self.assertEqual(views[1]['cycles'][0]['posted_cents'], 1000000)
            # seq3: the correction is revoked; the clawback is exactly the
            # previous top-up, and cumulative postings equal current L.
            self.assertEqual(views[2]['entries'][-1]['amount_cents'], -750000)
            self.assertEqual(views[2]['cycles'][0]['posted_cents'], 250000)
            # seq4: restoring the fault tops up only the clawed-back amount.
            self.assertEqual(views[3]['entries'][-1]['amount_cents'], 750000)
            self.assertEqual(views[3]['cycles'][0]['posted_cents'], 1000000)
            amounts = [e['amount_cents'] for e in views[3]['entries']]
            self.assertEqual(amounts, [250000, 750000, -750000, 750000])
            self.assertEqual(sum(amounts), 1000000)
            # Historical seqs still show their own intervals, tier amounts
            # and cumulative entries after everything committed.
            for i, view in enumerate(views, start=1):
                reread = dirpath / ('again%d.json' % i)
                self.assertEqual(cli('--credit-view', i, '--db', db,
                                     '--output', reread).returncode, 0)
                self.assertEqual(json.loads(reread.read_bytes()), view)
            self.assertEqual(views[0]['cycles'][0]['downtime_seconds'], 120)
            self.assertEqual(len(views[0]['entries']), 1)


if __name__ == '__main__':
    unittest.main()
