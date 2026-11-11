import json
from corpus_gate.errors import identifier, integer, text, fail, canonical, digest
from corpus_gate import documents
from corpus_gate.dedup import clusters

STRATIFICATIONS = ('source', 'risk', 'source_risk')
ALLOCATIONS = ('proportional', 'equal', 'explicit')
NONINTERVIEW_REASONS = ('withdrawn', 'replaced', 'unreachable')


def risk_level(document):
    """Deterministic risk stratum derived from the frozen document's findings."""
    if document['state'] == 'rejected' or any(finding['severity'] == 'reject' for finding in document['findings']):
        return 'high'
    if document['findings']:
        return 'medium'
    return 'low'


def stratum_key(document, stratification):
    level = risk_level(document)
    if stratification == 'source':
        return document['source_id']
    if stratification == 'risk':
        return level
    if stratification == 'source_risk':
        return document['source_id'] + '|' + level
    fail('survey_stratification', 'Unknown stratification dimension')


def head_frame(db):
    """All current head documents; the frozen population at the current corpus revision."""
    rows = db.all('SELECT document_id FROM document_heads ORDER BY document_id')
    if len(rows) >= 10000:
        fail('survey_frame_capacity', 'Frozen population exceeds the in-memory survey limit')
    return [documents.get(db, row['document_id']) for row in rows]


def cluster_frame(db, run_id):
    """Cluster run members become the frame; strata follow each cluster's representative."""
    run = clusters.get(db, run_id)
    groups = clusters.groups(db, run_id)
    result = []
    for group in groups:
        representative = documents.get(db, group['representative'])
        members = [documents.get(db, document_id) for document_id in group['members']]
        result.append({'cluster_id': group['cluster_id'], 'representative': representative, 'members': members})
    return run, result


def allocate(population, total, mode, explicit):
    """Return selected count per stratum key (SRSWOR, never exceeding stratum size)."""
    keys = sorted(population)
    if not keys:
        fail('survey_frame_empty', 'Frozen population has no sampling units')
    if mode == 'explicit':
        if not isinstance(explicit, dict) or not explicit:
            fail('survey_allocation', 'Explicit allocation requires per-stratum sample sizes')
        unknown = sorted(set(explicit) - set(keys))
        if unknown:
            fail('survey_allocation', 'Allocation names unknown strata', strata=unknown)
        counts = {}
        for key in keys:
            value = explicit.get(key, 0)
            integer(value, 'allocation', 0, 1000000)
            counts[key] = min(value, len(population[key]))
        return counts
    if mode == 'equal':
        share = total // len(keys)
        counts = {key: min(share, len(population[key])) for key in keys}
        remaining = total - sum(counts.values())
        for key in keys:
            if remaining <= 0:
                break
            room = len(population[key]) - counts[key]
            added = min(room, remaining)
            counts[key] += added
            remaining -= added
        return counts
    # Proportional allocation with largest-remainder rounding, capped at stratum size.
    frame_size = sum(len(population[key]) for key in keys)
    target = {key: total * len(population[key]) / frame_size for key in keys}
    counts = {key: min(int(target[key]), len(population[key])) for key in keys}
    remainder = total - sum(counts.values())
    ranked = sorted(keys, key=lambda key: (-(target[key] - int(target[key])), key))
    for key in ranked:
        if remainder <= 0:
            break
        room = len(population[key]) - counts[key]
        added = min(room, remainder)
        counts[key] += added
        remainder -= added
    return counts


def ordered_units(units, survey_id, seed, key):
    return sorted(units, key=lambda unit: (digest([survey_id, seed, key, unit['unit_key']]), unit['unit_key']))


def design(db, survey_id, seed, total=20, stratification='source', allocation='proportional', explicit=None, unit_type='element', run_id=None):
    identifier(survey_id)
    identifier(seed)
    integer(total, 'total', 1, 1000000)
    if stratification not in STRATIFICATIONS:
        fail('survey_stratification', 'Unknown stratification dimension')
    if allocation not in ALLOCATIONS:
        fail('survey_allocation', 'Unknown allocation mode')
    if unit_type not in ('element', 'cluster'):
        fail('survey_unit_type', 'Unit type must be element or cluster')
    if unit_type == 'element' and run_id is not None:
        fail('survey_arguments', 'Element designs cannot reference a cluster run')
    if unit_type == 'cluster':
        identifier(run_id, 'run_id')
    with db.transaction():
        if db.one('SELECT survey_id FROM survey_designs WHERE survey_id=?', (survey_id,)):
            fail('survey_exists', 'Survey design identifiers are immutable')
        corpus_revision = db.revision()
        strata = {}
        all_units = []
        if unit_type == 'element':
            frame = head_frame(db)
            population = {}
            for document in frame:
                key = stratum_key(document, stratification)
                population.setdefault(key, []).append(document)
            counts = allocate(population, total, allocation, explicit)
            for key in sorted(population):
                units = population[key]
                n_h = len(units)
                sample_size = counts[key]
                probability = sample_size / n_h if n_h else 0.0
                ranked = ordered_units([
                    {'unit_key': unit['document_id'], 'cluster_id': None, 'document_id': unit['document_id'],
                     'frame_revision': unit['revision'], 'frame_version': unit['version'], 'cluster_size': 1,
                     'members': None}
                    for unit in units
                ], survey_id, seed, key)
                selected = {row['unit_key'] for row in ranked[:sample_size]}
                for row in ranked:
                    is_selected = row['unit_key'] in selected
                    all_units.append({**row, 'stratum': key, 'inclusion_probability': probability,
                                      'design_weight': (n_h / sample_size) if sample_size else None,
                                      'selected': is_selected})
                strata[key] = {'population_size': n_h, 'sample_size': sample_size,
                               'inclusion_probability': probability,
                               'design_weight': (n_h / sample_size) if sample_size else None}
        else:
            run, groups = cluster_frame(db, run_id)
            population = {}
            for group in groups:
                key = stratum_key(group['representative'], stratification)
                population.setdefault(key, []).append(group)
            counts = allocate(population, total, allocation, explicit)
            for key in sorted(population):
                groups_h = population[key]
                n_h = len(groups_h)
                sample_size = counts[key]
                probability = sample_size / n_h if n_h else 0.0
                ranked = ordered_units([
                    {'unit_key': group['cluster_id'], 'cluster_id': group['cluster_id'],
                     'document_id': group['representative']['document_id'],
                     'frame_revision': group['representative']['revision'],
                     'frame_version': group['representative']['version'],
                     'cluster_size': len(group['members']),
                     'members': canonical([
                         {'document_id': member['document_id'], 'version': member['version'],
                          'revision': member['revision'], 'state': member['state'],
                          'exact_hash': member['exact_hash']}
                         for member in sorted(group['members'], key=lambda item: item['document_id'])
                     ])}
                    for group in groups_h
                ], survey_id, seed, key)
                selected = {row['unit_key'] for row in ranked[:sample_size]}
                for row in ranked:
                    is_selected = row['unit_key'] in selected
                    all_units.append({**row, 'stratum': key, 'inclusion_probability': probability,
                                      'design_weight': (n_h / sample_size) if sample_size else None,
                                      'selected': is_selected})
                strata[key] = {'population_size': n_h, 'sample_size': sample_size,
                               'inclusion_probability': probability,
                               'design_weight': (n_h / sample_size) if sample_size else None,
                               'population_elements': sum(len(group['members']) for group in groups_h)}
        frame_size = len(all_units)
        selected_total = sum(1 for unit in all_units if unit['selected'])
        if selected_total == 0:
            fail('survey_sample_empty', 'Allocation produced no sampled units')
        definition = {
            'survey_id': survey_id, 'unit_type': unit_type, 'stratification': stratification,
            'allocation': allocation, 'explicit': explicit or {}, 'seed': seed, 'total': total,
            'corpus_revision': corpus_revision, 'run_id': run_id,
            'strata': {key: strata[key] for key in sorted(strata)},
        }
        checksum = digest(definition)
        db.execute('INSERT INTO survey_designs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (survey_id, unit_type, stratification, allocation, corpus_revision, run_id, seed,
                    frame_size, canonical({key: strata[key] for key in sorted(strata)}),
                    canonical(definition), checksum, db.clock()))
        for unit in all_units:
            db.execute('INSERT INTO survey_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (survey_id, unit['unit_key'], unit['stratum'], unit['cluster_id'], unit['document_id'],
                        unit['frame_revision'], unit['frame_version'], unit['cluster_size'],
                        unit['inclusion_probability'], unit['design_weight'], int(unit['selected']),
                        unit['members']))
        db.log('survey.designed', {'survey_id': survey_id, 'unit_type': unit_type,
                                   'stratification': stratification, 'allocation': allocation,
                                   'corpus_revision': corpus_revision, 'frame_size': frame_size,
                                   'selected': selected_total, 'digest': checksum})
        return get(db, survey_id)


def get(db, survey_id):
    identifier(survey_id)
    row = db.one('SELECT * FROM survey_designs WHERE survey_id=?', (survey_id,))
    if row is None:
        fail('survey_missing', 'Survey design does not exist')
    row['strata'] = json.loads(row['strata'])
    definition = json.loads(row['definition'])
    if digest(definition) != row['digest']:
        fail('survey_corrupt', 'Stored survey design digest differs')
    row['definition'] = definition
    row['frame_frozen'] = row['corpus_revision'] == db.revision()
    row['units'] = db.all(
        'SELECT unit_key,stratum,cluster_id,document_id,frame_revision,frame_version,cluster_size,'
        'inclusion_probability,design_weight,selected,members FROM survey_units WHERE survey_id=? '
        'ORDER BY stratum,unit_key', (survey_id,))
    for unit in row['units']:
        unit['selected'] = bool(unit['selected'])
        unit['members'] = json.loads(unit['members']) if unit['members'] else None
    return row


def _require_selected(db, survey_id, unit_key):
    unit = db.one('SELECT * FROM survey_units WHERE survey_id=? AND unit_key=?', (survey_id, unit_key))
    if unit is None:
        fail('survey_unit_missing', 'Sampling unit is not part of this survey design')
    if not unit['selected']:
        fail('survey_not_sampled', 'Only sampled units accept review responses')
    return unit


def _check_version(unit, document):
    if document['revision'] != unit['frame_revision'] or document['version'] != unit['frame_version'] or not document['is_head']:
        fail('survey_version_mismatch',
             'Review only accepts the exact document version frozen by the sample; record a noninterview instead',
             unit_key=unit['unit_key'], frame_revision=unit['frame_revision'],
             current_revision=document['revision'])


def respond(db, survey_id, unit_key, conforming, actor, reason):
    identifier(survey_id)
    identifier(unit_key)
    if type(conforming) is not bool:
        fail('survey_conforming', 'conforming must be boolean')
    identifier(actor)
    reason = text(reason, 'reason')
    with db.transaction():
        design_row = get(db, survey_id)
        unit = _require_selected(db, survey_id, unit_key)
        document = documents.get(db, unit['document_id'])
        _check_version(unit, document)
        existing = db.one('SELECT * FROM survey_responses WHERE survey_id=? AND unit_key=? AND document_id=?',
                          (survey_id, unit_key, unit['document_id']))
        if existing is not None:
            if existing['conforming'] is not None and bool(existing['conforming']) == conforming \
                    and existing['reason'] == reason and existing['actor'] == actor:
                return {'survey_id': survey_id, 'unit_key': unit_key, 'conforming': conforming,
                        'idempotent': True}
            fail('survey_response_conflict',
                 'A different review result already exists; withdraw or replace the unit via noninterview')
        if design_row['unit_type'] == 'cluster':
            member_rows = json.loads(unit['members'])
            for member in member_rows:
                current = documents.get(db, member['document_id'])
                if current['revision'] != member['revision'] or current['version'] != member['version'] \
                        or not current['is_head']:
                    fail('survey_version_mismatch',
                         'Review only accepts the exact document versions frozen by the cluster sample',
                         unit_key=unit_key, document_id=member['document_id'])
            for member in member_rows:
                db.execute('INSERT INTO survey_responses VALUES(?,?,?,?,?,?,?)',
                           (survey_id, unit_key, member['document_id'], int(conforming), reason, actor, db.clock()))
        else:
            db.execute('INSERT INTO survey_responses VALUES(?,?,?,?,?,?,?)',
                       (survey_id, unit_key, unit['document_id'], int(conforming), reason, actor, db.clock()))
        db.log('survey.responded', {'survey_id': survey_id, 'unit_key': unit_key,
                                    'conforming': conforming, 'actor': actor})
        return {'survey_id': survey_id, 'unit_key': unit_key, 'conforming': conforming, 'idempotent': False}


def noninterview(db, survey_id, unit_key, reason, actor):
    identifier(survey_id)
    identifier(unit_key)
    if reason not in NONINTERVIEW_REASONS:
        fail('survey_noninterview_reason', 'Reason must be withdrawn, replaced or unreachable')
    identifier(actor)
    with db.transaction():
        get(db, survey_id)
        unit = _require_selected(db, survey_id, unit_key)
        existing = db.one('SELECT * FROM survey_responses WHERE survey_id=? AND unit_key=? AND document_id=?',
                          (survey_id, unit_key, unit_key))
        if existing is not None:
            if existing['conforming'] is None and existing['reason'] == reason and existing['actor'] == actor:
                return {'survey_id': survey_id, 'unit_key': unit_key, 'noninterview': True,
                        'reason': reason, 'idempotent': True}
            fail('survey_response_conflict', 'Unit already carries an incompatible response record')
        db.execute('INSERT INTO survey_responses VALUES(?,?,?,?,?,?,?)',
                   (survey_id, unit_key, unit_key, None, reason, actor, db.clock()))
        db.log('survey.noninterview', {'survey_id': survey_id, 'unit_key': unit_key,
                                       'reason': reason, 'actor': actor})
        return {'survey_id': survey_id, 'unit_key': unit_key, 'noninterview': True,
                'reason': reason, 'idempotent': False}


def unit_status(db, survey_id, unit):
    """Resolve a selected unit's status against the frozen frame and stored responses."""
    rows = db.all('SELECT document_id,conforming,reason,actor,recorded_at FROM survey_responses '
                  'WHERE survey_id=? AND unit_key=? ORDER BY document_id', (survey_id, unit['unit_key']))
    explicit_noninterview = next((row for row in rows if row['conforming'] is None), None)
    if explicit_noninterview:
        return {'status': 'noninterview', 'reason': explicit_noninterview['reason'],
                'actor': explicit_noninterview['actor'], 'recorded_at': explicit_noninterview['recorded_at'],
                'responses': rows}
    verdicts = [row for row in rows if row['conforming'] is not None]
    live_reason = _live_noninterview(db, unit)
    if live_reason:
        return {'status': 'noninterview', 'reason': live_reason, 'actor': None, 'recorded_at': None,
                'responses': rows, 'detected': True}
    if unit['members']:
        expected = {member['document_id'] for member in unit['members']}
    else:
        expected = {unit['document_id']}
    answered = {row['document_id'] for row in verdicts}
    if answered == expected:
        return {'status': 'responded', 'reason': None, 'actor': None, 'recorded_at': None,
                'responses': rows}
    if not answered:
        return {'status': 'pending', 'reason': None, 'actor': None, 'recorded_at': None, 'responses': rows}
    return {'status': 'partial', 'reason': None, 'actor': None, 'recorded_at': None, 'responses': rows}


def _live_noninterview(db, unit):
    """Withdrawn or replaced units are noninterviews, never pass/fail counts."""
    document = documents.get(db, unit['document_id'])
    if document['state'] == 'withdrawn':
        return 'withdrawn'
    if not document['is_head'] or document['revision'] != unit['frame_revision'] \
            or document['version'] != unit['frame_version']:
        return 'replaced'
    if unit['members']:
        for member in unit['members']:
            current = documents.get(db, member['document_id'])
            if current['state'] == 'withdrawn':
                return 'withdrawn'
            if not current['is_head'] or current['revision'] != member['revision'] \
                    or current['version'] != member['version']:
                return 'replaced'
    return None


def status(db, survey_id):
    get(db, survey_id)
    units = db.all('SELECT * FROM survey_units WHERE survey_id=? AND selected=1 ORDER BY stratum,unit_key',
                   (survey_id,))
    results = []
    for unit in units:
        unit['members'] = json.loads(unit['members']) if unit['members'] else None
        resolved = unit_status(db, survey_id, unit)
        results.append({'unit_key': unit['unit_key'], 'stratum': unit['stratum'],
                        'cluster_id': unit['cluster_id'], 'document_id': unit['document_id'],
                        **{key: value for key, value in resolved.items() if key != 'responses'},
                        'responses': resolved['responses']})
    counts = {'pending': 0, 'partial': 0, 'responded': 0, 'noninterview': 0}
    for row in results:
        counts[row['status']] += 1
    return {'survey_id': survey_id, 'counts': counts, 'units': results}
