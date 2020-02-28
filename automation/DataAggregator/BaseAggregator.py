import abc
import json
import logging
import queue
import threading
import time
from typing import Any, Dict, List

from multiprocess import Queue

from ..SocketInterface import serversocket
from ..utilities.multiprocess_utils import Process

RECORD_TYPE_CONTENT = 'page_content'
STATUS_TIMEOUT = 120  # seconds
SHUTDOWN_SIGNAL = 'SHUTDOWN'

STATUS_UPDATE_INTERVAL = 5  # seconds


class BaseListener(object):
    """Base class for the data aggregator listener process. This class is used
    alongside the BaseAggregator class to spawn an aggregator process that
    combines data collected in multiple crawl processes and stores it
    persistently as specified in the child class. The BaseListener class
    is instantiated in the remote process, and sets up a listening socket to
    receive data. Classes which inherit from this base class define
    how that data is written to disk.
    """
    __metaclass = abc.ABCMeta

    def __init__(self, status_queue: Queue, completion_queue: Queue,
                 shutdown_queue: Queue):
        """
        Creates a BaseListener instance

        Parameters
        ----------
        status_queue
            queue that the current amount of records to be processed will
            be sent to
            also used for initialization
        completion_queue
            queue containing the visitIDs of saved records
        shutdown_queue
            queue that the main process can use to shut down the listener
        """
        self.status_queue = status_queue
        self.completion_queue = completion_queue
        self.shutdown_queue = shutdown_queue
        self._shutdown_flag = False
        self._last_update = time.time()  # last status update time
        self.record_queue = None  # Initialized on `startup`
        self.logger = logging.getLogger('openwpm')
        self.browser_map = dict()  # maps crawl_id to visit_id

    @abc.abstractmethod
    def process_record(self, record):
        """Parse and save `record` to persistent storage.

        Parameters
        ----------
        record : tuple
            2-tuple in format (table_name, data). `data` is a dict which maps
            column name to the record for that column"""

    @abc.abstractmethod
    def process_content(self, record):
        """Parse and save page content `record` to persistent storage.

        Parameters
        ----------
        record : tuple
            2-tuple in format (table_name, data). `data` is a 2-tuple of the
            for (content, content_hash)"""

    @abc.abstractmethod
    def visit_done(self, visit_id: int, is_shutdown: bool = False):
        """Will be called once a visit_id will receive no new records

        Parameters
        ----------
        visit_id
            the id that will receive no more updates
        is_shutdowb
            if this call is made during shutdown"""

    def startup(self):
        """Run listener startup tasks

        Note: Child classes should call this method"""
        self.sock = serversocket(name=type(self).__name__)
        self.status_queue.put(self.sock.sock.getsockname())
        self.sock.start_accepting()
        self.record_queue = self.sock.queue

    def should_shutdown(self):
        """Return `True` if the listener has received a shutdown signal"""
        if not self.shutdown_queue.empty():
            self.shutdown_queue.get()
            self.logger.info("Received shutdown signal!")
            return True
        return False

    def update_status_queue(self):
        """Send manager process a status update."""
        if (time.time() - self._last_update) < STATUS_UPDATE_INTERVAL:
            return
        qsize = self.record_queue.qsize()
        self.status_queue.put(qsize)
        self.logger.debug(
            "Status update; current record queue size: %d. "
            "current number of threads: %d." %
            (qsize, threading.active_count())
        )
        self._last_update = time.time()

    def update_records(self, table: str, data: Dict[str, Any]):
        """A method to keep track of which browser is working on which visit_id
           Some data should contain a visit_id and a crawl_id, but the method
           handles both being not set
        """

        # All data records should be keyed by the crawler and site visit
        try:
            visit_id = data['visit_id']
        except KeyError:
            self.logger.error("Record for table %s has no visit id" % table)
            self.logger.error(json.dumps(data))
            return

        try:
            crawl_id = data['crawl_id']
        except KeyError:
            self.logger.error("Record for table %s has no crawl id" % table)
            self.logger.error(json.dumps(data))
            return
        # Check if the browser for this record has moved on to a new visit
        if crawl_id not in self.browser_map:
            self.browser_map[crawl_id] = visit_id
        elif self.browser_map[crawl_id] != visit_id:
            self.mark_visit_id_done(self.browser_map[crawl_id])
            self.visit_done(self.browser_map[crawl_id])
            self.browser_map[crawl_id] = visit_id

    def mark_visit_id_done(self, visit_id: int):
        """ This function should be called to indicate that all records
        relating to a certain visit_id have been saved"""

        self.logger.debug("Putting visit_id {0} into queue".format(visit_id))

        self.completion_queue.put(visit_id)

    def shutdown(self):
        """Run shutdown tasks defined in the base listener
        Note: Child classes should call this method"""
        self.sock.close()
        for visit_id in self.browser_map.values():
            self.mark_visit_id_done(visit_id, is_shutdown=True)

    def drain_queue(self):
        """ Ensures queue is empty before closing """
        time.sleep(3)  # TODO: the socket needs a better way of closing
        while not self.record_queue.empty():
            record = self.record_queue.get()
            self.process_record(record)


class BaseAggregator(object):
    """Base class for the data aggregator interface. This class is used
    alongside the BaseListener class to spawn an aggregator process that
    combines data from multiple crawl processes. The BaseAggregator class
    manages the child listener process.

    Parameters
    ----------
    manager_params : dict
        TaskManager configuration parameters
    browser_params : list of dict
        List of browser configuration dictionaries"""
    __metaclass__ = abc.ABCMeta

    def __init__(self, manager_params, browser_params):
        self.manager_params = manager_params
        self.browser_params = browser_params
        self.listener_address = None
        self.listener_process = None
        self.status_queue = Queue()
        self.completion_queue = Queue()
        self.shutdown_queue = Queue()
        self._last_status = None
        self._last_status_received = None
        self.logger = logging.getLogger('openwpm')

    @abc.abstractmethod
    def save_configuration(self, openwpm_version, browser_version):
        """Save configuration details to the database"""

    @abc.abstractmethod
    def get_next_visit_id(self):
        """Return a unique visit ID to be used as a key for a single visit"""

    @abc.abstractmethod
    def get_next_crawl_id(self):
        """Return a unique crawl ID used as a key for a browser instance"""

    def get_most_recent_status(self):
        """Return the most recent queue size sent from the listener process"""

        # Block until we receive the first status update
        if self._last_status is None:
            return self.get_status()

        # Drain status queue until we receive most recent update
        while not self.status_queue.empty():
            self._last_status = self.status_queue.get()
            self._last_status_received = time.time()

        # Check last status signal
        if (time.time() - self._last_status_received) > STATUS_TIMEOUT:
            raise RuntimeError(
                "No status update from DataAggregator listener process "
                "for %d seconds." % (time.time() - self._last_status_received)
            )

        return self._last_status

    def get_status(self):
        """Get listener process status. If the status queue is empty, block."""
        try:
            self._last_status = self.status_queue.get(
                block=True, timeout=STATUS_TIMEOUT)
            self._last_status_received = time.time()
        except queue.Empty:
            raise RuntimeError(
                "No status update from DataAggregator listener process "
                "for %d seconds." % (time.time() - self._last_status_received)
            )
        return self._last_status

    def get_saved_visit_ids(self) -> List[int]:
        """Returns a list of all visit ids that have been saved at the time
        of calling this method.
        This method will return an empty list in case no visit ids have
        been finished since the last time this method was called"""
        finished_visit_ids = list()
        while not self.completion_queue.empty():
            finished_visit_ids.append(self.completion_queue.get())
        return finished_visit_ids

    def launch(self, listener_process_runner, *args):
        """Launch the aggregator listener process"""
        args = ((self.status_queue,
                 self.completion_queue, self.shutdown_queue),) + args
        self.listener_process = Process(
            target=listener_process_runner,
            args=args
        )
        self.listener_process.daemon = True
        self.listener_process.start()
        self.listener_address = self.status_queue.get()

    def shutdown(self):
        """ Terminate the aggregator listener process"""
        self.logger.debug(
            "Sending the shutdown signal to the %s listener process..." %
            type(self).__name__
        )
        self.shutdown_queue.put(SHUTDOWN_SIGNAL)
        start_time = time.time()
        self.listener_process.join(300)
        self.logger.debug(
            "%s took %s seconds to close." % (
                type(self).__name__,
                str(time.time() - start_time)
            )
        )
        self.listener_address = None
        self.listener_process = None
