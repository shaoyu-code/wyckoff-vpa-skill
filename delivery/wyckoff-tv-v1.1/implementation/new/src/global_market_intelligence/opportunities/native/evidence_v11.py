"""v1.1 price/volume evidence primitives. Arithmetic is not a phase classifier.

The existing source/calendar qualifier remains authoritative for source eligibility.
This module checks the complete normalized window against that qualified input,
measures explicitly selected spans, and retains available-at times. It never invents
data, assigns a probability from a score, or upgrades a candidate into a confirmed event.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import math

from ..contracts import digest
from .tradingview import NativeError

FIELDS = ('open', 'high', 'low', 'close', 'volume')
VERSION = 'tv11-evidence-1'


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise NativeError('EVIDENCE_TIME_MUST_BE_STRING')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise NativeError('INVALID_EVIDENCE_TIME') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise NativeError('EVIDENCE_TIMEZONE_REQUIRED')
    return result.astimezone(timezone.utc)


def finite(value, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise NativeError('NONFINITE_EVIDENCE:' + name)
    return float(value)


def audit_window(dataset: dict, *, cutoff: str | None = None) -> dict:
    """Check every qualified input, not just the most recent bar or swing anchors.

    `native_by_open` comes from the upstream qualification. Rechecking it here catches
    later corrupt conversions, missing middle bars and duplicate/pagination mistakes.
    Calendar eligibility is NOT inferred from this arithmetic audit.
    """
    bars = dataset.get('bars')
    source = dataset.get('native_by_open')
    if not isinstance(bars, list) or not bars or not isinstance(source, dict):
        raise NativeError('COMPLETE_QUALIFIED_WINDOW_REQUIRED')
    as_of = instant(cutoff or dataset.get('qualification_as_of') or dataset['captured_at'])
    if dataset.get('native_capture') is not None:
        from ..contracts import Instrument
        from .data import qualify_capture
        requalified = qualify_capture(dataset['native_capture'],Instrument(**dataset['instrument']),
                         dataset['qualification_profile'],
                         as_of=dataset.get('qualification_as_of') or dataset['captured_at'])
        if requalified['bars'] != bars or requalified['native_by_open'] != source:
            raise NativeError('QUALIFIED_WINDOW_NOT_BOUND_TO_CAPTURE')
    previous_close = None
    keys = []
    available = []
    for i, bar in enumerate(bars):
        opened, closed = instant(bar['opened_at']), instant(bar['closed_at'])
        if closed <= opened or (previous_close is not None and opened < previous_close):
            raise NativeError('WINDOW_TIME_ORDER_OR_OVERLAP')
        previous_close = closed
        # Delayed observations/publications cannot be backdated to the bar close.
        times = [closed]
        for name in ('available_at', 'published_at', 'observed_at'):
            if bar.get(name) is not None:
                times.append(instant(bar[name]))
        known = max(times)
        if known > as_of:
            raise NativeError('EVIDENCE_NOT_AVAILABLE_AT_CUTOFF')
        values = {k: finite(bar.get(k), k) for k in FIELDS}
        if values['volume'] < 0:
            raise NativeError('NEGATIVE_TRADED_VOLUME')
        if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
            raise NativeError('IMPOSSIBLE_OHLC')
        key = bar['opened_at']
        if key in keys or key not in source:
            raise NativeError('WINDOW_DUPLICATE_OR_UNBOUND_BAR')
        raw = source[key]
        raw_time = finite(raw.get('time'), 'time')
        if raw_time != int(raw_time):
            raise NativeError('NONINTEGRAL_RAW_TIME')
        if raw_time != opened.timestamp():
            # Some exchange bars are labelled by session date rather than open time.
            # Reuse the actual qualifier; do not guess a UTC or six-bars-per-day rule.
            from ..contracts import Instrument
            from .data import bar_interval
            if not dataset.get('qualification_profile'):
                raise NativeError('RAW_NORMALIZED_TIME_MISMATCH')
            op,cl,_ = bar_interval(int(raw_time),dataset['timeframe'],
                                  Instrument(**dataset['instrument']),dataset['qualification_profile'])
            if op != opened or cl != closed:
                raise NativeError('RAW_NORMALIZED_TIME_MISMATCH')
        if any(finite(raw.get(k), 'raw.' + k) != values[k] for k in FIELDS):
            raise NativeError('RAW_NORMALIZED_VALUE_MISMATCH:' + str(i))
        keys.append(key)
        available.append(known.isoformat())
    if set(keys) != set(source):
        raise NativeError('WINDOW_COVERAGE_DIFFERENCE')
    return {'version': VERSION, 'rows': len(bars), 'first': bars[0]['opened_at'],
            'last_close': bars[-1]['closed_at'], 'available_at': max(available),
            'input_sha256': digest(bars), 'source_sha256': digest(source),
            'calendar_validation': 'REQUIRES_EXISTING_SOURCE_QUALIFIER',
            'scope': 'ALL_NORMALIZED_ROWS_BOUND_TO_QUALIFIED_SOURCE'}


def measure_span(bars: list[dict], *, start: int, end: int,
                 start_field: str = 'close', end_field: str = 'close',
                 cutoff: str, ongoing: bool = False) -> dict:
    """Inclusive selected span; explicit endpoint fields and no right-side pivot fill."""
    if type(start) is not int or type(end) is not int or not 0 <= start < end < len(bars):
        raise NativeError('WAVE_BOUNDS_INVALID')
    if start_field not in FIELDS[:-1] or end_field not in FIELDS[:-1]:
        raise NativeError('WAVE_PRICE_FIELD_INVALID')
    if type(ongoing) is not bool:
        raise NativeError('WAVE_COMPLETION_FLAG_REQUIRED')
    selected = bars[start:end+1]
    as_of = instant(cutoff)
    previous_close = None
    durations, volumes, known_times = [], [], []
    for b in selected:
        opened, closed = instant(b['opened_at']), instant(b['closed_at'])
        if closed <= opened or (previous_close is not None and opened < previous_close):
            raise NativeError('WAVE_TIME_ORDER_OR_OVERLAP')
        previous_close = closed
        available = max([closed] + [instant(b[k]) for k in ('available_at', 'published_at', 'observed_at') if b.get(k)])
        if available > as_of:
            raise NativeError('WAVE_FUTURE_DEPENDENCY')
        known_times.append(available)
        durations.append((closed-opened).total_seconds())
        values = {k: finite(b.get(k), k) for k in FIELDS}
        if values['volume'] < 0 or not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
            raise NativeError('WAVE_VALUE_INVALID')
        volumes.append(values['volume'])
    first = float(selected[0][start_field])
    last = float(selected[-1][end_field])
    delta = last-first
    duration = math.fsum(durations)
    total = math.fsum(volumes)
    return {
        'start_index': start, 'end_index': end, 'start_time': selected[0]['opened_at'],
        'end_time': selected[-1]['opened_at'], 'closed_through': selected[-1]['closed_at'],
        'start_price_field': start_field, 'end_price_field': end_field,
        'start_price': first, 'end_price': last,
        'direction': 'UP' if delta > 0 else ('DOWN' if delta < 0 else 'FLAT'),
        'bar_count': len(selected), 'endpoint_inclusion': 'BOTH',
        'net_change': delta, 'absolute_progress': abs(delta),
        'price_span': max(b['high'] for b in selected)-min(b['low'] for b in selected),
        'return_fraction': delta/first if first > 0 else None,
        'volume_total': total, 'volume_per_bar': total/len(selected),
        'trading_seconds': duration, 'volume_per_trading_second': total/duration,
        'elapsed_seconds': (instant(selected[-1]['closed_at'])-instant(selected[0]['opened_at'])).total_seconds(),
        'price_progress_per_trading_second': abs(delta)/duration,
        'ongoing': ongoing, 'selected_at': as_of.isoformat(),
        'data_available_at': max(known_times).isoformat(),
        'endpoint_status': 'ANALYST_SELECTED_SPAN_NOT_AUTOMATIC_PIVOT_CONFIRMATION',
        'input_sha256': digest(selected),
    }


def compare_spans(reference: dict, current: dict) -> dict:
    """Ratios are descriptive; they never become probabilities or automatic labels."""
    if reference['start_price_field'] != current['start_price_field'] or reference['end_price_field'] != current['end_price_field']:
        raise NativeError('INCOMPARABLE_WAVE_ENDPOINT_BASIS')
    def ratio(field):
        denominator = reference[field]
        return current[field]/denominator if denominator > 0 else None
    flags = []
    if reference['ongoing'] or current['ongoing']:
        flags.append('INCOMPLETE_SPAN_NO_FINAL_CONTRACTION_CLAIM')
    if reference['trading_seconds'] != current['trading_seconds']:
        flags.append('UNEQUAL_TRADING_DURATION_COMPARE_RATE_ALSO')
    if reference['direction'] != current['direction']:
        flags.append('OPPOSITE_DIRECTION_COMPARISON')
    if max(reference['start_index'], current['start_index']) <= min(reference['end_index'], current['end_index']):
        flags.append('OVERLAPPING_SPANS_NOT_INDEPENDENT')
    return {'volume_ratio': ratio('volume_total'),
            'volume_rate_ratio': ratio('volume_per_trading_second'),
            'progress_ratio': ratio('absolute_progress'),
            'range_ratio': ratio('price_span'),
            'duration_ratio': ratio('trading_seconds'),
            'flags': flags, 'probability': None,
            'interpretation': 'MEASUREMENT_ONLY_REQUIRES_BACKGROUND_AND_FOLLOW_THROUGH'}


def reviewed_comparisons(value: dict, dataset: dict) -> list[dict]:
    """Compute model-selected comparisons; the model is not trusted with numbers."""
    requests = value.get('wave_comparisons', [])
    if not isinstance(requests, list) or len(requests) > 3:
        raise NativeError('WAVE_COMPARISON_LIMIT')
    result, identifiers = [], set()
    cutoff = dataset.get('qualification_as_of') or dataset['captured_at']
    for item in requests:
        key = item['id']
        if not key or key in identifiers or not item.get('basis'):
            raise NativeError('WAVE_COMPARISON_ID_OR_BASIS_INVALID')
        identifiers.add(key)
        measured = {}
        for role in ('reference', 'current'):
            span = item[role]
            measured[role] = measure_span(dataset['bars'], start=span['start_index'],
                end=span['end_index'], start_field=span['start_price_field'],
                end_field=span['end_price_field'], ongoing=span['ongoing'], cutoff=cutoff)
        result.append({'id': key, **measured,
                       'comparison': compare_spans(measured['reference'], measured['current']),
                       'basis': item['basis']})
    return result


def freeze_research(store, dataset: dict, review: dict, manifest: dict, *, at: str) -> dict:
    """Persist the EXACT dataset/review/plan before any native market-object write."""
    audit = audit_window(dataset)
    if manifest['data_cutoff'] != dataset['bars'][-1]['closed_at']:
        raise NativeError('MANIFEST_CUTOFF_NOT_BOUND')
    if manifest['timeframe'] != dataset['timeframe'] or manifest['product_id'] != dataset['instrument']['product_id']:
        raise NativeError('MANIFEST_PRODUCT_OR_TIMEFRAME_NOT_BOUND')
    packet = {'schema_version': 'gmi-tv11-frozen-research-v1', 'dataset': dataset,
              'review': review, 'manifest': deepcopy(manifest), 'window_audit': audit}
    key = store.snapshot(packet)
    if digest(store.read_snapshot(key)) != digest(packet):
        raise NativeError('FROZEN_RESEARCH_READBACK_FAILED')
    subject = 'tv11-frozen:' + key
    existing = next((e for e in store.events() if e['subject'] == subject and e['kind'] == 'TV11_RESEARCH_FROZEN'), None)
    if existing is None:
        store.append('TV11_RESEARCH_FROZEN', subject, {'snapshot': key, 'data_cutoff': manifest['data_cutoff']},
            at=at, idempotency_key=subject, snapshot_sha256=key)
    return {**deepcopy(manifest), 'frozen_research_sha256': key, 'evidence_contract_version': VERSION}


def verify_frozen_manifest(store, manifest: dict) -> None:
    if not manifest.get('evidence_contract_version'):
        return  # Imported legacy manifests remain explicitly outside v1.1 certification.
    key = manifest.get('frozen_research_sha256', '')
    packet = store.read_snapshot(key)
    original = {k: v for k, v in manifest.items()
                if k not in {'frozen_research_sha256', 'evidence_contract_version'}}
    if packet.get('schema_version') != 'gmi-tv11-frozen-research-v1' or digest(packet['manifest']) != digest(original):
        raise NativeError('FROZEN_MANIFEST_CHANGED')
    if audit_window(packet['dataset']) != packet['window_audit']:
        raise NativeError('FROZEN_WINDOW_CHANGED')
    if not any(e['kind'] == 'TV11_RESEARCH_FROZEN' and e['subject'] == 'tv11-frozen:' + key
               and e.get('snapshot_sha256') == key for e in store.events()):
        raise NativeError('FROZEN_RESEARCH_LEDGER_MISSING')


def main_context_records(records: list[dict]) -> list[dict]:
    """Allow-list main-only records; unknown or P&F-derived records stay out."""
    allowed = {'MAIN_RESEARCH', 'MAIN_REVIEW', 'VERIFIED_BOOK_RULE'}
    return [deepcopy(r) for r in records
            if r.get('record_type') in allowed and r.get('lineage_scope') == 'MAIN_ONLY'
            and r.get('contains_sidecar_targets') is False]
