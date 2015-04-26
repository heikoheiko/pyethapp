from devp2p.service import BaseService
from gevent.event import Event
from ethereum.db import _EphemDB


class EphemDB(_EphemDB, BaseService):

    name = 'db'

    def __init__(self, app):
        BaseService.__init__(self, app)
        _EphemDB.__init__(self)
        self.stop_event = Event()

    def _run(self):
        self.stop_event.wait()

    def stop(self):
        self.stop_event.set()
        # commit?
        log.info('closing db')
