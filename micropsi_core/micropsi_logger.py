"""
    Logging classes for storing log records in memory so browser clients
    can fetch them.
"""

__author__ = 'rvuine'

import logging
import time
from operator import itemgetter

MAX_RECORDS_PER_STORAGE = 1000

class RecordWebStorageHandler(logging.Handler):

    record_storage = None

    def __init__(self, record_storage):
        """
        Initialize the handler
        """
        logging.Handler.__init__(self)
        self.record_storage = record_storage

    def flush(self):
        """
        does nothing for this handler
        """

    def emit(self, record):
        self.format(record)
        while len(self.record_storage) >= MAX_RECORDS_PER_STORAGE:
            del self.record_storage[0]
        dictrecord = {
            "logger": record.name,
            "time": record.created * 1000,
            "level": record.levelname,
            "msg": record.message
        }
        self.record_storage.append(dictrecord)


class MicropsiLogger():

    logging_levels = {
        'CRITICAL': logging.CRITICAL,
        'ERROR': logging.ERROR,
        'WARNING': logging.WARNING,
        'INFO': logging.INFO,
        'DEBUG': logging.DEBUG
    }

    frontend_loggers = {
        'system': {},
        'world': {},
        'nodenet': {}
    }

    nodenet_record_storage = []
    world_record_storage = []
    system_record_storage = []

    records = {}

    handlers = {}


    def __init__(self, default_logging_levels={}):

        logging.basicConfig(level=self.logging_levels.get('logging_level', logging.INFO))

        self.system_logger = logging.getLogger("system")
        self.world_logger = logging.getLogger("world")
        self.nodenet_logger = logging.getLogger("nodenet")

        self.system_logger.setLevel(self.logging_levels.get(default_logging_levels.get('system', {}), logging.WARNING))
        self.world_logger.setLevel(self.logging_levels.get(default_logging_levels.get('world', {}), logging.WARNING))
        self.nodenet_logger.setLevel(self.logging_levels.get(default_logging_levels.get('nodenet', {}), logging.WARNING))

        logging.captureWarnings(True)

        self.handlers = {
            'system': RecordWebStorageHandler(self.system_record_storage),
            'world': RecordWebStorageHandler(self.world_record_storage),
            'nodenet': RecordWebStorageHandler(self.nodenet_record_storage)
        }

        logging.getLogger("py.warnings").addHandler(self.handlers['system'])

        logging.getLogger("system").addHandler(self.handlers['system'])
        logging.getLogger("world").addHandler(self.handlers['world'])
        logging.getLogger("nodenet").addHandler(self.handlers['nodenet'])

        logging.getLogger("system").debug("System logger ready.")
        logging.getLogger("world").debug("World logger ready.")
        logging.getLogger("nodenet").debug("Nodenet logger ready.")

    def clear_logs(self):
        del self.system_record_storage[:]
        del self.world_record_storage[:]
        del self.nodenet_record_storage[:]

    def set_logging_level(self, logger, level):
        logging.getLogger(logger).setLevel(self.logging_levels[level])

    def get_logs(self, logger=[], after=0):
        """
            Returns a dict with the current time and a list of log entries,
            filtered by logger name and timestamp
        """
        logs = []
        if 'system' in logger:
            logs.extend(self.system_record_storage)
        if 'world' in logger:
            logs.extend(self.world_record_storage)
        if 'nodenet' in logger:
            logs.extend(self.nodenet_record_storage)

        logs = sorted(logs, key=itemgetter('time'))

        if after > 0:
            logs = [l for l in logs if l['time'] >= after]

        now = int(round(time.time() * 1000))
        return {
            "servertime": now,
            "logs": logs}
