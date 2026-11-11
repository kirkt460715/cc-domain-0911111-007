import math
from tests.support import Case, record
from corpus_gate.review import surveys, estimation
from corpus_gate import documents, batch
from corpus_gate.ingest import sources
from corpus_gate.dedup import clusters


def clean(identity):
    return record(identity, 'alpha beta gamma delta epsilon zeta eta theta')


def short(identity):
    return record(identity, 'short')


def empty(identity):
    return record(identity, '')


class SurveyElementTests(Case):
    def second_source(self, source_id='source-b', priority=10):
        sources.create(self.db, source_id, _authorization(), priority)
        return source_id

    def ingest_into(self, source, records, batch_id):
        batch.stage(self.db, batch_id, source, 'default', records)
        batch.run(self.db, batch_id)

    def build_two_sources(self):
        b = self.second_source()
        self.ingest_into('source', [clean('a1'), clean('a2'), short('a3'), short('a4')], 'batch-a')
        self.ingest_into(b, [clean('b1'), short('b2'), empty('b3')], 'batch-b')
        return b

    def test_design_freezes_frame_strata_probabilities_and_weights(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'wave', 'seed', total=4, stratification='source', allocation='equal')
        self.assertEqual(design['corpus_revision'], self.db.revision())
        self.assertTrue(design['frame_frozen'])
        strata = design['strata']
        self.assertEqual(strata['source']['population_size'], 4)
        self.assertEqual(strata['source-b']['population_size'], 3)
        # equal allocation, capped at stratum size: 2 + 2
        self.assertEqual(strata['source']['sample_size'], 2)
        self.assertEqual(strata['source-b']['sample_size'], 2)
        self.assertAlmostEqual(strata['source']['inclusion_probability'], 0.5)
        self.assertAlmostEqual(strata['source']['design_weight'], 2.0)
        self.assertAlmostEqual(strata['source-b']['inclusion_probability'], 2 / 3)
        self.assertAlmostEqual(strata['source-b']['design_weight'], 1.5)
        selected = [unit for unit in design['units'] if unit['selected']]
        self.assertEqual(len(selected), 4)
        for unit in selected:
            self.assertEqual(unit['design_weight'], strata[unit['stratum']]['design_weight'])
            self.assertEqual(unit['inclusion_probability'], strata[unit['stratum']]['inclusion_probability'])
        # Deterministic given seed, and identifier is immutable.
        again = surveys.get(self.db, 'wave')
        self.assertEqual(again['digest'], design['digest'])
        self.fails('survey_exists', surveys.design, self.db, 'wave', 'seed', 4)

    def test_proportional_default_allocation_and_source_risk_strata(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'wave', 'seed', total=7)
        # Default stratification is source, default allocation proportional.
        self.assertEqual(design['stratification'], 'source')
        self.assertEqual(design['allocation_mode'], 'proportional')
        total_allocated = sum(value['sample_size'] for value in design['strata'].values())
        self.assertEqual(total_allocated, 7)
        # Larger stratum receives at least as many slots.
        sizes = {key: value['sample_size'] for key, value in design['strata'].items()}
        self.assertGreaterEqual(sizes['source'], sizes['source-b'])
        composite = surveys.design(self.db, 'composite', 'seed', 4, 'source_risk', 'equal')
        for key in composite['strata']:
            self.assertIn('|', key)

    def test_explicit_allocation_and_risk_stratification(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'risk-wave', 'seed', total=7, stratification='risk',
                                allocation='explicit', explicit={'high': 1, 'medium': 2, 'low': 4})
        sizes = {key: value['sample_size'] for key, value in design['strata'].items()}
        # high has one rejected doc, medium the quarantined, low the clean ones (capped at frame).
        self.assertEqual(design['strata']['high']['population_size'], 1)
        self.assertEqual(sizes['high'], 1)
        self.assertEqual(sizes['medium'], 2)
        self.assertEqual(design['strata']['low']['population_size'], 3)
        self.assertEqual(sizes['low'], 3)  # capped
        self.assertEqual(sum(sizes.values()), 6)
        keys = {unit['stratum'] for unit in design['units']}
        self.assertEqual(keys, {'high', 'medium', 'low'})

    def test_review_only_accepts_frozen_version_and_is_idempotent(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'wave', 'seed', 4, 'source', 'equal')
        unit = next(unit for unit in design['units'] if unit['selected'])
        surveys.respond(self.db, 'wave', unit['unit_key'], False, 'reviewer', 'finding confirmed')
        repeated = surveys.respond(self.db, 'wave', unit['unit_key'], False, 'reviewer', 'finding confirmed')
        self.assertTrue(repeated['idempotent'])
        self.fails('survey_response_conflict', surveys.respond, self.db, 'wave', unit['unit_key'],
                   True, 'reviewer', 'changed mind')
        # A non-sampled unit cannot receive a response.
        not_sampled = next(unit for unit in design['units'] if not unit['selected'])
        self.fails('survey_not_sampled', surveys.respond, self.db, 'wave', not_sampled['unit_key'],
                   True, 'reviewer', 'x')

    def test_replaced_version_is_noninterview_not_conforming(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'wave', 'seed', 4, 'source', 'equal')
        # Pick whatever sampled unit we will supersede below.
        target = next(unit for unit in design['units'] if unit['selected'])
        frozen_doc = documents.get(self.db, target['document_id'])
        external = frozen_doc['external_id']
        # Responding to the frozen version first is allowed.
        surveys.respond(self.db, 'wave', target['unit_key'], True, 'reviewer', 'ok')
        # Then a corrected version supersedes the frozen document.
        batch.stage(self.db, 'corrected', frozen_doc['source_id'], 'default',
                    [record(external, version=2)])
        batch.run(self.db, 'corrected')
        self.assertFalse(documents.get(self.db, target['document_id'])['is_head'])
        # A new response against the stale frozen version is rejected.
        self.fails('survey_version_mismatch', surveys.respond, self.db, 'wave', target['unit_key'],
                   True, 'reviewer', 'late')
        # Explicit noninterview recording is idempotent and conflict-checked.
        other = next(unit for unit in design['units'] if unit['selected'] and unit['unit_key'] != target['unit_key'])
        surveys.noninterview(self.db, 'wave', other['unit_key'], 'withdrawn', 'reviewer')
        again = surveys.noninterview(self.db, 'wave', other['unit_key'], 'withdrawn', 'reviewer')
        self.assertTrue(again['idempotent'])
        self.fails('survey_noninterview_reason', surveys.noninterview, self.db, 'wave',
                   other['unit_key'], 'bogus', 'reviewer')
        status = surveys.status(self.db, 'wave')
        reasons = {row['unit_key']: row['status'] for row in status['units']}
        self.assertEqual(reasons[target['unit_key']], 'noninterview')
        target_row = next(row for row in status['units'] if row['unit_key'] == target['unit_key'])
        self.assertEqual(target_row['reason'], 'replaced')
        self.assertTrue(target_row.get('detected'))

    def test_withdrawn_sample_detected_as_noninterview(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'wave', 'seed', 4, 'source', 'equal')
        target = next(unit for unit in design['units'] if unit['selected'])
        doc = documents.get(self.db, target['document_id'])
        documents.withdraw(self.db, target['document_id'], doc['revision'], 'pulled')
        status = surveys.status(self.db, 'wave')
        row = next(row for row in status['units'] if row['unit_key'] == target['unit_key'])
        self.assertEqual(row['status'], 'noninterview')
        self.assertEqual(row['reason'], 'withdrawn')

    def test_pending_units_never_mix_into_conforming_counts(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'wave', 'seed', 4, 'source', 'equal')
        selected = [unit for unit in design['units'] if unit['selected']]
        surveys.respond(self.db, 'wave', selected[0]['unit_key'], False, 'reviewer', 'bad')
        report = estimation.estimate(self.db, 'wave')
        # One stratum still has pending work; its point estimate excludes nothing by counting
        # pending as conforming. The pending count is surfaced explicitly.
        self.assertEqual(report['response_counts']['pending'] + report['response_counts']['responded'], 4)
        for row in report['strata']:
            if row['responded'] == 0:
                self.assertFalse(row['estimable'])
                self.assertEqual(row['reason'], 'no_observed_units')

    def test_element_estimator_weights_fpc_and_standard_error(self):
        # Four clean documents form one risk stratum; sample two, both observed.
        self.ingest([clean('d1'), clean('d2'), clean('d3'), clean('d4')])
        design = surveys.design(self.db, 'wave', 'seed', 2, 'risk', 'explicit',
                                explicit={'low': 2})
        selected = [unit for unit in design['units'] if unit['selected']]
        verdicts = iter([True, False])
        for unit in selected:
            surveys.respond(self.db, 'wave', unit['unit_key'], next(verdicts), 'reviewer', 'checked')
        report = estimation.estimate(self.db, 'wave')
        overall = report['overall']
        self.assertTrue(overall['estimable'])
        row = next(row for row in report['strata'] if row['estimable'])
        N_h, n_h = row['population_size'], row['sample_size']
        self.assertEqual(n_h, 2)
        self.assertAlmostEqual(row['observed_nonconforming_rate'], 0.5)
        self.assertAlmostEqual(row['estimated_nonconforming_total'], N_h * 0.5)
        expected_fpc = max(0.0, 1 - n_h / N_h)
        self.assertAlmostEqual(row['fpc_factor'], expected_fpc)
        s2 = 0.5  # indicators {0,1}
        expected_rate_var = (N_h / n_h) ** 2 * expected_fpc * s2 / 2
        self.assertAlmostEqual(row['rate_variance'], expected_rate_var)
        self.assertAlmostEqual(row['rate_standard_error'], math.sqrt(expected_rate_var))
        self.assertAlmostEqual(overall['rate_standard_error'], math.sqrt(expected_rate_var))

    def test_census_has_zero_variance_from_fpc(self):
        self.ingest([clean('d1'), short('d2'), short('d3')])
        design = surveys.design(self.db, 'wave', 'seed', 3, 'risk', 'equal')
        for unit in [u for u in design['units'] if u['selected']]:
            surveys.respond(self.db, 'wave', unit['unit_key'], False, 'reviewer', 'bad')
        report = estimation.estimate(self.db, 'wave')
        for row in report['strata']:
            if row['estimable']:
                self.assertEqual(row['fpc_factor'], 0.0)
                self.assertEqual(row['rate_variance'], 0.0)
                self.assertEqual(row['rate_standard_error'], 0.0)

    def test_single_sampling_unit_is_flagged_not_estimated(self):
        self.ingest([empty('only-high'), clean('c1'), clean('c2')])
        design = surveys.design(self.db, 'wave', 'seed', 3, 'risk', 'equal')
        for unit in [u for u in design['units'] if u['selected']]:
            surveys.respond(self.db, 'wave', unit['unit_key'], False, 'reviewer', 'bad')
        report = estimation.estimate(self.db, 'wave')
        high = next(row for row in report['strata'] if row['stratum'] == 'high')
        self.assertFalse(high['estimable'])
        self.assertEqual(high['reason'], 'single_sampling_unit')
        self.assertIn('high', [item['stratum'] for item in report['not_estimable']])

    def test_report_recomputes_against_frozen_frame_after_corpus_changes(self):
        self.build_two_sources()
        design = surveys.design(self.db, 'wave', 'seed', 4, 'source', 'equal')
        for unit in [u for u in design['units'] if u['selected']]:
            surveys.respond(self.db, 'wave', unit['unit_key'], True, 'reviewer', 'ok')
        first = estimation.estimate(self.db, 'wave')
        # Corpus evolves after the freeze; the stored design still recomputes identically.
        self.ingest_into('source-b', [clean('b4')], 'later-batch')
        self.assertFalse(surveys.get(self.db, 'wave')['frame_frozen'])
        second = estimation.estimate(self.db, 'wave')
        self.assertEqual(second['frozen_corpus_revision'], first['frozen_corpus_revision'])
        self.assertEqual(second['overall']['estimated_nonconforming_rate'],
                         first['overall']['estimated_nonconforming_rate'])
        self.assertEqual(second['overall']['rate_variance'], first['overall']['rate_variance'])


class SurveyClusterTests(Case):
    def test_cluster_sampling_uses_clusters_as_units_and_cluster_variance(self):
        # Near-duplicate accepted texts form multi-document clusters.
        records = [
            record('a', 'alpha beta gamma delta epsilon zeta eta theta'),
            record('b', 'alpha beta gamma delta epsilon zeta eta iota'),
            record('c', 'alpha beta gamma delta epsilon zeta eta kappa'),
            record('d', 'completely different quiet river valley morning stone path'),
            record('e', 'completely different quiet river valley evening stone road'),
        ]
        self.ingest(records)
        run = clusters.run(self.db, 'run', numerator=2, denominator=5)
        self.assertLess(len(clusters.groups(self.db, 'run')), len(records))
        design = surveys.design(self.db, 'wave', 'seed', total=2, stratification='source',
                                allocation='equal', unit_type='cluster', run_id='run')
        self.assertEqual(design['unit_type'], 'cluster')
        for unit in design['units']:
            self.assertIsNotNone(unit['cluster_id'])
            if unit['selected']:
                self.assertGreaterEqual(unit['cluster_size'], 1)
        selected = [u for u in design['units'] if u['selected']]
        # Need at least two responding clusters for an estimable stratum.
        self.assertGreaterEqual(len(selected), 2)
        for unit in selected:
            surveys.respond(self.db, 'wave', unit['unit_key'], False, 'reviewer', 'cluster fail')
        report = estimation.estimate(self.db, 'wave')
        self.assertEqual(report['unit_type'], 'cluster')
        row = next(row for row in report['strata'] if row['estimable'])
        self.assertIn('mean_cluster_size', row)
        self.assertGreaterEqual(row['rate_variance'], 0.0)
        self.assertAlmostEqual(row['fpc_factor'], max(0.0, 1 - row['sample_clusters'] / row['population_clusters']))
        self.assertEqual(report['methodology']['variance'], 'between-cluster variance on sampled clusters')

    def test_cluster_version_pinning_and_partial_status(self):
        records = [
            record('a', 'alpha beta gamma delta epsilon zeta eta theta'),
            record('b', 'alpha beta gamma delta epsilon zeta eta iota'),
            record('c', 'totally separate content about weather and distant mountains today'),
        ]
        self.ingest(records)
        clusters.run(self.db, 'run', numerator=2, denominator=5)
        design = surveys.design(self.db, 'wave', 'seed', 2, 'source', 'equal',
                                unit_type='cluster', run_id='run')
        multi = next(u for u in design['units'] if u['selected'] and u['cluster_size'] > 1)
        # A member replaced after freezing blocks the whole-cluster response.
        member = multi['members'][0]['document_id']
        doc = documents.get(self.db, member)
        external = doc['external_id']
        batch.stage(self.db, 'fix', 'source', 'default', [record(external, version=2)])
        batch.run(self.db, 'fix')
        self.fails('survey_version_mismatch', surveys.respond, self.db, 'wave',
                   multi['unit_key'], True, 'reviewer', 'late')


class SurveyComparisonTests(Case):
    def _build_app(self):
        from corpus_gate.service import CorpusGate
        from corpus_gate.text import profiles
        app = CorpusGate(clock=lambda: self.time)
        self.addCleanup(app.close)
        sources.create(app.db, 'source', _authorization())
        sources.create(app.db, 'source-b', _authorization(), 10)
        profiles.put(app.db, 'default', 0, {})
        return app

    def _wave(self, app, wave_id, records_a, records_b):
        batch.stage(app.db, wave_id + '-a', 'source', 'default', records_a)
        batch.run(app.db, wave_id + '-a')
        batch.stage(app.db, wave_id + '-b', 'source-b', 'default', records_b)
        batch.run(app.db, wave_id + '-b')
        design = surveys.design(app.db, wave_id, 'seed', 100, 'source', 'equal')
        for unit in [u for u in design['units'] if u['selected']]:
            conforming = documents.get(app.db, unit['document_id'])['state'] == 'accepted'
            surveys.respond(app.db, wave_id, unit['unit_key'], conforming, 'reviewer', 'ok')
        return design

    def test_structure_change_is_separated_from_within_stratum_quality_change(self):
        # Two independently frozen populations, each with its own frozen frame.
        # Within-stratum rates differ between strata, so a shift in the population mix
        # moves the overall rate even though no within-stratum rate changes.
        first_app = self._build_app()
        # source: 2 of 4 nonconforming (0.5); source-b: 1 of 4 nonconforming (0.25).
        self._wave(first_app, 'wave-1',
                   [clean('a1'), clean('a2'), short('a3'), short('a4')],
                   [clean('b1'), clean('b2'), clean('b3'), short('b4')])
        second_app = self._build_app()
        # Same per-stratum rates, but source-b doubles its population share.
        self._wave(second_app, 'wave-2',
                   [clean('a1'), clean('a2'), short('a3'), short('a4')],
                   [clean('b1'), clean('b2'), clean('b3'), clean('b4'),
                    clean('b5'), clean('b6'), short('b7'), short('b8')])
        first_report = estimation.estimate(first_app.db, 'wave-1')
        second_report = estimation.estimate(second_app.db, 'wave-2')
        # Per-stratum rates are identical across waves.
        first_rates = {row['stratum']: row['estimated_nonconforming_rate']
                       for row in first_report['strata'] if row['estimable']}
        second_rates = {row['stratum']: row['estimated_nonconforming_rate']
                        for row in second_report['strata'] if row['estimable']}
        self.assertEqual(first_rates, second_rates)
        # The overall rate still moves because the mix shifted toward the cleaner source.
        self.assertNotAlmostEqual(first_report['overall']['estimated_nonconforming_rate'],
                                  second_report['overall']['estimated_nonconforming_rate'])
        from corpus_gate.review.estimation import decompose_reports
        result = decompose_reports(first_report, second_report)
        decomposition = result['decomposition']
        self.assertIsNotNone(decomposition)
        # No within-stratum quality moved, so the quality component is exactly zero.
        self.assertEqual(decomposition['within_stratum_quality_component'], 0.0)
        # The entire movement is attributable to the population structure change.
        self.assertNotAlmostEqual(decomposition['population_structure_component'], 0.0)
        self.assertAlmostEqual(decomposition['rate_difference'],
                               decomposition['population_structure_component'])
        row_b = next(row for row in result['stratum_table'] if row['stratum'] == 'source-b')
        self.assertEqual(row_b['before_population_size'], 4)
        self.assertEqual(row_b['after_population_size'], 8)

    def test_compare_operation_on_evolving_corpus(self):
        sources.create(self.db, 'source-b', _authorization(), 10)
        batch.stage(self.db, 'w1-a', 'source', 'default', [clean('a1'), short('a2')])
        batch.run(self.db, 'w1-a')
        batch.stage(self.db, 'w1-b', 'source-b', 'default', [clean('b1'), short('b2')])
        batch.run(self.db, 'w1-b')
        first = surveys.design(self.db, 'wave-1', 'seed', 100, 'source', 'equal')
        for unit in [u for u in first['units'] if u['selected']]:
            conforming = documents.get(self.db, unit['document_id'])['state'] == 'accepted'
            surveys.respond(self.db, 'wave-1', unit['unit_key'], conforming, 'reviewer', 'ok')
        # Corpus structure evolves before the second freeze.
        batch.stage(self.db, 'w2-b', 'source-b', 'default',
                    [clean('b3'), clean('b4'), short('b5'), short('b6')])
        batch.run(self.db, 'w2-b')
        second = surveys.design(self.db, 'wave-2', 'seed', 100, 'source', 'equal')
        for unit in [u for u in second['units'] if u['selected']]:
            conforming = documents.get(self.db, unit['document_id'])['state'] == 'accepted'
            surveys.respond(self.db, 'wave-2', unit['unit_key'], conforming, 'reviewer', 'ok')
        result = estimation.compare(self.db, 'wave-1', 'wave-2')
        self.assertIsNotNone(result['decomposition'])
        self.assertEqual(result['before']['survey_id'], 'wave-1')
        self.assertEqual(result['after']['survey_id'], 'wave-2')
        self.assertNotEqual(result['before']['frozen_corpus_revision'],
                            result['after']['frozen_corpus_revision'])


class SurveyValidationTests(Case):
    def test_operations_registered_and_validated(self):
        self.ingest([clean('d1'), short('d2')])
        registry = self.app.operations()
        for operation in ('survey.design', 'survey.get', 'survey.respond', 'survey.noninterview',
                          'survey.status', 'survey.estimate', 'survey.compare'):
            self.assertIn(operation, registry)
        design = self.app.dispatch('survey.design', {'survey_id': 'wave', 'seed': 'seed',
                                                     'total': 2, 'stratification': 'source'})
        self.assertEqual(design['stratification'], 'source')
        self.fails('survey_missing', surveys.get, self.db, 'unknown')
        self.fails('survey_stratification', surveys.design, self.db, 'bad-dim', 'seed', 2, 'bogus')
        self.fails('survey_allocation', surveys.design, self.db, 'bad-alloc', 'seed', 2,
                   'source', 'mystery')
        self.fails('survey_allocation', surveys.design, self.db, 'bad-explicit', 'seed', 2,
                   'source', 'explicit', {'ghost-stratum': 1})

    def test_cluster_design_requires_run_and_element_rejects_run(self):
        self.ingest([clean('d1')])
        self.fails('cluster_missing', surveys.design, self.db, 'cluster-wave', 'seed', 1,
                   'source', 'equal', None, 'cluster', 'missing-run')
        self.fails('survey_arguments', surveys.design, self.db, 'element-wave', 'seed', 1,
                   'source', 'equal', None, 'element', 'run-x')


def _authorization():
    return {'purposes': ['research'], 'regions': ['cn', 'eu'],
            'valid_from': '2026-01-01T00:00:00Z', 'valid_until': '2027-01-01T00:00:00Z',
            'obligations': ['attribution'], 'allow_export': True}
