import threading
import datetime
from Queue import Queue

from ichnaea.db import Measure, RADIO_TYPE
from ichnaea.renderer import dump_decimal_json, loads_decimal_json
from ichnaea.db import MeasureDB


_LOCALS = threading.local()
_LOCALS.dbs = {}
_BATCH_SIZE = 100
_MAX_AGE = datetime.timedelta(seconds=600)
_LOCK = threading.RLock()


class TimedQueue(Queue):
    """A Queue with an age for the first item
    """
    def __init__(self, maxsize=0):
        Queue.__init__(self, maxsize)
        self._first_put_time = None

    @property
    def age(self):
        if self._first_put_time is None:
            return datetime.timedelta(seconds=0)
        return datetime.datetime.utcnow() - self._first_put_time

    def put(self, item, block=True, timeout=None):
        # first item
        if self.empty():
            self._first_put_time = datetime.datetime.utcnow()
        return Queue.put(self, item, block=block, timeout=timeout)

    def get(self, block=True, timeout=None):
        res = Queue.get(self, block=block, timeout=timeout)
        if self.empty():
            self._first_put_time = None
        return res


_BATCH = TimedQueue(maxsize=_BATCH_SIZE)


def add_measures(request):
    """Adds measures in a queue and dump them to the database when
    a batch is ready.

    In async mode the batch is pushed in redis.
    """
    # options
    settings = request.registry.settings
    batch_size = int(settings.get('batch_size', _BATCH_SIZE))
    batch_age = settings.get('batch_age')
    if batch_age is None:
        batch_age = _MAX_AGE
    else:
        batch_age = datetime.timedelta(seconds=batch_age)

    # data
    measures = [dump_decimal_json(measure)
                for measure in request.validated['items']]

    if batch_size != -1:
        # we are batching in memory
        for measure in measures:
            _BATCH.put(measure)

        # using a lock so only on thread gets to empty the queue
        with _LOCK:
            current_size = _BATCH.qsize()
            batch_ready = _BATCH.age > batch_age or current_size >= batch_size

            if not batch_ready:
                return

            measures = [_BATCH.get() for i in range(current_size)]

    if request.registry.settings.get('async'):
        return push_measures(request, measures)

    return _add_measures(measures, db_instance=request.measuredb)


def _get_db(sqluri):
    if sqluri not in _LOCALS.dbs:
        _LOCALS.dbs[sqluri] = MeasureDB(sqluri)
    return _LOCALS.dbs[sqluri]


def _process_wifi(values):
    # convert frequency into channel numbers
    result = []
    for entry in values:
        # always remove frequency
        freq = entry.pop('frequency')
        # if no explicit channel was given, calculate
        if freq and not entry['channel']:
            if 2411 < freq < 2473:
                # 2.4 GHz band
                entry['channel'] = (freq - 2407) // 5
            elif 5169 < freq < 5826:
                # 5 GHz band
                entry['channel'] = (freq - 5000) // 5
        result.append(entry)
    return result


def _add_measures(measures, db_instance=None, sqluri=None):

    if db_instance is None:
        db_instance = _get_db(sqluri)

    session = db_instance.session()

    for data in measures:
        if isinstance(data, basestring):
            data = loads_decimal_json(data)
        measure = Measure()
        measure.lat = int(data['lat'] * 1000000)
        measure.lon = int(data['lon'] * 1000000)
        measure.accuracy = data['accuracy']
        measure.altitude = data['altitude']
        measure.altitude_accuracy = data['altitude_accuracy']
        if data.get('cell'):
            measure.radio = RADIO_TYPE.get(data['radio'], 0)
            measure.cell = dump_decimal_json(data['cell'])
        if data.get('wifi'):
            measure.wifi = dump_decimal_json(_process_wifi(data['wifi']))
        session.add(measure)

    session.commit()


def push_measures(request, measures):
    request.queue.enqueue('ichnaea.worker:_add_measures', measures=measures,
                          sqluri=request.measuredb.sqluri)