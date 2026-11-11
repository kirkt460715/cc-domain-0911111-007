from corpus_gate.errors import identifier, fail
from corpus_gate.review import surveys


def _fpc(n_h, N_h):
    """Finite population correction factor (1 - n/N), bounded at zero."""
    if N_h <= 1:
        return 0.0
    return max(0.0, 1.0 - n_h / N_h)


def _verdicts(db, survey_id, unit_key):
    rows = db.all('SELECT document_id,conforming FROM survey_responses WHERE survey_id=? AND unit_key=?',
                  (survey_id, unit_key))
    return {row['document_id']: (0 if row['conforming'] else 1) for row in rows
            if row['conforming'] is not None}


def _grouped(db, design):
    selected = [unit for unit in design['units'] if unit['selected']]
    groups = {key: [] for key in sorted(design['strata'])}
    for unit in selected:
        groups.setdefault(unit['stratum'], []).append((unit, surveys.unit_status(db, design['survey_id'], unit)))
    return groups


def _element_stratum(db, survey_id, stratum, info, unit_states):
    N_h = info['population_size']
    n_h = info['sample_size']
    responding = [(unit, state) for unit, state in unit_states if state['status'] == 'responded']
    r_h = len(responding)
    noninterviews = sum(1 for _, state in unit_states if state['status'] == 'noninterview')
    pending = sum(1 for _, state in unit_states if state['status'] in ('pending', 'partial'))
    base = {'stratum': stratum, 'population_size': N_h, 'sample_size': n_h, 'responded': r_h,
            'noninterviews': noninterviews, 'pending': pending,
            'inclusion_probability': info['inclusion_probability'],
            'design_weight': info['design_weight'],
            'sampling_fraction': (n_h / N_h if N_h else None), 'fpc_factor': _fpc(n_h, N_h)}
    if n_h == 0:
        return {**base, 'estimable': False, 'reason': 'unsampled_stratum'}
    if r_h == 0:
        return {**base, 'estimable': False, 'reason': 'no_observed_units'}
    if r_h == 1:
        indicator = next(iter(_verdicts(db, survey_id, responding[0][0]['unit_key']).values()))
        rate_h = float(indicator)
        return {**base, 'estimable': False, 'reason': 'single_sampling_unit',
                'observed_nonconforming_rate': rate_h, 'observed_nonconforming': indicator}
    indicators = [next(iter(_verdicts(db, survey_id, unit['unit_key']).values())) for unit, _ in responding]
    sample_rate = sum(indicators) / r_h
    sample_variance = sum((value - sample_rate) ** 2 for value in indicators) / (r_h - 1)
    # Noninterviews shrink the responding sample; the estimator assumes missingness is
    # ignorable within stratum and the variance below uses the observed r_h.
    variance_rate = (N_h / n_h) ** 2 * _fpc(n_h, N_h) * sample_variance / r_h
    estimated_total = N_h * sample_rate
    variance_total = N_h ** 2 * _fpc(n_h, N_h) * sample_variance / r_h
    return {**base, 'estimable': True, 'observed_nonconforming_rate': sample_rate,
            'estimated_nonconforming_rate': sample_rate,
            'rate_variance': variance_rate, 'rate_standard_error': variance_rate ** 0.5,
            'population_size_estimate': float(N_h),
            'estimated_nonconforming_total': estimated_total,
            'total_variance': variance_total, 'total_standard_error': variance_total ** 0.5}


def _cluster_stratum(db, survey_id, stratum, info, unit_states):
    N_h = info['population_size']
    n_h = info['sample_size']
    M_h = info.get('population_elements')
    responding = [(unit, state) for unit, state in unit_states if state['status'] == 'responded']
    r_h = len(responding)
    noninterviews = sum(1 for _, state in unit_states if state['status'] == 'noninterview')
    pending = sum(1 for _, state in unit_states if state['status'] in ('pending', 'partial'))
    base = {'stratum': stratum, 'population_clusters': N_h, 'sample_clusters': n_h,
            'responded_clusters': r_h, 'noninterviews': noninterviews, 'pending': pending,
            'inclusion_probability': info['inclusion_probability'],
            'design_weight': info['design_weight'],
            'sampling_fraction': (n_h / N_h if N_h else None), 'fpc_factor': _fpc(n_h, N_h)}
    if n_h == 0:
        return {**base, 'estimable': False, 'reason': 'unsampled_stratum'}
    if r_h == 0:
        return {**base, 'estimable': False, 'reason': 'no_observed_units'}
    if r_h == 1:
        unit, _ = responding[0]
        verdicts = _verdicts(db, survey_id, unit['unit_key'])
        y_i = sum(verdicts.values())
        return {**base, 'estimable': False, 'reason': 'single_sampling_unit',
                'observed_cluster_nonconforming': y_i, 'cluster_size': unit['cluster_size']}
    clusters = []
    for unit, _ in responding:
        verdicts = _verdicts(db, survey_id, unit['unit_key'])
        clusters.append((sum(verdicts.values()), unit['cluster_size']))
    y_bar = sum(y_i for y_i, _ in clusters) / r_h
    m_bar = sum(m_i for _, m_i in clusters) / r_h
    s_y2 = sum((y_i - y_bar) ** 2 for y_i, _ in clusters) / (r_h - 1)
    s_m2 = sum((m_i - m_bar) ** 2 for _, m_i in clusters) / (r_h - 1)
    s_ym = sum((y_i - y_bar) * (m_i - m_bar) for y_i, m_i in clusters) / (r_h - 1)
    total_y = N_h * y_bar
    total_m = N_h * m_bar
    rate = y_bar / m_bar if m_bar else None
    variance_y = N_h ** 2 * _fpc(n_h, N_h) * s_y2 / r_h
    variance_m = N_h ** 2 * _fpc(n_h, N_h) * s_m2 / r_h
    # Combined-ratio variance for the cluster nonconforming rate.
    if m_bar:
        variance_rate = _fpc(n_h, N_h) / r_h * (
            s_y2 - 2 * rate * s_ym + rate ** 2 * s_m2) / m_bar ** 2
    else:
        variance_rate = None
    return {**base, 'estimable': True, 'mean_cluster_nonconforming': y_bar,
            'mean_cluster_size': m_bar, 'observed_nonconforming_rate': rate,
            'estimated_nonconforming_rate': rate,
            'rate_variance': variance_rate,
            'rate_standard_error': (variance_rate ** 0.5 if variance_rate is not None else None),
            'population_elements': M_h, 'population_elements_estimate': total_m,
            'estimated_nonconforming_total': total_y,
            'total_variance': variance_y, 'total_standard_error': variance_y ** 0.5,
            'cluster_size_variance': variance_m}


def estimate(db, survey_id):
    identifier(survey_id)
    design = surveys.get(db, survey_id)
    groups = _grouped(db, design)
    rows = []
    for stratum in sorted(design['strata']):
        info = design['strata'][stratum]
        unit_states = groups.get(stratum, [])
        if design['unit_type'] == 'cluster':
            rows.append(_cluster_stratum(db, survey_id, stratum, info, unit_states))
        else:
            rows.append(_element_stratum(db, survey_id, stratum, info, unit_states))
    estimable = [row for row in rows if row['estimable']]
    not_estimable = [{'stratum': row['stratum'], 'reason': row['reason']} for row in rows if not row['estimable']]
    total_variance = sum(row['total_variance'] for row in estimable)
    estimated_total = sum(row['estimated_nonconforming_total'] for row in estimable)
    rate_variances = [row['rate_variance'] for row in estimable if row['rate_variance'] is not None]
    rate_variance = sum(rate_variances) if rate_variances else None
    if design['unit_type'] == 'cluster':
        population_size = sum(row.get('population_elements_estimate', 0.0) for row in estimable)
        frozen_population = sum(info.get('population_elements', 0) for info in design['strata'].values())
        frame_label = 'population_elements'
    else:
        population_size = float(sum(row['population_size'] for row in estimable))
        frozen_population = sum(info['population_size'] for info in design['strata'].values())
        frame_label = 'population_size'
    overall = None
    if estimable and population_size:
        overall = {'estimable': True,
                   'estimated_nonconforming_total': estimated_total,
                   'total_variance': total_variance,
                   'total_standard_error': total_variance ** 0.5,
                   'estimated_nonconforming_rate': estimated_total / population_size,
                   'rate_variance': rate_variance,
                   'rate_standard_error': (rate_variance ** 0.5 if rate_variance is not None else None),
                   'estimated_frame_base': population_size}
    else:
        overall = {'estimable': False, 'reason': 'no_estimable_strata'}
    status = surveys.status(db, survey_id)
    return {
        'survey_id': survey_id,
        'unit_type': design['unit_type'],
        'stratification': design['stratification'],
        'allocation': design['allocation_mode'],
        'seed': design['seed'],
        'frozen_corpus_revision': design['corpus_revision'],
        'frame_frozen': design['frame_frozen'],
        'run_id': design['run_id'],
        'design_digest': design['digest'],
        'frame_size': design['frame_size'],
        'frozen_population_base': frozen_population,
        'frame_base_label': frame_label,
        'response_counts': status['counts'],
        'overall': overall,
        'not_estimable': not_estimable,
        'strata': rows,
        'methodology': {
            'estimator': 'stratified_design_weighted' if design['unit_type'] == 'element'
            else 'stratified_single_stage_cluster_ratio',
            'weights': 'inverse inclusion probability N_h/n_h frozen at design time',
            'variance': 'stratum SRSWOR variance' if design['unit_type'] == 'element'
            else 'between-cluster variance on sampled clusters',
            'fpc': 'finite population correction (1 - n_h/N_h) applied per stratum',
            'noninterview': 'withdrawn, replaced or unreachable units are excluded from conforming '
                            'and nonconforming counts; within-stratum nonresponse assumed ignorable',
            'recomputation': 'all figures derive from the frozen frame and stored responses only',
        },
    }


def _frame_size(report, stratum):
    row = next(item for item in report['strata'] if item['stratum'] == stratum)
    return row.get('population_elements', row['population_size'])


def _frame_total(report):
    if report['unit_type'] == 'cluster':
        return sum(row.get('population_elements', 0) for row in report['strata'])
    return sum(row['population_size'] for row in report['strata'])


def decompose_reports(before, after):
    """Pure Kitagawa-style decomposition of two stored estimation reports.

    Structure component = frozen population mix change; quality component =
    within-stratum nonconforming rate change. Runs on report payloads so reports
    produced against different frozen populations remain comparable.
    """
    if before['unit_type'] != after['unit_type'] or before['stratification'] != after['stratification']:
        fail('survey_compare_strata', 'Reports must share unit type and stratification')
    before_rows = {row['stratum']: row for row in before['strata']}
    after_rows = {row['stratum']: row for row in after['strata']}
    keys = sorted(set(before_rows) | set(after_rows))
    table = []
    for key in keys:
        left = before_rows.get(key)
        right = after_rows.get(key)
        table.append({
            'stratum': key,
            'before_population_size': _frame_size(before, key) if left else 0,
            'after_population_size': _frame_size(after, key) if right else 0,
            'before_rate': left.get('estimated_nonconforming_rate') if left else None,
            'after_rate': right.get('estimated_nonconforming_rate') if right else None,
            'before_estimable': bool(left and left['estimable']),
            'after_estimable': bool(right and right['estimable']),
        })
    shared = [row for row in table if row['before_estimable'] and row['after_estimable']]
    not_estimable = sorted({row['stratum'] for row in table
                            if not (row['before_estimable'] and row['after_estimable'])})
    decomposition = None
    if before['overall'].get('estimable') and after['overall'].get('estimable') and shared:
        total_before = _frame_total(before)
        total_after = _frame_total(after)
        weights_before = {row['stratum']: row['before_population_size'] / total_before for row in shared}
        weights_after = {row['stratum']: row['after_population_size'] / total_after for row in shared}
        rates_before = {row['stratum']: row['before_rate'] for row in shared}
        rates_after = {row['stratum']: row['after_rate'] for row in shared}
        quality_component = sum(
            weights_before[key] * (rates_after[key] - rates_before[key]) for key in weights_before)
        structure_component = sum(
            (weights_after[key] - weights_before[key]) * rates_after[key] for key in weights_before)
        decomposition = {
            'scope': 'estimable shared strata',
            'rate_difference': after['overall']['estimated_nonconforming_rate']
            - before['overall']['estimated_nonconforming_rate'],
            'within_stratum_quality_component': quality_component,
            'population_structure_component': structure_component,
            'components_sum_on_shared_scope': quality_component + structure_component,
            'stratum_table': table,
            'not_estimable': not_estimable,
            'note': 'structure component reflects frozen population mix changes; quality component '
                    'reflects within-stratum nonconforming rate changes',
        }
    return {'before': {'survey_id': before['survey_id'],
                       'frozen_corpus_revision': before['frozen_corpus_revision'],
                       'overall': before['overall']},
            'after': {'survey_id': after['survey_id'],
                      'frozen_corpus_revision': after['frozen_corpus_revision'],
                      'overall': after['overall']},
            'stratum_table': table,
            'decomposition': decomposition,
            'not_estimable': not_estimable}


def compare(db, before_id, after_id):
    identifier(before_id)
    identifier(after_id)
    before = estimate(db, before_id)
    after = estimate(db, after_id)
    return decompose_reports(before, after)
