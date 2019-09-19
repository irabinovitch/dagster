import glob
import os
import sqlite3
from abc import ABCMeta, abstractmethod
from collections import defaultdict
from contextlib import contextmanager

import gevent.lock
import pyrsistent
import six
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

from dagster import check, seven
from dagster.core.errors import DagsterError
from dagster.core.events.log import EventRecord
from dagster.core.serdes import deserialize_json_to_dagster_namedtuple, serialize_dagster_namedtuple
from dagster.utils import mkdir_p

from .pipeline_run import PipelineRunStatus


class EventLogInvalidForRun(DagsterError):
    def __init__(self, *args, **kwargs):
        self.run_id = check.str_param(kwargs.pop('run_id'), 'run_id')
        super(EventLogInvalidForRun, self).__init__(*args, **kwargs)


class EventLogSequence(pyrsistent.CheckedPVector):
    __type__ = EventRecord


class EventLogStorage(six.with_metaclass(ABCMeta)):  # pylint: disable=no-init
    @abstractmethod
    def get_logs_for_run(self, run_id, cursor=-1):
        '''Get all of the logs corresponding to a run.

        Args:
            run_id (str): The id of the run for which to fetch logs.
            cursor (Optional[int]): Zero-indexed logs will be returned starting from cursor + 1,
                i.e., if cursor is -1, all logs will be returned. (default: -1)
        '''

    @abstractmethod
    def store_event(self, event):
        '''Store an event corresponding to a pipeline run.

        Args:
            run_id (str): The id of the run that generated the event.
            event (EventRecord): The event to store.
        '''

    @property
    @abstractmethod
    def is_persistent(self):
        '''(bool) Whether the log storage persists after the process that
        created it dies.'''

    @abstractmethod
    def wipe(self):
        '''Clear the log storage.'''


class WatchableEventLogStorage(EventLogStorage):
    @abstractmethod
    def watch(self, run_id, start_cursor, callback):
        '''Call this method to start watching.'''

    @abstractmethod
    def end_watch(self, run_id, handler):
        '''Call this method to stop watching.'''


class InMemoryEventLogStorage(EventLogStorage):
    def __init__(self):
        self._logs = defaultdict(EventLogSequence)
        self._lock = defaultdict(gevent.lock.Semaphore)

    def get_logs_for_run(self, run_id, cursor=-1):
        check.str_param(run_id, 'run_id')
        check.int_param(cursor, 'cursor')
        check.invariant(
            cursor >= -1,
            'Don\'t know what to do with negative cursor {cursor}'.format(cursor=cursor),
        )

        cursor = cursor + 1
        with self._lock[run_id]:
            return self._logs[run_id][cursor:]

    @property
    def is_persistent(self):
        return False

    def store_event(self, event):
        check.inst_param(event, 'event', EventRecord)
        run_id = event.run_id
        with self._lock[run_id]:
            self._logs[run_id] = self._logs[run_id].append(event)

    def wipe(self):
        self._logs = defaultdict(EventLogSequence)
        self._lock = defaultdict(gevent.lock.Semaphore)


CREATE_EVENT_LOG_SQL = '''
CREATE TABLE IF NOT EXISTS event_logs (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT
)
'''

FETCH_EVENTS_SQL = '''
SELECT event FROM event_logs WHERE row_id >= ? ORDER BY row_id ASC
'''

INSERT_EVENT_SQL = '''
INSERT INTO event_logs (event) VALUES (?)
'''


class FilesystemEventLogStorage(WatchableEventLogStorage):
    def __init__(self, base_dir):
        self._base_dir = check.str_param(base_dir, 'base_dir')
        mkdir_p(self._base_dir)

        self._known_run_ids = set([])
        self._watchers = {}
        self._obs = Observer()
        self._obs.start()

    @contextmanager
    def _connect(self, run_id):
        try:
            with sqlite3.connect(self.filepath_for_run_id(run_id)) as conn:
                yield conn
        finally:
            conn.close()

    def filepath_for_run_id(self, run_id):
        check.str_param(run_id, 'run_id')
        return os.path.join(self._base_dir, '{run_id}.db'.format(run_id=run_id))

    def store_event(self, event):
        check.inst_param(event, 'event', EventRecord)
        run_id = event.run_id
        if not run_id in self._known_run_ids:
            with self._connect(run_id) as conn:
                conn.cursor().execute(CREATE_EVENT_LOG_SQL)
                self._known_run_ids.add(run_id)
        with self._connect(run_id) as conn:
            conn.cursor().execute(INSERT_EVENT_SQL, (serialize_dagster_namedtuple(event),))

    def get_logs_for_run(self, run_id, cursor=-1):
        check.str_param(run_id, 'run_id')
        check.int_param(cursor, 'cursor')
        check.invariant(
            cursor >= -1,
            'Don\'t know what to do with negative cursor {cursor}'.format(cursor=cursor),
        )

        events = []
        if not os.path.exists(self.filepath_for_run_id(run_id)):
            return events

        try:
            with self._connect(run_id) as conn:
                results = conn.cursor().execute(FETCH_EVENTS_SQL, (str(cursor),)).fetchall()
        except sqlite3.Error as err:
            six.raise_from(EventLogInvalidForRun(run_id=run_id), err)

        try:
            for (json_str,) in results:
                events.append(
                    check.inst_param(
                        deserialize_json_to_dagster_namedtuple(json_str), 'event', EventRecord
                    )
                )
        except (seven.JSONDecodeError, check.CheckError) as err:
            six.raise_from(EventLogInvalidForRun(run_id=run_id), err)

        return events

    def wipe(self):
        for filename in glob.glob(os.path.join(self._base_dir, '*.db')):
            os.unlink(filename)

    @property
    def is_persistent(self):
        return True

    def watch(self, run_id, start_cursor, callback):
        watchdog = EventLogStorageWatchdog(self, run_id, callback, start_cursor)
        self._watchers[run_id] = self._obs.schedule(watchdog, self._base_dir, True)

    def end_watch(self, run_id, handler):
        self._obs.remove_handler_for_watch(handler, self._watchers[run_id])
        del self._watchers[run_id]


class EventLogStorageWatchdog(PatternMatchingEventHandler):
    def __init__(self, event_log_storage, run_id, callback, start_cursor, **kwargs):
        self._event_log_storage = check.inst_param(
            event_log_storage, 'event_log_storage', WatchableEventLogStorage
        )
        self._run_id = check.str_param(run_id, 'run_id')
        self._cb = check.callable_param(callback, 'callback')
        self._log_path = event_log_storage.filepath_for_run_id(run_id)
        self._cursor = start_cursor
        super(EventLogStorageWatchdog, self).__init__(patterns=[self._log_path], **kwargs)

    def _process_log(self):
        events = self._event_log_storage.get_logs_for_run(self._run_id, self._cursor)
        self._cursor += len(events)
        for event in events:
            status = self._cb(event)

            if status == PipelineRunStatus.SUCCESS or status == PipelineRunStatus.FAILURE:
                self._event_log_storage.end_watch(self._run_id, self)

    def on_created(self, event):
        check.invariant(event.src_path == self._log_path)
        self._process_log()

    def on_modified(self, event):
        check.invariant(event.src_path == self._log_path)
        self._process_log()
