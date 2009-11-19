# txSpy, a set of tools to spy inside Twisted applications
#
# Copyright (C) 2009 Nicolas Trangez  <eikke eikke com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, version 2.1
# of the License.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301  USA

'''Heap object type count tracker

The inspiration for this tool comes from the Dowser_ project, which was released
in the public domain.

No code in this module is shared with the Dowser project, it is a from-scratch
implementation.

:author: Nicolas Trangez
:license: GNU Lesser General Public License version 2.1
:copyright: |copy| 2009 Nicolas Trangez

:see: Dowser_

.. _Dowser: http://www.aminus.net/wiki/Dowser
.. |copy| unicode:: 0xA9 .. copyright sign
'''

import gc
import time
import types
import operator
import itertools
import collections

from twisted.application import service
from twisted.internet import task
from twisted.python import log
from twisted.web import resource

import txspy

__author__ = txspy.__author__
__license__ = txspy.__license__
__version__ = txspy.__version__

__docformat__ = 'restructuredtext en'

def _log(fun, self, args, kwargs):
    '''
    Helper function calling log function 'fun' with the 'system' kwarg set to
    the name of IService 'self'

    :Parameters:
        fun : callable
          Logging function to call
        self : `twisted.application.service.IService`
          Service on which the method is executed
        args : iterable
          Args to pass to `fun`
        kwargs : dict
          Kwargs to pass to `fun`, updated with 'system'
    '''
    if 'system' not in kwargs:
        kwargs = kwargs.copy()
        kwargs['system'] = service.IService(self).name or '-'

    fun(*args, **kwargs)


class LoggedServiceMixin:
    '''Mixin providing logging-related utility methods'''

    msg = lambda self, *a, **k: _log(log.msg, self, a, k)
    err = lambda self, *a, **k: _log(log.err, self, a, k)
    debug = lambda self, *a, **k: None

    # IService helpers
    def startService(self):
        '''Start the service'''
        self.msg('Starting %s service "%s"' % \
                 (self.__class__.__name__, self.name))

    def stopService(self):
        '''Stop the service'''
        self.msg('Stopping %s service "%s"' % \
                 (self.__class__.__name__, self.name))


    def enableDebug(self):
        '''Enable debugging output'''
        self.debug = self.msg

    def disableDebug(self):
        '''Disable debugging output'''
        self.debug = LoggedServiceMixin.debug


def safeCall(fun, err=lambda exc: log.err(exc, 'safeCall failed')):
    '''
    Call a function, catching all exceptions, which are logged using 'err'

    This function returns nothing.

    :Parameters:
        fun : callable
          Function to execute
        err : callable
          Function to call if calling `fun` raises an `Exception`. The
          `Exception` will be passed as single argument.
    '''
    try:
        fun()
    except Exception, exc:
        err(exc)


def getTypeName(object_):
    '''Get the full type name of an object

    :Parameters:
        `object\_` : object
          Object of which to calculate the type

    :return: Complete name of `object_`'s type
    :rtype: str
    '''
    type_ = type(object_)

    if type_ == types.InstanceType:
        type_ = object_.__class__

    return '%s.%s' % (type_.__module__, type_.__name__)


# TODO Is there no C builtin for this somehow?
def count(iterable):
    '''Count the number of items in an iterable

    Note: this function consumes the iterable.

    :Parameters:
        iterable : iterable
          Iterable to count

    :return: Number of items in the given iterable
    :rtype: number
    '''
    result = 0

    for _ in iterable:
        result += 1

    return result


class ObjectBrowser(object, service.Service, resource.Resource,
                    LoggedServiceMixin):
    '''Object browser service'''

    __slots__ = '_sampleInterval', '_sampleHistorySize', '_loop', '_history', \
                   '_timestamps',
    
    def __init__(self, sampleInterval, sampleHistorySize):
        '''
        :Parameters:
            sampleInterval : number
              Interval (in seconds) object count samples should be taken
            sampleHistorySize : number
              Number of samples to keep track of
        '''
        self.msg('Initializing %s(%d, %d)' % \
                 (self.__class__.__name__, sampleInterval, sampleHistorySize))

        resource.Resource.__init__(self)

        self._sampleInterval = sampleInterval
        self._sampleHistorySize = sampleHistorySize

        self._loop = task.LoopingCall(
            lambda: safeCall(self.updateStats,
             lambda exc: self.err(exc, 'Error while updating object stats')))

        self._history = None
        self._timestamps = None

    # IService
    def startService(self):
        '''Start the service'''
        LoggedServiceMixin.startService(self)

        self._history = dict()
        self._timestamps = RingBuffer(self.sampleHistorySize)

        self.loop.start(self.sampleInterval)

        return service.Service.startService(self)

    def stopService(self):
        '''Stop the service'''
        self.loop.stop()

        self._history = None
        self._timestamps = None

        LoggedServiceMixin.stopService(self)
        
        return service.Service.stopService(self)


    # IResource
    isLeaf = True

    def render_GET(self, request):
        '''Temporary GET resource'''
        def genContent():
            for typeName, samples in sorted(self.history.iteritems()):
                yield '<div><strong>%s</strong>: %s</div>' % \
                          (typeName, samples)

        return '\n'.join(genContent())


    def updateStats(self):
        '''Update object count statistics'''
        self.debug('Updating object stats')

        gc.collect()

        # Get all objects
        allObjects = gc.get_objects()
        # Calculate their type names
        objectTypes = itertools.imap(getTypeName, allObjects)
        # Sort all names (for itertools.groupby to work correctly)
        sortedObjectTypes = sorted(objectTypes)

        # Group all object type names
        groups = itertools.groupby(sortedObjectTypes)

        # Put counts of types in the sample history
        for typeName, objects in groups:
            history = self._history.get(typeName, None)

            if history is None:
                history = RingBuffer(self.sampleHistorySize)
                # Prefill the buffer with zeros so final length will match
                # Note we didn't update self.timestamps yet
                history.extend(itertools.repeat(0, len(self.timestamps)))

                self.history[typeName] = history

            history.append(count(objects))


        objectTypeSet = set(sortedObjectTypes)
        # Can't use iteritems, modifying dict in the loop
        for typeName, samples in self.history.items():
            # Append 0 to every type we're tracking, but of which we no longer
            # found an object
            if typeName not in objectTypeSet:
                samples.append(0)

            # Prune object types for which we no longer have stats
            if all(s == 0 for s in samples):
                self.history.pop(typeName)

        # Update timestamp bookkeeping
        self.timestamps.append(time.time())

        # Some sanity checking
        numSamples = len(self.timestamps)
        assert all(len(history) == numSamples
                   for history in self.history.itervalues())

        self.debug('Tracking %d object types in %d samples' % \
                   (len(self.history), numSamples))


    sampleInterval = property(operator.attrgetter('_sampleInterval'),
                              doc='Sample interval')
    sampleHistorySize = property(operator.attrgetter('_sampleHistorySize'),
                                 doc='Number of samples to keep track of')
    loop = property(operator.attrgetter('_loop'), doc='Loop task')
    history = property(operator.attrgetter('_history'), doc='Sample history')
    timestamps = property(operator.attrgetter('_timestamps'),
                          doc='Sample timestamps')


class RingBuffer(object):
    '''Simple ring buffer implementation'''

    __slots__ = '_maxSize', '_collection',
    
    def __init__(self, size):
        '''
        :Parameters:
            size : number
              Maximum size of the ringbuffer
        '''
        assert size > 0
        self._maxSize = size
        self._collection = collections.deque()

    def append(self, x):
        '''Append an item to the ring buffer

        If the maximum number of items is exceeded, the buffer will be shifted.

        :Parameters:
            x : object
              Object to append to the buffer
        '''
        self._collection.append(x)

        if len(self) > self._maxSize:
            self._collection.popleft()

    def extend(self, xs):
        ''''''
        self._collection.extend(xs)
    extend.__doc__ = collections.deque.extend.__doc__

    def __len__(self):
        ''''''
        return self._collection.__len__()
    __len__.__doc__ = collections.deque.__len__.__doc__

    def __iter__(self):
        ''''''
        return self._collection.__iter__()
    __iter__.__doc__ = collections.deque.__iter__.__doc__

    def __str__(self):
        ''''''
        return self._collection.__str__()
    __iter__.__doc__ = collections.deque.__str__.__doc__

    # TODO This is not completely safe, e.g. in case extend() is called with
    # > maxSize elements


# Twistd compatibility
if __name__ == '__builtin__':
    from twisted.application import internet
    from twisted.web import server
    
    application = service.Application('web')

    objectbrowser = service.IService(ObjectBrowser(5, 200))
    objectbrowser.enableDebug()
    objectbrowser.setName('objectbrowser')
    objectbrowser.setServiceParent(application)
    
    site = server.Site(resource.IResource(objectbrowser))
    internet.TCPServer(8080, site).setServiceParent(application)
