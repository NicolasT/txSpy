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
import cgi
import time
import types
import base64
import operator
import itertools
import collections

from twisted.application import service
from twisted.internet import task
from twisted.python import log
from twisted.web import resource
from twisted.web.error import NoResource

import pygooglechart

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


def renderTemplate(template, values):
    '''Render a simple template

    :Parameters:
        template : str
          Template string
        values : dict
          Key/value pairs to fill placeholders

    :return: Rendered template
    :rtype: str
    '''
    for key, value in values.iteritems():
        template = template.replace('{ %s }' % key, value)

    return template


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
        self.putChild('style', CSSResource())
        self.putChild('script', ScriptResource())
        self.putChild('image', ImageResource())
        self.putChild('graphs', GraphResource(self))

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
    def getChild(self, name, request):
        if name == '':
            return self

        return resource.Resource.getChild(self, name, request)

    def render_GET(self, request):
        '''Temporary GET resource'''

        def genContent():
            # Make a type name slightly more human-readable
            hr = lambda n: n if not n.startswith('__builtin__.') \
                                else n[len('__builtin__.'):]

            history = sorted(self.history.iteritems(),
                             key=lambda (t, _): hr(t))

            # Some trickery to get everything in 3 columns
            for i in xrange(3):
                if i < 2:
                    yield '<div class="span-8">'
                else:
                    yield '<div class="span-8 last">'

                for typeName, samples in history[i::3]:
                    range_ = [0, ((max(samples) / 10) + 1) * 10]
                    chart = pygooglechart.SimpleLineChart(300, 60,
                                                          y_range=range_)

                    if len(samples) < self.sampleHistorySize:
                        data = list(samples)
                        data.extend(itertools.repeat(
                            None, self.sampleHistorySize - len(samples)))
                    else:
                        data = samples

                    chart.add_data(data)
                    chart.set_axis_labels(pygooglechart.Axis.LEFT,
                                          chart.y_range)

                    graphElement = '<img src="%s" />' % chart.get_url()

                    yield '''
<div class="minigraph">
    <strong>%(humanTypeName)s:</strong> %(min)d / %(max)d / %(current)d
    <div>
    <a href="graphs/%(uriTypeName)s" class="lightbox" title="%(typeName)s">
        %(img)s
    </a>
    </div>
</div>''' % {
    'typeName': typeName,
    'humanTypeName': hr(typeName),
    'uriTypeName': cgi.escape(typeName),
    'min': min(samples),
    'max': max(samples),
    'current': samples[-1],
    'img': graphElement,
}
                yield '</div>'

        return renderTemplate(BASE_TEMPLATE, {
            'title': 'Heap Usage Statistics',
            'root': '',
            'body': '''
<div class="span-24 last">
    <h1>Heap Usage Statistics</h1>
    <p>Object counts are min / max / current.</p>
</div>
%s''' % '\n'.join(genContent()),
        })
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


class GraphResource(resource.Resource):
    '''A resource redirecting to larger graphs for a given type'''
    def __init__(self, objectBrowser):
        '''
        :Parameters:
            objectBrowser : ObjectBrowser
              ObjectBrowser managing type count history
        '''
        resource.Resource.__init__(self)

        self.objectBrowser = objectBrowser

    def getChild(self, name, request):
        if name in self.objectBrowser.history.iterkeys():
            return self

        return resource.Resource.getChild(self, name, resource)

    def render_GET(self, request):
        typeName = request.prepath[-1]

        samples = self.objectBrowser.history[typeName]

        range_ = [0, ((max(samples) / 10) + 1) * 10]

        chart = pygooglechart.SimpleLineChart(700, 300, y_range=range_)

        chart.add_data(samples)
        chart.set_axis_labels(pygooglechart.Axis.LEFT, chart.y_range)

        request.redirect(chart.get_url())
        request.finish()


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

    def __getitem__(self, key):
        ''''''
        return self._collection.__getitem__(key)
    __getitem__.__doc__ = collections.deque.__getitem__.__doc__

    # TODO This is not completely safe, e.g. in case extend() is called with
    # > maxSize elements


class InlineResource(resource.Resource):
    '''A resource serving hardcoded strings'''
    RESOURCES = None
    CONTENT_TYPE = None

    def getChild(self, name, request):
        if name not in self.RESOURCES:
            return NoResource()

        return self

    def render_GET(self, request):
        request.setHeader('Content-Type', self.CONTENT_TYPE)
        return self.RESOURCES[request.prepath[-1]]

class CSSResource(InlineResource):
    '''A resource serving static CSS files'''
    # Little hack since STYLE isn't defined yet
    RESOURCES = property(lambda _: STYLE)
    CONTENT_TYPE = 'text/css'

class ScriptResource(InlineResource):
    '''A resource serving Javascript files'''
    RESOURCES = property(lambda _: SCRIPT)
    CONTENT_TYPE = 'text/javascript'

class ImageResource(InlineResource):
    '''A resource serving image files'''
    RESOURCES = property(lambda _: IMAGE)
    CONTENT_TYPE = 'image/gif'


# Twistd compatibility
if __name__ == '__builtin__':
    import random

    from twisted.application import internet
    from twisted.web import server
    
    application = service.Application('web')

    objectbrowser = service.IService(ObjectBrowser(5, 200))
    objectbrowser.enableDebug()
    objectbrowser.setName('objectbrowser')
    objectbrowser.setServiceParent(application)
    
    site = server.Site(resource.IResource(objectbrowser))
    internet.TCPServer(8080, site).setServiceParent(application)

    # Service keeping references to a random number of instances of a custom
    # type, for demonstration purposes
    class DemoType(object): pass

    class ReferenceGenerator(service.Service):
        def __init__(self):
            self.loop = task.LoopingCall(lambda: safeCall(self._run))

        def startService(self):
            self._container = list()
            self.loop.start(6)
            return service.Service.startService(self)

        def stopService(self):
            self._container = None
            return service.Service.stopService(self)

        def _run(self):
            cnt = random.randint(0, 300)
            log.msg('Generating %d demo objects' % cnt)
            self._container = [DemoType() for _ in xrange(cnt)]

    ReferenceGenerator().setServiceParent(application)


# Some templates and static files come next
BASE_TEMPLATE = '''
<!DOCTYPE html
     PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
     "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" dir="ltr" xml:lang="en">
<head>
    <title>{ title }</title>
    <link rel="stylesheet" type="text/css" media="screen, projection"
         href="{ root }style/screen.css" />
    <link rel="stylesheet" type="text/css" media="print"
         href="{ root }style/print.css" />
    <!--[if lt IE 8]>
    <link rel="stylesheet" type="text/css" media="screen, projection"
         href="{ root }style/ie.css" />
    <![endif]-->

    <link rel="stylesheet" type="text/css" media="screen, projection"
         href="{ root }style/lightbox.css" />

    <script type="text/javascript" src="http://www.google.com/jsapi"></script>
    <script type="text/javascript">
    google.load("jquery", "1.3.2");
    </script>
    <script type="text/javascript"
         src="{ root }script/jquery.lightbox-0.5.min.js"></script>

    <script type="text/javascript">
    $(function() {
        $('a.lightbox').lightBox({
            imageLoading: '{ root }image/lightbox-ico-loading.gif',
            imageBtnClose: '{ root }image/lightbox-btn-close.gif',
            imageBtnPrev: '{ root }image/lightbox-btn-prev.gif',
            imageBtnNext: '{ root }image/lightbox-btn-next.gif',
            imageBlank: '{ root }image/lightbox-blank.gif',
            txtImage: 'Type'
        });
    });
    </script>

    <style type="text/css">
    body {
        padding-top: 5px;
    }
    div.minigraph {
        margin: 3px;
    }
    </style>
</head>
<body>
<div class="container">
{ body }
</div>
</body>
</html>
'''


##############################################################################
# Blueprint CSS - See http://www.blueprintcss.org
#
# Blueprint is distributed under a modified MIT license:
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sub-license, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice, and every other copyright notice found in this
# software, and all the attributions in every file, and this permission notice
# shall be included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
##############################################################################

STYLE = {
    'screen.css': '''
/* -----------------------------------------------------------------------


 Blueprint CSS Framework 0.9
 http://blueprintcss.org

   * Copyright (c) 2007-Present. See LICENSE for more info.
   * See README for instructions on how to use Blueprint.
   * For credits and origins, see AUTHORS.
   * This is a compressed file. See the sources in the 'src' directory.

----------------------------------------------------------------------- */

/* reset.css */
html, body, div, span, object, iframe, h1, h2, h3, h4, h5, h6, p, blockquote, pre, a, abbr, acronym, address, code, del, dfn, em, img, q, dl, dt, dd, ol, ul, li, fieldset, form, label, legend, table, caption, tbody, tfoot, thead, tr, th, td {margin:0;padding:0;border:0;font-weight:inherit;font-style:inherit;font-size:100%;font-family:inherit;vertical-align:baseline;}
body {line-height:1.5;}
table {border-collapse:separate;border-spacing:0;}
caption, th, td {text-align:left;font-weight:normal;}
table, td, th {vertical-align:middle;}
blockquote:before, blockquote:after, q:before, q:after {content:"";}
blockquote, q {quotes:"" "";}
a img {border:none;}

/* typography.css */
html {font-size:100.01%;}
body {font-size:75%;color:#222;background:#fff;font-family:"Helvetica Neue", Arial, Helvetica, sans-serif;}
h1, h2, h3, h4, h5, h6 {font-weight:normal;color:#111;}
h1 {font-size:3em;line-height:1;margin-bottom:0.5em;}
h2 {font-size:2em;margin-bottom:0.75em;}
h3 {font-size:1.5em;line-height:1;margin-bottom:1em;}
h4 {font-size:1.2em;line-height:1.25;margin-bottom:1.25em;}
h5 {font-size:1em;font-weight:bold;margin-bottom:1.5em;}
h6 {font-size:1em;font-weight:bold;}
h1 img, h2 img, h3 img, h4 img, h5 img, h6 img {margin:0;}
p {margin:0 0 1.5em;}
p img.left {float:left;margin:1.5em 1.5em 1.5em 0;padding:0;}
p img.right {float:right;margin:1.5em 0 1.5em 1.5em;}
a:focus, a:hover {color:#000;}
a {color:#009;text-decoration:underline;}
blockquote {margin:1.5em;color:#666;font-style:italic;}
strong {font-weight:bold;}
em, dfn {font-style:italic;}
dfn {font-weight:bold;}
sup, sub {line-height:0;}
abbr, acronym {border-bottom:1px dotted #666;}
address {margin:0 0 1.5em;font-style:italic;}
del {color:#666;}
pre {margin:1.5em 0;white-space:pre;}
pre, code, tt {font:1em 'andale mono', 'lucida console', monospace;line-height:1.5;}
li ul, li ol {margin:0;}
ul, ol {margin:0 1.5em 1.5em 0;padding-left:3.333em;}
ul {list-style-type:disc;}
ol {list-style-type:decimal;}
dl {margin:0 0 1.5em 0;}
dl dt {font-weight:bold;}
dd {margin-left:1.5em;}
table {margin-bottom:1.4em;width:100%;}
th {font-weight:bold;}
thead th {background:#c3d9ff;}
th, td, caption {padding:4px 10px 4px 5px;}
tr.even td {background:#e5ecf9;}
tfoot {font-style:italic;}
caption {background:#eee;}
.small {font-size:.8em;margin-bottom:1.875em;line-height:1.875em;}
.large {font-size:1.2em;line-height:2.5em;margin-bottom:1.25em;}
.hide {display:none;}
.quiet {color:#666;}
.loud {color:#000;}
.highlight {background:#ff0;}
.added {background:#060;color:#fff;}
.removed {background:#900;color:#fff;}
.first {margin-left:0;padding-left:0;}
.last {margin-right:0;padding-right:0;}
.top {margin-top:0;padding-top:0;}
.bottom {margin-bottom:0;padding-bottom:0;}

/* forms.css */
label {font-weight:bold;}
fieldset {padding:1.4em;margin:0 0 1.5em 0;border:1px solid #ccc;}
legend {font-weight:bold;font-size:1.2em;}
input[type=text], input[type=password], input.text, input.title, textarea, select {background-color:#fff;border:1px solid #bbb;}
input[type=text]:focus, input[type=password]:focus, input.text:focus, input.title:focus, textarea:focus, select:focus {border-color:#666;}
input[type=text], input[type=password], input.text, input.title, textarea, select {margin:0.5em 0;}
input.text, input.title {width:300px;padding:5px;}
input.title {font-size:1.5em;}
textarea {width:390px;height:250px;padding:5px;}
input[type=checkbox], input[type=radio], input.checkbox, input.radio {position:relative;top:.25em;}
form.inline {line-height:3;}
form.inline p {margin-bottom:0;}
.error, .notice, .success {padding:.8em;margin-bottom:1em;border:2px solid #ddd;}
.error {background:#FBE3E4;color:#8a1f11;border-color:#FBC2C4;}
.notice {background:#FFF6BF;color:#514721;border-color:#FFD324;}
.success {background:#E6EFC2;color:#264409;border-color:#C6D880;}
.error a {color:#8a1f11;}
.notice a {color:#514721;}
.success a {color:#264409;}

/* grid.css */
.container {width:950px;margin:0 auto;}
.showgrid {background:url(src/grid.png);}
.column, div.span-1, div.span-2, div.span-3, div.span-4, div.span-5, div.span-6, div.span-7, div.span-8, div.span-9, div.span-10, div.span-11, div.span-12, div.span-13, div.span-14, div.span-15, div.span-16, div.span-17, div.span-18, div.span-19, div.span-20, div.span-21, div.span-22, div.span-23, div.span-24 {float:left;margin-right:10px;}
.last, div.last {margin-right:0;}
.span-1 {width:30px;}
.span-2 {width:70px;}
.span-3 {width:110px;}
.span-4 {width:150px;}
.span-5 {width:190px;}
.span-6 {width:230px;}
.span-7 {width:270px;}
.span-8 {width:310px;}
.span-9 {width:350px;}
.span-10 {width:390px;}
.span-11 {width:430px;}
.span-12 {width:470px;}
.span-13 {width:510px;}
.span-14 {width:550px;}
.span-15 {width:590px;}
.span-16 {width:630px;}
.span-17 {width:670px;}
.span-18 {width:710px;}
.span-19 {width:750px;}
.span-20 {width:790px;}
.span-21 {width:830px;}
.span-22 {width:870px;}
.span-23 {width:910px;}
.span-24, div.span-24 {width:950px;margin-right:0;}
input.span-1, textarea.span-1, input.span-2, textarea.span-2, input.span-3, textarea.span-3, input.span-4, textarea.span-4, input.span-5, textarea.span-5, input.span-6, textarea.span-6, input.span-7, textarea.span-7, input.span-8, textarea.span-8, input.span-9, textarea.span-9, input.span-10, textarea.span-10, input.span-11, textarea.span-11, input.span-12, textarea.span-12, input.span-13, textarea.span-13, input.span-14, textarea.span-14, input.span-15, textarea.span-15, input.span-16, textarea.span-16, input.span-17, textarea.span-17, input.span-18, textarea.span-18, input.span-19, textarea.span-19, input.span-20, textarea.span-20, input.span-21, textarea.span-21, input.span-22, textarea.span-22, input.span-23, textarea.span-23, input.span-24, textarea.span-24 {border-left-width:1px!important;border-right-width:1px!important;padding-left:5px!important;padding-right:5px!important;}
input.span-1, textarea.span-1 {width:18px!important;}
input.span-2, textarea.span-2 {width:58px!important;}
input.span-3, textarea.span-3 {width:98px!important;}
input.span-4, textarea.span-4 {width:138px!important;}
input.span-5, textarea.span-5 {width:178px!important;}
input.span-6, textarea.span-6 {width:218px!important;}
input.span-7, textarea.span-7 {width:258px!important;}
input.span-8, textarea.span-8 {width:298px!important;}
input.span-9, textarea.span-9 {width:338px!important;}
input.span-10, textarea.span-10 {width:378px!important;}
input.span-11, textarea.span-11 {width:418px!important;}
input.span-12, textarea.span-12 {width:458px!important;}
input.span-13, textarea.span-13 {width:498px!important;}
input.span-14, textarea.span-14 {width:538px!important;}
input.span-15, textarea.span-15 {width:578px!important;}
input.span-16, textarea.span-16 {width:618px!important;}
input.span-17, textarea.span-17 {width:658px!important;}
input.span-18, textarea.span-18 {width:698px!important;}
input.span-19, textarea.span-19 {width:738px!important;}
input.span-20, textarea.span-20 {width:778px!important;}
input.span-21, textarea.span-21 {width:818px!important;}
input.span-22, textarea.span-22 {width:858px!important;}
input.span-23, textarea.span-23 {width:898px!important;}
input.span-24, textarea.span-24 {width:938px!important;}
.append-1 {padding-right:40px;}
.append-2 {padding-right:80px;}
.append-3 {padding-right:120px;}
.append-4 {padding-right:160px;}
.append-5 {padding-right:200px;}
.append-6 {padding-right:240px;}
.append-7 {padding-right:280px;}
.append-8 {padding-right:320px;}
.append-9 {padding-right:360px;}
.append-10 {padding-right:400px;}
.append-11 {padding-right:440px;}
.append-12 {padding-right:480px;}
.append-13 {padding-right:520px;}
.append-14 {padding-right:560px;}
.append-15 {padding-right:600px;}
.append-16 {padding-right:640px;}
.append-17 {padding-right:680px;}
.append-18 {padding-right:720px;}
.append-19 {padding-right:760px;}
.append-20 {padding-right:800px;}
.append-21 {padding-right:840px;}
.append-22 {padding-right:880px;}
.append-23 {padding-right:920px;}
.prepend-1 {padding-left:40px;}
.prepend-2 {padding-left:80px;}
.prepend-3 {padding-left:120px;}
.prepend-4 {padding-left:160px;}
.prepend-5 {padding-left:200px;}
.prepend-6 {padding-left:240px;}
.prepend-7 {padding-left:280px;}
.prepend-8 {padding-left:320px;}
.prepend-9 {padding-left:360px;}
.prepend-10 {padding-left:400px;}
.prepend-11 {padding-left:440px;}
.prepend-12 {padding-left:480px;}
.prepend-13 {padding-left:520px;}
.prepend-14 {padding-left:560px;}
.prepend-15 {padding-left:600px;}
.prepend-16 {padding-left:640px;}
.prepend-17 {padding-left:680px;}
.prepend-18 {padding-left:720px;}
.prepend-19 {padding-left:760px;}
.prepend-20 {padding-left:800px;}
.prepend-21 {padding-left:840px;}
.prepend-22 {padding-left:880px;}
.prepend-23 {padding-left:920px;}
div.border {padding-right:4px;margin-right:5px;border-right:1px solid #eee;}
div.colborder {padding-right:24px;margin-right:25px;border-right:1px solid #eee;}
.pull-1 {margin-left:-40px;}
.pull-2 {margin-left:-80px;}
.pull-3 {margin-left:-120px;}
.pull-4 {margin-left:-160px;}
.pull-5 {margin-left:-200px;}
.pull-6 {margin-left:-240px;}
.pull-7 {margin-left:-280px;}
.pull-8 {margin-left:-320px;}
.pull-9 {margin-left:-360px;}
.pull-10 {margin-left:-400px;}
.pull-11 {margin-left:-440px;}
.pull-12 {margin-left:-480px;}
.pull-13 {margin-left:-520px;}
.pull-14 {margin-left:-560px;}
.pull-15 {margin-left:-600px;}
.pull-16 {margin-left:-640px;}
.pull-17 {margin-left:-680px;}
.pull-18 {margin-left:-720px;}
.pull-19 {margin-left:-760px;}
.pull-20 {margin-left:-800px;}
.pull-21 {margin-left:-840px;}
.pull-22 {margin-left:-880px;}
.pull-23 {margin-left:-920px;}
.pull-24 {margin-left:-960px;}
.pull-1, .pull-2, .pull-3, .pull-4, .pull-5, .pull-6, .pull-7, .pull-8, .pull-9, .pull-10, .pull-11, .pull-12, .pull-13, .pull-14, .pull-15, .pull-16, .pull-17, .pull-18, .pull-19, .pull-20, .pull-21, .pull-22, .pull-23, .pull-24 {float:left;position:relative;}
.push-1 {margin:0 -40px 1.5em 40px;}
.push-2 {margin:0 -80px 1.5em 80px;}
.push-3 {margin:0 -120px 1.5em 120px;}
.push-4 {margin:0 -160px 1.5em 160px;}
.push-5 {margin:0 -200px 1.5em 200px;}
.push-6 {margin:0 -240px 1.5em 240px;}
.push-7 {margin:0 -280px 1.5em 280px;}
.push-8 {margin:0 -320px 1.5em 320px;}
.push-9 {margin:0 -360px 1.5em 360px;}
.push-10 {margin:0 -400px 1.5em 400px;}
.push-11 {margin:0 -440px 1.5em 440px;}
.push-12 {margin:0 -480px 1.5em 480px;}
.push-13 {margin:0 -520px 1.5em 520px;}
.push-14 {margin:0 -560px 1.5em 560px;}
.push-15 {margin:0 -600px 1.5em 600px;}
.push-16 {margin:0 -640px 1.5em 640px;}
.push-17 {margin:0 -680px 1.5em 680px;}
.push-18 {margin:0 -720px 1.5em 720px;}
.push-19 {margin:0 -760px 1.5em 760px;}
.push-20 {margin:0 -800px 1.5em 800px;}
.push-21 {margin:0 -840px 1.5em 840px;}
.push-22 {margin:0 -880px 1.5em 880px;}
.push-23 {margin:0 -920px 1.5em 920px;}
.push-24 {margin:0 -960px 1.5em 960px;}
.push-1, .push-2, .push-3, .push-4, .push-5, .push-6, .push-7, .push-8, .push-9, .push-10, .push-11, .push-12, .push-13, .push-14, .push-15, .push-16, .push-17, .push-18, .push-19, .push-20, .push-21, .push-22, .push-23, .push-24 {float:right;position:relative;}
.prepend-top {margin-top:1.5em;}
.append-bottom {margin-bottom:1.5em;}
.box {padding:1.5em;margin-bottom:1.5em;background:#E5ECF9;}
hr {background:#ddd;color:#ddd;clear:both;float:none;width:100%;height:.1em;margin:0 0 1.45em;border:none;}
hr.space {background:#fff;color:#fff;visibility:hidden;}
.clearfix:after, .container:after {content:"\0020";display:block;height:0;clear:both;visibility:hidden;overflow:hidden;}
.clearfix, .container {display:block;}
.clear {clear:both;}
''',
    'print.css': '''
/* -----------------------------------------------------------------------


 Blueprint CSS Framework 0.9
 http://blueprintcss.org

   * Copyright (c) 2007-Present. See LICENSE for more info.
   * See README for instructions on how to use Blueprint.
   * For credits and origins, see AUTHORS.
   * This is a compressed file. See the sources in the 'src' directory.

----------------------------------------------------------------------- */

/* print.css */
body {line-height:1.5;font-family:"Helvetica Neue", Arial, Helvetica, sans-serif;color:#000;background:none;font-size:10pt;}
.container {background:none;}
hr {background:#ccc;color:#ccc;width:100%;height:2px;margin:2em 0;padding:0;border:none;}
hr.space {background:#fff;color:#fff;visibility:hidden;}
h1, h2, h3, h4, h5, h6 {font-family:"Helvetica Neue", Arial, "Lucida Grande", sans-serif;}
code {font:.9em "Courier New", Monaco, Courier, monospace;}
a img {border:none;}
p img.top {margin-top:0;}
blockquote {margin:1.5em;padding:1em;font-style:italic;font-size:.9em;}
.small {font-size:.9em;}
.large {font-size:1.1em;}
.quiet {color:#999;}
.hide {display:none;}
a:link, a:visited {background:transparent;font-weight:700;text-decoration:underline;}
a:link:after, a:visited:after {content:" (" attr(href) ")";font-size:90%;}
''',
    'ie.css': '''
/* -----------------------------------------------------------------------


 Blueprint CSS Framework 0.9
 http://blueprintcss.org

   * Copyright (c) 2007-Present. See LICENSE for more info.
   * See README for instructions on how to use Blueprint.
   * For credits and origins, see AUTHORS.
   * This is a compressed file. See the sources in the 'src' directory.

----------------------------------------------------------------------- */

/* ie.css */
body {text-align:center;}
.container {text-align:left;}
* html .column, * html div.span-1, * html div.span-2, * html div.span-3, * html div.span-4, * html div.span-5, * html div.span-6, * html div.span-7, * html div.span-8, * html div.span-9, * html div.span-10, * html div.span-11, * html div.span-12, * html div.span-13, * html div.span-14, * html div.span-15, * html div.span-16, * html div.span-17, * html div.span-18, * html div.span-19, * html div.span-20, * html div.span-21, * html div.span-22, * html div.span-23, * html div.span-24 {display:inline;overflow-x:hidden;}
* html legend {margin:0px -8px 16px 0;padding:0;}
sup {vertical-align:text-top;}
sub {vertical-align:text-bottom;}
html>body p code {*white-space:normal;}
hr {margin:-8px auto 11px;}
img {-ms-interpolation-mode:bicubic;}
.clearfix, .container {display:inline-block;}
* html .clearfix, * html .container {height:1%;}
fieldset {padding-top:0;}
textarea {overflow:auto;}
input.text, input.title, textarea {background-color:#fff;border:1px solid #bbb;}
input.text:focus, input.title:focus {border-color:#666;}
input.text, input.title, textarea, select {margin:0.5em 0;}
input.checkbox, input.radio {position:relative;top:.25em;}
form.inline div, form.inline p {vertical-align:middle;}
form.inline label {position:relative;top:-0.25em;}
form.inline input.checkbox, form.inline input.radio, form.inline input.button, form.inline button {margin:0.5em 0;}
button, input.button {position:relative;top:0.25em;}
''',

    'lightbox.css': '''
/**
 * jQuery lightBox plugin
 * This jQuery plugin was inspired and based on Lightbox 2 by Lokesh Dhakar (http://www.huddletogether.com/projects/lightbox2/)
 * and adapted to me for use like a plugin from jQuery.
 * @name jquery-lightbox-0.5.css
 * @author Leandro Vieira Pinho - http://leandrovieira.com
 * @version 0.5
 * @date April 11, 2008
 * @category jQuery plugin
 * @copyright (c) 2008 Leandro Vieira Pinho (leandrovieira.com)
 * @license CC Attribution-No Derivative Works 2.5 Brazil - http://creativecommons.org/licenses/by-nd/2.5/br/deed.en_US
 * @example Visit http://leandrovieira.com/projects/jquery/lightbox/ for more informations about this jQuery plugin
 */
#jquery-overlay {
	position: absolute;
	top: 0;
	left: 0;
	z-index: 90;
	width: 100%;
	height: 500px;
}
#jquery-lightbox {
	position: absolute;
	top: 0;
	left: 0;
	width: 100%;
	z-index: 100;
	text-align: center;
	line-height: 0;
}
#jquery-lightbox a img { border: none; }
#lightbox-container-image-box {
	position: relative;
	background-color: #fff;
	width: 250px;
	height: 250px;
	margin: 0 auto;
}
#lightbox-container-image { padding: 10px; }
#lightbox-loading {
	position: absolute;
	top: 40%;
	left: 0%;
	height: 25%;
	width: 100%;
	text-align: center;
	line-height: 0;
}
#lightbox-nav {
	position: absolute;
	top: 0;
	left: 0;
	height: 100%;
	width: 100%;
	z-index: 10;
}
#lightbox-container-image-box > #lightbox-nav { left: 0; }
#lightbox-nav a { outline: none;}
#lightbox-nav-btnPrev, #lightbox-nav-btnNext {
	width: 49%;
	height: 100%;
	zoom: 1;
	display: block;
}
#lightbox-nav-btnPrev { 
	left: 0; 
	float: left;
}
#lightbox-nav-btnNext { 
	right: 0; 
	float: right;
}
#lightbox-container-image-data-box {
	font: 10px Verdana, Helvetica, sans-serif;
	background-color: #fff;
	margin: 0 auto;
	line-height: 1.4em;
	overflow: auto;
	width: 100%;
	padding: 0 10px 0;
}
#lightbox-container-image-data {
	padding: 0 10px; 
	color: #666; 
}
#lightbox-container-image-data #lightbox-image-details { 
	width: 70%; 
	float: left; 
	text-align: left; 
}	
#lightbox-image-details-caption { font-weight: bold; }
#lightbox-image-details-currentNumber {
	display: block; 
	clear: left; 
	padding-bottom: 1.0em;	
}			
#lightbox-secNav-btnClose {
	width: 66px; 
	float: right;
	padding-bottom: 0.7em;	
}
''',
}


# Scripts
SCRIPT = {
    'jquery.lightbox-0.5.min.js': '''
/**
 * jQuery lightBox plugin
 * This jQuery plugin was inspired and based on Lightbox 2 by Lokesh Dhakar (http://www.huddletogether.com/projects/lightbox2/)
 * and adapted to me for use like a plugin from jQuery.
 * @name jquery-lightbox-0.5.js
 * @author Leandro Vieira Pinho - http://leandrovieira.com
 * @version 0.5
 * @date April 11, 2008
 * @category jQuery plugin
 * @copyright (c) 2008 Leandro Vieira Pinho (leandrovieira.com)
 * @license CC Attribution-No Derivative Works 2.5 Brazil - http://creativecommons.org/licenses/by-nd/2.5/br/deed.en_US
 * @example Visit http://leandrovieira.com/projects/jquery/lightbox/ for more informations about this jQuery plugin
 */
(function($){$.fn.lightBox=function(settings){settings=jQuery.extend({overlayBgColor:'#000',overlayOpacity:0.8,fixedNavigation:false,imageLoading:'images/lightbox-ico-loading.gif',imageBtnPrev:'images/lightbox-btn-prev.gif',imageBtnNext:'images/lightbox-btn-next.gif',imageBtnClose:'images/lightbox-btn-close.gif',imageBlank:'images/lightbox-blank.gif',containerBorderSize:10,containerResizeSpeed:400,txtImage:'Image',txtOf:'of',keyToClose:'c',keyToPrev:'p',keyToNext:'n',imageArray:[],activeImage:0},settings);var jQueryMatchedObj=this;function _initialize(){_start(this,jQueryMatchedObj);return false;}
function _start(objClicked,jQueryMatchedObj){$('embed, object, select').css({'visibility':'hidden'});_set_interface();settings.imageArray.length=0;settings.activeImage=0;if(jQueryMatchedObj.length==1){settings.imageArray.push(new Array(objClicked.getAttribute('href'),objClicked.getAttribute('title')));}else{for(var i=0;i<jQueryMatchedObj.length;i++){settings.imageArray.push(new Array(jQueryMatchedObj[i].getAttribute('href'),jQueryMatchedObj[i].getAttribute('title')));}}
while(settings.imageArray[settings.activeImage][0]!=objClicked.getAttribute('href')){settings.activeImage++;}
_set_image_to_view();}
function _set_interface(){$('body').append('<div id="jquery-overlay"></div><div id="jquery-lightbox"><div id="lightbox-container-image-box"><div id="lightbox-container-image"><img id="lightbox-image"><div style="" id="lightbox-nav"><a href="#" id="lightbox-nav-btnPrev"></a><a href="#" id="lightbox-nav-btnNext"></a></div><div id="lightbox-loading"><a href="#" id="lightbox-loading-link"><img src="'+settings.imageLoading+'"></a></div></div></div><div id="lightbox-container-image-data-box"><div id="lightbox-container-image-data"><div id="lightbox-image-details"><span id="lightbox-image-details-caption"></span><span id="lightbox-image-details-currentNumber"></span></div><div id="lightbox-secNav"><a href="#" id="lightbox-secNav-btnClose"><img src="'+settings.imageBtnClose+'"></a></div></div></div></div>');var arrPageSizes=___getPageSize();$('#jquery-overlay').css({backgroundColor:settings.overlayBgColor,opacity:settings.overlayOpacity,width:arrPageSizes[0],height:arrPageSizes[1]}).fadeIn();var arrPageScroll=___getPageScroll();$('#jquery-lightbox').css({top:arrPageScroll[1]+(arrPageSizes[3]/10),left:arrPageScroll[0]}).show();$('#jquery-overlay,#jquery-lightbox').click(function(){_finish();});$('#lightbox-loading-link,#lightbox-secNav-btnClose').click(function(){_finish();return false;});$(window).resize(function(){var arrPageSizes=___getPageSize();$('#jquery-overlay').css({width:arrPageSizes[0],height:arrPageSizes[1]});var arrPageScroll=___getPageScroll();$('#jquery-lightbox').css({top:arrPageScroll[1]+(arrPageSizes[3]/10),left:arrPageScroll[0]});});}
function _set_image_to_view(){$('#lightbox-loading').show();if(settings.fixedNavigation){$('#lightbox-image,#lightbox-container-image-data-box,#lightbox-image-details-currentNumber').hide();}else{$('#lightbox-image,#lightbox-nav,#lightbox-nav-btnPrev,#lightbox-nav-btnNext,#lightbox-container-image-data-box,#lightbox-image-details-currentNumber').hide();}
var objImagePreloader=new Image();objImagePreloader.onload=function(){$('#lightbox-image').attr('src',settings.imageArray[settings.activeImage][0]);_resize_container_image_box(objImagePreloader.width,objImagePreloader.height);objImagePreloader.onload=function(){};};objImagePreloader.src=settings.imageArray[settings.activeImage][0];};function _resize_container_image_box(intImageWidth,intImageHeight){var intCurrentWidth=$('#lightbox-container-image-box').width();var intCurrentHeight=$('#lightbox-container-image-box').height();var intWidth=(intImageWidth+(settings.containerBorderSize*2));var intHeight=(intImageHeight+(settings.containerBorderSize*2));var intDiffW=intCurrentWidth-intWidth;var intDiffH=intCurrentHeight-intHeight;$('#lightbox-container-image-box').animate({width:intWidth,height:intHeight},settings.containerResizeSpeed,function(){_show_image();});if((intDiffW==0)&&(intDiffH==0)){if($.browser.msie){___pause(250);}else{___pause(100);}}
$('#lightbox-container-image-data-box').css({width:intImageWidth});$('#lightbox-nav-btnPrev,#lightbox-nav-btnNext').css({height:intImageHeight+(settings.containerBorderSize*2)});};function _show_image(){$('#lightbox-loading').hide();$('#lightbox-image').fadeIn(function(){_show_image_data();_set_navigation();});_preload_neighbor_images();};function _show_image_data(){$('#lightbox-container-image-data-box').slideDown('fast');$('#lightbox-image-details-caption').hide();if(settings.imageArray[settings.activeImage][1]){$('#lightbox-image-details-caption').html(settings.imageArray[settings.activeImage][1]).show();}
if(settings.imageArray.length>1){$('#lightbox-image-details-currentNumber').html(settings.txtImage+' '+(settings.activeImage+1)+' '+settings.txtOf+' '+settings.imageArray.length).show();}}
function _set_navigation(){$('#lightbox-nav').show();$('#lightbox-nav-btnPrev,#lightbox-nav-btnNext').css({'background':'transparent url('+settings.imageBlank+') no-repeat'});if(settings.activeImage!=0){if(settings.fixedNavigation){$('#lightbox-nav-btnPrev').css({'background':'url('+settings.imageBtnPrev+') left 15% no-repeat'}).unbind().bind('click',function(){settings.activeImage=settings.activeImage-1;_set_image_to_view();return false;});}else{$('#lightbox-nav-btnPrev').unbind().hover(function(){$(this).css({'background':'url('+settings.imageBtnPrev+') left 15% no-repeat'});},function(){$(this).css({'background':'transparent url('+settings.imageBlank+') no-repeat'});}).show().bind('click',function(){settings.activeImage=settings.activeImage-1;_set_image_to_view();return false;});}}
if(settings.activeImage!=(settings.imageArray.length-1)){if(settings.fixedNavigation){$('#lightbox-nav-btnNext').css({'background':'url('+settings.imageBtnNext+') right 15% no-repeat'}).unbind().bind('click',function(){settings.activeImage=settings.activeImage+1;_set_image_to_view();return false;});}else{$('#lightbox-nav-btnNext').unbind().hover(function(){$(this).css({'background':'url('+settings.imageBtnNext+') right 15% no-repeat'});},function(){$(this).css({'background':'transparent url('+settings.imageBlank+') no-repeat'});}).show().bind('click',function(){settings.activeImage=settings.activeImage+1;_set_image_to_view();return false;});}}
_enable_keyboard_navigation();}
function _enable_keyboard_navigation(){$(document).keydown(function(objEvent){_keyboard_action(objEvent);});}
function _disable_keyboard_navigation(){$(document).unbind();}
function _keyboard_action(objEvent){if(objEvent==null){keycode=event.keyCode;escapeKey=27;}else{keycode=objEvent.keyCode;escapeKey=objEvent.DOM_VK_ESCAPE;}
key=String.fromCharCode(keycode).toLowerCase();if((key==settings.keyToClose)||(key=='x')||(keycode==escapeKey)){_finish();}
if((key==settings.keyToPrev)||(keycode==37)){if(settings.activeImage!=0){settings.activeImage=settings.activeImage-1;_set_image_to_view();_disable_keyboard_navigation();}}
if((key==settings.keyToNext)||(keycode==39)){if(settings.activeImage!=(settings.imageArray.length-1)){settings.activeImage=settings.activeImage+1;_set_image_to_view();_disable_keyboard_navigation();}}}
function _preload_neighbor_images(){if((settings.imageArray.length-1)>settings.activeImage){objNext=new Image();objNext.src=settings.imageArray[settings.activeImage+1][0];}
if(settings.activeImage>0){objPrev=new Image();objPrev.src=settings.imageArray[settings.activeImage-1][0];}}
function _finish(){$('#jquery-lightbox').remove();$('#jquery-overlay').fadeOut(function(){$('#jquery-overlay').remove();});$('embed, object, select').css({'visibility':'visible'});}
function ___getPageSize(){var xScroll,yScroll;if(window.innerHeight&&window.scrollMaxY){xScroll=window.innerWidth+window.scrollMaxX;yScroll=window.innerHeight+window.scrollMaxY;}else if(document.body.scrollHeight>document.body.offsetHeight){xScroll=document.body.scrollWidth;yScroll=document.body.scrollHeight;}else{xScroll=document.body.offsetWidth;yScroll=document.body.offsetHeight;}
var windowWidth,windowHeight;if(self.innerHeight){if(document.documentElement.clientWidth){windowWidth=document.documentElement.clientWidth;}else{windowWidth=self.innerWidth;}
windowHeight=self.innerHeight;}else if(document.documentElement&&document.documentElement.clientHeight){windowWidth=document.documentElement.clientWidth;windowHeight=document.documentElement.clientHeight;}else if(document.body){windowWidth=document.body.clientWidth;windowHeight=document.body.clientHeight;}
if(yScroll<windowHeight){pageHeight=windowHeight;}else{pageHeight=yScroll;}
if(xScroll<windowWidth){pageWidth=xScroll;}else{pageWidth=windowWidth;}
arrayPageSize=new Array(pageWidth,pageHeight,windowWidth,windowHeight);return arrayPageSize;};function ___getPageScroll(){var xScroll,yScroll;if(self.pageYOffset){yScroll=self.pageYOffset;xScroll=self.pageXOffset;}else if(document.documentElement&&document.documentElement.scrollTop){yScroll=document.documentElement.scrollTop;xScroll=document.documentElement.scrollLeft;}else if(document.body){yScroll=document.body.scrollTop;xScroll=document.body.scrollLeft;}
arrayPageScroll=new Array(xScroll,yScroll);return arrayPageScroll;};function ___pause(ms){var date=new Date();curDate=null;do{var curDate=new Date();}
while(curDate-date<ms);};return this.unbind('click').click(_initialize);};})(jQuery);
''',
}

# Images, base64-encoded
IMAGE = {
    'lightbox-blank.gif': base64.decodestring('''
R0lGODlhAQABAID/AMDAwAAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==
'''.strip()),
    'lightbox-btn-close.gif': base64.decodestring('''
R0lGODlhQgAWANUAAP////39/fr6+vj4+PX19fDw8Ovr6+np6ebm5uTk5N/f39zc3Nra2tfX19XV
1dLS0svLy8jIyL6+vre3t7S0tLKysq+vr6ioqKOjo56enpubm5aWlpGRkY+Pj4yMjIeHh4KCgoCA
gH19fXZ2dnNzc3FxcW5ubmlpaWdnZ2RkZGJiYl9fX11dXVpaWlVVVVNTU1BQUE5OTktLS0lJSUZG
RgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACH5BAAHAP8ALAAAAABCABYAAAb/QIBw
SCwaj8ikcslsOp/QqLQZmFqvRAHngYxsBEaCw3EYHhwE5IFykZSLZ7RQPK4PBxzaimGEtGgcaXMZ
L4UvH0IXLw5HEoaFF0MEIo8RAA6PhUMWNC8xKw1EES0xLzQYQ5QfY2yJi0YRh2UKlBRCHC8XDhEZ
ZZgcdYxCBiQzLzIqC0KjpTMhBUIULxlHisJEKCiCAAQoLEIvJEaYkUcGIzKeKAt+pTQgA0Pe20TW
RQrTRYSMLyjjuUnOGYOBgtQxENsOHEJij4g0S6IA4gpEBJOICxjfEDmXDgaMYx/iDSHH8FU9kyMB
TnrBguKlTNeIFCjx0ZMGMBUBVkPpKubL2XKXKLFQ8HNJgRM1Y3Qwgo9DSZ+ObBFxJKGIIxFFkyww
UcrjMQ1ViLDwt9OnQkREcGkcQuJF1iMLUKSLkWLFRxlgTwK91NMIJaJC8GE14u1tkQUr0skgkSCC
V7xhubUF5gCXJUUcMGJkpIAFCwkOKHgGnCHDmA/5LGoGyiCxCxkjDAiZ8DjvHEqFWFADoChTOQco
DKEADAC1od2YMglhwEIxCdlDKsjwSKMCnDFQFJAxEqdJgQ4yZjw3UiGGjBN8sKjP4mH8EQsnEKyf
P0QAvSIB5NPfz7+/+iAAOw==
'''.strip()),
    'lightbox-btn-next.gif': base64.decodestring('''
R0lGODlhPwAgAOYAAP////39/fv7+/n5+ff39/X19fPz8/Hx8e/v7+3t7evr6+np6efn5+bm5uTk
5OLi4uDg4N7e3tzc3NjY2NbW1tLS0tDQ0M7OzsrKysjIyMbGxsLCwsDAwLy8vLq6ura2trS0tLOz
s7Gxsa+vr62traurq6mpqaenp6WlpaOjo52dnZubm5mZmZGRkY+Pj42NjYuLi4eHh4WFhYODg4GB
gYCAgH5+fnp6enh4eHZ2dnR0dHBwcG5ubmxsbGpqamhoaGZmZmRkZGJiYmBgYF5eXlxcXFpaWlhY
WFZWVlRUVFJSUlBQUE5OTk1NTUtLS0lJSUdHR0VFRUNDQ////wAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACH5
BAEHAFMALAAAAAA/ACAAAAf/gFNTAISFhoeIiYqLjIiDjZCRkpOUlZaXmJmam5ydnp+goaKjpKWm
p6igARomF4UCHCIABiAlJrcnDxQoroQKJRsOKSYlIyUnIASeAS5PQxGEAzZFAAxCSUFCQkUXDD1G
EADMTRgWSEJGSkNDOAfLLUlMMsoDNUQAC0UzCQz9yhhNYATIwGRFgAENFqh4UmHBggDLXPx4wQQE
AAE07i0w8iIRCyUhdvBAYKgElAWhmPlogOOIAwAZ8Q3hUQJFChIFCBG4scRIhUMmoDRIKZHAhCTz
Yi4IYkSHyBruxM14UiScoaBDVUk8ECBFkxIxhuArIgNBAgUKIAIQoeSEkBtRiQlhJfqDZIEeRYIA
wWekBaIIR2oA+PDkxFWhdEkCMLfkBz4iOTpI9vBgAA0kEgAMuJFkQqG5qlrsiJtCSQ98QJpAWS3l
hAgnhglRODJabpSsoAgQUCtOt7gDCMwmSFDgQFpDBxYIIFRAwfJU0CkFEBCgunXr0TXxzs69u/fv
4MOLdzRekqDykAIBADs=
'''.strip()),
    'lightbox-btn-prev.gif': base64.decodestring('''
R0lGODlhPwAgAOYAAP////39/fv7+/n5+fj4+Pb29vT09PLy8vDw8O7u7urq6unp6efn5+Xl5ePj
4+Hh4d/f393d3dvb29ra2tjY2NbW1tTU1NDQ0M7OzszMzMvLy8nJycfHx8XFxcPDw8HBwb+/v729
vby8vLq6uri4uLa2trKysq6urq2traurq6mpqaenp6Ojo6GhoZ+fn56enpycnJqampiYmJSUlJCQ
kI+Pj42NjYWFhYODg4GBgYCAgHx8fHp6enh4eHZ2dnR0dHJycnFxcW9vb21tbWtra2lpaWVlZWNj
Y2JiYmBgYF5eXlxcXFpaWlhYWFZWVlRUVFNTU1FRUU9PT01NTUtLS0lJSUdHR0JCQkBAQP///wAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACH5
BAEHAFkALAAAAAA/ACAAAAf/gACCg4SFhoeIiYqGWVmLj5CRj46SlZaXmJmam5ydnp+goaKjpKWm
p6ipmwMYICAfFQSCE64hGQuCBx4fvLwMGhwBgwMbG6IFOVFJyzsQAC1QSkpIRikAEUtOS0xMShc1
URKDE0o2xzpCBQclVDYCLUsSAxFBTRQOUjQQFPwBH1MuBqmoguEcEAEAAjDxYYCFkgaCVkT5wGDK
ikIIiAwpAKCAjyDCQhXQEQRBgRJWbARgsUTEhBBHkkBw8ARHiBEkPCCM8UQDAAtVRIwqgCOKkiVJ
ekwAwAKKkSVPgHQAIGGJkiJGjuAYAGDCFBkAZCRhMDSHEQ8dKMh6tkQDCiojnQRJiDLDAYQIDkLu
EALhyAyE534YcpFEgQAfSA5QnWLNEIcpN5IUHKoDCFdCLpQ4ACCCyotrUWhUGG0hgaAEQ6rwCHmM
B5HLg2A8cWagR5UND55YucIbiwlBAWBcOVEqwIIGrAUpkLBWQYUFAyRMmD4dwaADFDiq2n6JwIAB
AsIL+E4gOfdEAgKoX7/+vPv38OPLn0+//iVK9hU1CgQAOw==
'''.strip()),
    'lightbox-ico-loading.gif': base64.decodestring('''
R0lGODlhIAAgAOYAAP////39/fr6+vj4+PX19fPz8/Dw8O7u7uvr6+np6ebm5uTk5OHh4d/f39zc
3Nra2tfX19XV1dLS0s3NzcvLy8jIyMbGxsPDw8HBwb6+vry8vLm5ube3t7S0tLKysq+vr62traqq
qqioqKWlpZ6enpaWlpSUlJGRkY+Pj4yMjIqKioeHh4WFhYKCgoCAgH19fXt7e3h4eHZ2dnNzc3Fx
cWxsbGlpaWJiYl9fX1paWlhYWFVVVU5OTktLS0REREFBQT8/Pzw8PDc3NzIyMjAwMC0tLSsrKygo
KCYmJiMjIyEhIR4eHhwcHBkZGRcXFxISEg8PDw0NDQoKCggICAUFBf4BAgAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACH/
C05FVFNDQVBFMi4wAwEAAAAh+QQFCgBVACwAAAAAIAAgAAAH/4AAgoOEhYaHiImKi4yNjQGGBAgC
joYIEhcRCIMCGywSlYQPKzxAPCsQkAQnPxuhgggwSlS0STAIAQEQHJuECA0EiZ1BtMVCG5AAub4u
OR/BhwQkR8W1JNCGEkJQNQeIBCNF1VRHIZSHCM3PiAESPFHVPBTJ6A/YhwciPk1PTD4jBV4VClDg
AYgVMlJo8CZwEAELKmSo2MDgwDlHyzht6NEEir8KryCQIMFgEIMa42bcW4TgxZIkKRhK8DHOR69G
DHLQqtErAo9xPG4yKlACiA8RAQEcWLGkGJMTFxsdoECBoTIINYQkESKjZMOBDDaE2ADB4tdoFUic
4EUvUUZ2FpV2IFkSxIRQQwEokLiQFJ0MJ8V+YHC7AQgTIdcQNdghpdgREckIFMB2QAYUWjYcIEJQ
4zKtIBoACJiA4kWIXgVWOJkCxcbdQQE2NmlChAUDXTmcQCFCwlu7HEBydIhaiIAnGCQaBACHpFiO
B4ICPMgQYaUhAQgQQAOXhBaU54TahpKQezeJvmdFX2Axg2T6QgIOaHcUCAAh+QQFCgBVACwAAAAA
HwAgAAAH/4BVgoOEhAgODgiFi4yMBBYvOzsuFQSNl4sXOU1UVE05FpiiVQguS52dSy+Ko4USCAIQ
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
'''.strip()),
}
