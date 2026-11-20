"""Reproduce operational boundaries through real temporary database operations."""
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from strataleaf import Database
from .workflows import seed


def contention(folder):
    with Database(folder / 'observe.db', timeout=0.03) as db:
        seed(db)
        holder = sqlite3.connect(folder / 'observe.db', isolation_level=None)
        try:
            holder.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            try:
                db.batch().delete('segments', 'seg_00').commit()
            except sqlite3.OperationalError as exc:
                blocked = {'exception': type(exc).__name__, 'message': str(exc),
                           'elapsed_ms': round((time.monotonic() - started) * 1000, 2)}
            else:
                raise RuntimeError('expected SQLite writer contention was not observed')
        finally:
            holder.rollback()
            holder.close()
        result = db.batch().delete('segments', 'seg_00').commit()
        return {'blocked_write': blocked, 'after_release': result.__dict__, 'inspection': db.diagnostics.inspect(True)}


def retention_pressure(folder):
    with Database(folder / 'observe.db') as db:
        first = seed(db)
        db.retention.pin('long_reader', first)
        db.feed.register('offline_replica', first)
        for value in range(3):
            db.batch().put('segments', 'seg_00', {'region': 'east', 'sequence': value + 10}).commit()
        both = db.retention.compact(db.revision)
        db.retention.unpin('long_reader')
        one = db.retention.compact(db.revision)
        db.feed.acknowledge('offline_replica', db.revision)
        none = db.retention.compact(db.revision)
        return {'both_holds': both, 'one_hold': one, 'released': none, 'inspection': db.diagnostics.inspect(True)}


def numeric_modes(folder):
    """Show how the same historical rows behave under each numeric mode."""
    with Database(folder / 'observe.db') as db:
        seed(db)
        db.catalog.create('weights', {'fields': {'bucket': 'string'}})
        db.batch().put('weights', 'w1', {'bucket': 'rounding', 'value': 1e16}).put(
            'weights', 'w2', {'bucket': 'rounding', 'value': 1.0}).put(
            'weights', 'w3', {'bucket': 'rounding', 'value': -1e16}).put(
            'weights', 'w4', {'bucket': 'big', 'value': 10**30}).put(
            'weights', 'w5', {'bucket': 'big', 'value': 1}).put(
            'weights', 'w6', {'bucket': 'mixed', 'value': True}).put(
            'weights', 'w7', {'bucket': 'mixed'}).put(
            'weights', 'w8', {'bucket': 'sparse'}).commit()
        revision = db.revision
        observations = {}
        for mode in ('native', 'float', 'exact'):
            result = db.aggregates.group('weights', 'bucket', 'value', revision=revision, numeric=mode)
            observations[mode] = {
                'summary': result['summary'],
                'groups': {group['key']: {name: group[name] for name in
                                          ('sum', 'average', 'min', 'max', 'numeric_count', 'invalid_measure_count')}
                           for group in result['groups']},
                'reasons': sorted({(error['group'], error['reason']) for error in result['errors']}),
            }
        return {'revision': revision, 'modes': observations,
                'head_unchanged': db.revision == revision}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='strataleaf-observation-') as directory:
        operation = {'contention': contention, 'retention_pressure': retention_pressure,
                     'numeric_modes': numeric_modes}[sys.argv[1]]
        print(json.dumps(operation(Path(directory)), ensure_ascii=False, sort_keys=True))
