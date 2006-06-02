import unittest
import rfc822
import socket
import traceback
from copy import copy

from BitTornado.clock import clock

import database
import dialogs
import eventloop
import httpclient
import util
from test.schedulertest import EventLoopTest
from test.framework import DemocracyTestCase

class TestingConnectionHandler(httpclient.ConnectionHandler):
    def __init__(self):
        super(TestingConnectionHandler, self).__init__()
        self.states['foo'] = self.handleFoo
        self.states['bar'] = self.handleBar
        self.states['noread'] = None
        self.fooData = ''
        self.barData = ''
        self.gotHandleClose = False
        self.closeType = None
    def handleFoo(self):
        data = self.buffer.read()
        self.fooData += data
        eventloop.quit()
    def handleBar(self):
        data = self.buffer.read()
        self.barData += data
        eventloop.quit()
    def handleClose(self, type):
        self.gotHandleClose = True
        self.closeType = type
        eventloop.quit()

class TestingHTTPConnection(httpclient.HTTPConnection):
    """HTTPConnection that doesn't actually connect to the network."""

    def __init__(self, closeCallback=None, readyCallback=None):
        class FakeStream(object):
            def isOpen(self):
                return True
        super(TestingHTTPConnection, self).__init__(closeCallback,
                readyCallback)
        self.output = ''
        self.stream = FakeStream()

    def openConnection(self, host, port):
        self.host = host
        self.port = port
        self.socketOpen = True

    def closeConnection(self):
        self.socketOpen = False
        if self.closeCallback:
            self.closeCallback(self)

    def sendData(self, data):
        self.output += data

    def updateReadCallback(self):
        pass

class TestingHTTPConnectionPool(httpclient.HTTPConnectionPool):
    MAX_CONNECTIONS = 4 # makes testing more sane

    def __init__(self):
        super(TestingHTTPConnectionPool, self).__init__()

    def _makeNewConnection(self, req):
        conn = TestingHTTPConnection(self._onConnectionClosed,
                self._onConnectionReady)
        conn.scheme = req['scheme']
        conn.openConnection(req['host'], req['port'])
        conn.sendRequest(req['callback'], req['errback'],
                req['headerCallback'], req['bodyDataCallback'], req['method'],
                req['path'], req['headers'])
        return conn

    def checkConnectionStarted(self, url):
        scheme, host, port, path = httpclient.parseURL(url)
        conns = self._getServerConnections(scheme, host, port)
        for conn in conns['active']:
            if conn.path == path:
                return True
        return False

    def assertConnectionStarted(self, url):
        assert self.checkConnectionStarted(url)

    def assertConnectionNotStarted(self, url):
        assert (not self.checkConnectionStarted(url))

    def getConnection(self, scheme, host, port=None, type='active'):
        if port is None:
            if scheme == 'https':
                port = 443
            else:
                port = 80
        conns = self._getServerConnections(scheme, host, port)
        for conn in conns[type]:
            return conn # returns the 1st one we find

    def finishConnection(self, scheme, host, port=None, type='active'):
        conn = self.getConnection(scheme, host, port, type)
        conn.handleData("""\
HTTP/1.1 200 OK\r
Content-Type: text/plain; charset=ISO-8859-1\r
Last-Modified: Wed, 10 May 2006 22:30:33 GMT\r
Date: Wed, 10 May 2006 22:38:39 GMT\r
Content-Length: 4
\r
1234""")

    def closeConnection(self, scheme, host, port=None, type='active'):
        conn = self.getConnection(scheme, host, port, type)
        conn.handleClose(socket.SHUT_RDWR)

class TestingAuthDelegate:
    def __init__(self):
        self.logins = []
    def addLogin(self, user, password):
        self.logins.append((user, password))

    def runDialog(self, dialog):
        if self.logins:
            user, password = self.logins.pop(0)
            dialog.runCallback(dialogs.BUTTON_OK, user, password)
        else:
            dialog.runCallback(None)

def startResponse(version='1.1', status=200, headers={}):
    rv = """\
HTTP/%s %s OK\r
Content-Type: text/plain; charset=ISO-8859-1\r
Last-Modified: Wed, 10 May 2006 22:30:33 GMT\r
Date: Wed, 10 May 2006 22:38:39 GMT\r
""" % (version, status)
    for key, value in headers.items():
        rv += '%s: %s\r\n' % (key, value)
    rv += '\r\n'
    return rv

class NetworkBufferTest(DemocracyTestCase):
    def setUp(self):
        self.buffer = httpclient.NetworkBuffer()

    def testReadLine(self):
        self.buffer.addData("HEL")
        self.assertEquals(self.buffer.readline(), None)
        self.buffer.addData("LO\r\n")
        self.assertEquals(self.buffer.readline(), 'HELLO')
        self.buffer.addData("HOWS\r\nIT\nGOING\r\nCRONLY\rDOESNTBREAK")
        self.assertEquals(self.buffer.readline(), 'HOWS')
        self.assertEquals(self.buffer.readline(), 'IT')
        self.assertEquals(self.buffer.readline(), 'GOING')
        self.assertEquals(self.buffer.readline(), None)
        self.assertEquals(self.buffer.read(), "CRONLY\rDOESNTBREAK")

    def testRead(self):
        self.buffer.addData("12345678901234567890")
        self.assertEquals(self.buffer.read(4), "1234")
        self.assertEquals(self.buffer.read(6), "567890")
        self.buffer.addData("CARBOAT")
        self.assertEquals(self.buffer.read(), "1234567890CARBOAT")

    def testLength(self):
        self.buffer.addData("ONE\r\nTWO")
        self.assertEquals(self.buffer.length, 8)
        self.buffer.readline()
        self.assertEquals(self.buffer.length, 3)
        self.buffer.read(1)
        self.assertEquals(self.buffer.length, 2)
        self.buffer.unread("AAA")
        self.assertEquals(self.buffer.length, 5)
        self.buffer.addData("MORE")
        self.assertEquals(self.buffer.length, 9)

    def testGetValue(self):
        self.buffer.addData("ONE")
        self.buffer.addData("TWO")
        self.buffer.addData("THREE")
        self.assertEquals(self.buffer.getValue(), "ONETWOTHREE")
        # check to make sure the value doesn't change as a result
        self.assertEquals(self.buffer.getValue(), "ONETWOTHREE")

class AsyncSocketTest(EventLoopTest):
    def setUp(self):
        self.data = None
        self.errbackCalled = False
        self.callbackCalled = False
        self.fakeCallbackError = False
        EventLoopTest.setUp(self)

    def callback(self, data):
        if self.fakeCallbackError:
            1/0
        self.data = data
        self.callbackCalled = True
        eventloop.quit()

    def errback(self, error):
        self.data = error
        self.errbackCalled = True
        eventloop.quit()

    def testCloseDuringOpenConnection(self):
        # Test opening a connection, then closing the HTTPConnection before it
        # happens.  The openConnection callback shouldn't be called
        #
        # open a socket on localhost and try to connect to that, this should
        # be pretty much instantanious, so we don't need a long timeout to
        # runEventLoop
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind( ('127.0.0.1', 0))
        sock.listen(1)
        host, port = sock.getsockname()
        try:
            conn = httpclient.AsyncSocket()
            conn.openConnection(host, port, self.callback, self.errback)
            conn.closeConnection()
            self.runEventLoop(timeout=1, timeoutNormal=True)
            self.assert_(not self.callbackCalled)
            self.assert_(self.errbackCalled)
        finally:
            sock.close()

    def testCloseDurringAcceptConnection(self):
        # Test opening a connection, then closing the HTTPConnection before it
        # happens.  The openConnection callback shouldn't be called
        #
        # open a socket on localhost and try to connect to that, this should
        # be pretty much instantanious, so we don't need a long timeout to
        # runEventLoop
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            conn = httpclient.AsyncSocket()
            conn.acceptConnection('127.0.0.1', 0, self.callback, self.errback)
            sock.connect((conn.addr, conn.port))
            conn.closeConnection()
            self.runEventLoop(timeout=1, timeoutNormal=True)
            self.assert_(not self.callbackCalled)
            self.assert_(self.errbackCalled)
        finally:
            sock.close()

class ConnectionHandlerTest(EventLoopTest):
    def setUp(self):
        super(ConnectionHandlerTest, self).setUp()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind( ('127.0.0.1', 0) )
        server.listen(1)
        address = server.getsockname()

        self.connectionHandler = TestingConnectionHandler()
        def stopEventLoop(conn):
            eventloop.quit()
        self.connectionHandler.openConnection(address[0], address[1],
                stopEventLoop, stopEventLoop)
        self.runEventLoop()
        self.remoteSocket, address = server.accept()
        self.remoteSocket.setblocking(False)

    def testSend(self):
        data = 'abcabc' * 1024  * 64
        self.connectionHandler.sendData(data)
        self.received = httpclient.NetworkBuffer()
        def readData():
            try:
                readIn = self.remoteSocket.recv(1024 * 1024)
            except:
                readIn = ''
            self.received.addData(readIn)
            if self.received.length == len(data):
                eventloop.quit()
            else:
                eventloop.addTimeout(0.1, readData, 'test')
        eventloop.addTimeout(0.1, readData, 'test')
        self.runEventLoop()
        self.assert_(self.received.read() == data)

    def testRead(self):
        self.connectionHandler.changeState('foo')
        self.remoteSocket.send('abc')
        self.runEventLoop(timeout=1)
        self.assertEquals(self.connectionHandler.fooData, 'abc')
        self.connectionHandler.changeState('bar')
        self.remoteSocket.send('def')
        self.runEventLoop(timeout=1)
        self.assertEquals(self.connectionHandler.barData, 'def')
        self.remoteSocket.send('ghi')
        self.connectionHandler.changeState('noread')
        self.runEventLoop(timeout=0.1, timeoutNormal=True)
        self.assertEquals(self.connectionHandler.fooData, 'abc')
        self.assertEquals(self.connectionHandler.barData, 'def')
        self.connectionHandler.changeState('foo')
        self.runEventLoop(timeout=1)
        self.assertEquals(self.connectionHandler.fooData, 'abcghi')

    def testClose(self):
        self.connectionHandler.closeConnection()
        self.assert_(not self.connectionHandler.stream.isOpen())
        # second close shouldn't throw any exceptions
        self.connectionHandler.closeConnection()

    def testRemoteClose(self):
        self.connectionHandler.changeState('foo')
        self.remoteSocket.shutdown(socket.SHUT_WR)
        self.runEventLoop()
        self.assertEquals(self.connectionHandler.gotHandleClose, True)

    def testRemoteClose2(self):
        self.remoteSocket.shutdown(socket.SHUT_RD)
        self.remoteSocket.close()
        # NOTE, we have to send enough data so that the OS won't buffer the
        # entire send call.  Otherwise we may miss that the socket has closed.
        self.connectionHandler.sendData("A" * 1024 * 1024)
        self.runEventLoop(timeout=1)
        self.assertEquals(self.connectionHandler.gotHandleClose, True)

    def testString(self):
        # just make sure it doesn't throw an exception
        str(self.connectionHandler)

class HTTPClientTestBase(AsyncSocketTest):
    def setUp(self):
        AsyncSocketTest.setUp(self)
        self.testRequest = TestingHTTPConnection()
        self.testRequest.openConnection('foo.com', 80)
        self.testRequest.sendRequest(self.callback, self.errback, 
                method='GET', path='/bar/baz;123?a=b') 
        self.authDelegate = TestingAuthDelegate()
        dialogs.setDelegate(self.authDelegate)

    def tearDown(self):
        # clear out any HTTPAuth objects in there
        AsyncSocketTest.tearDown(self)

class HTTPClientTest(HTTPClientTestBase):
    def testScheme(self):
        conn = httpclient.HTTPConnection()
        self.assertEquals(conn.scheme, 'http')

    def testRequestLine(self):
        self.assertEquals(self.testRequest.output.split("\r\n")[0],
                'GET /bar/baz;123?a=b HTTP/1.1')

    def testStatusLine(self):
        self.testRequest.handleData("HTTP/1.0 200 OK\r\n")
        self.assertEquals(self.testRequest.version, 'HTTP/1.0')
        self.assertEquals(self.testRequest.status, 200)
        self.assertEquals(self.testRequest.reason, 'OK')

    def testStatusLine2(self):
        self.testRequest.handleData("HTTP/1.1 404 Not Found\r\n")
        self.assertEquals(self.testRequest.version, 'HTTP/1.1')
        self.assertEquals(self.testRequest.status, 404)
        self.assertEquals(self.testRequest.reason, 'Not Found')

    def testBadStatusLine(self):
        self.testRequest.handleData("HTTP/0.9 200 OK\r\n")
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.BadStatusLine))

    def testBadStatusLine2(self):
        self.testRequest.handleData("HTTP/1.0 641 OK\r\n")
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.BadStatusLine))

    def testBadStatusLine3(self):
        self.testRequest.handleData("HTTP/1.0 TwoHundred OK\r\n")
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.BadStatusLine))

    def testNoReason(self):
        self.testRequest.handleData("HTTP/1.0 200\r\n")
        self.assertEquals(self.testRequest.version, 'HTTP/1.0')
        self.assertEquals(self.testRequest.status, 200)
        self.assertEquals(self.testRequest.reason, '')

    def testTryToHandleHTTP0Point9(self):
        self.testRequest.handleData("StartOfThebody\r\n")
        self.assertEquals(self.testRequest.version, 'HTTP/0.9')
        self.assertEquals(self.testRequest.status, 200)
        self.assertEquals(self.testRequest.reason, '')
        self.assertEquals(self.testRequest.buffer.read(),
                "StartOfThebody\r\n")

    fakeResponse = """\
HTTP/1.0 200 OK\r
Content-Type: text/plain; charset=ISO-8859-1\r
Last-Modified: Wed, 10 May 2006 22:30:33 GMT\r
Date: Wed, 10 May 2006 22:38:39 GMT\r
X-Cache: HIT from pcf2.pcf.osuosl.org\r
Server: Apache\r
Content-Length: 14\r
\r
HELLO: WORLD\r\n"""

    def testBasicHeaders(self):
        self.testRequest.handleData(self.fakeResponse)
        self.testRequest.handleClose(socket.SHUT_RD)
        headers = self.testRequest.headers
        self.assertEquals(headers['x-cache'], 'HIT from pcf2.pcf.osuosl.org')
        self.assertEquals(headers['server'], 'Apache')
        self.assertEquals(headers['last-modified'], 
            'Wed, 10 May 2006 22:30:33 GMT')
        self.assertEquals(headers['content-length'], '14')
        self.assertEquals(self.testRequest.contentLength, 14)
        self.assertEquals(headers['date'], 'Wed, 10 May 2006 22:38:39 GMT')
        self.assertEquals(headers['content-type'], 
            'text/plain; charset=ISO-8859-1')

    def testCallbackError(self):
        self.fakeCallbackError = True
        self.failedCalled = False
        def fakeFailed(*args, **kwargs):
            self.failedCalled = True
        oldFailed = util.failed
        util.failed = fakeFailed
        try:
            self.testRequest.handleData(self.fakeResponse)
            self.testRequest.handleClose(socket.SHUT_RD)
        finally:
            util.failed = oldFailed
        self.assert_(self.failedCalled)

    def testBadHeader(self):
        self.testRequest.handleData("HTTP/1.0 200 OK\r\n")
        self.testRequest.handleData("FOO:\r\n")
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.BadHeaderLine))

    def testHeaderContinuation(self):
        self.testRequest.handleData("HTTP/1.0 200 OK\r\n")
        self.testRequest.handleData("Cont\r\n")
        self.testRequest.handleData(" ent-Type: text/plain\r\n")
        self.assertEquals(self.testRequest.headers['content-type'],
                'text/plain')

    def testHeaderJoin(self):
        self.testRequest.handleData("HTTP/1.0 200 OK\r\n")
        self.testRequest.handleData("x-test-list: 1\r\n")
        self.testRequest.handleData("x-test-list: 2\r\n")
        self.testRequest.handleData("x-test-list: 3\r\n")
        self.testRequest.handleData("x-test-list: 4\r\n")
        self.testRequest.handleData("\r\n")
        self.assertEquals(self.testRequest.headers['x-test-list'], '1,2,3,4')

    def testBadHeaderContinuation(self):
        self.testRequest.handleData("HTTP/1.0 200 OK\r\n")
        self.testRequest.handleData("IShouldBeContinued\r\n")
        self.testRequest.handleData("\r\n")
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.BadHeaderLine))

    def testWillClose(self):
        self.testRequest.handleData(startResponse(
            headers={'Content-Length': 128}))
        self.assertEquals(self.testRequest.willClose, False)

    def testWillClose2(self):
        self.testRequest.handleData(startResponse(
            headers={'Transfer-Encoding':'chunked'}))
        self.assertEquals(self.testRequest.willClose, False)

    def testWillClose3(self):
        self.testRequest.handleData(startResponse(version='1.0',
            headers={'Content-Length': 128}))
        # HTTP1.0 connections always close
        self.assertEquals(self.testRequest.willClose, True)

    def testWillClose4(self):
        self.testRequest.handleData(startResponse())
        # No content-length and not chunked, we need to close
        self.assertEquals(self.testRequest.willClose, True)

    def testWillClose5(self):
        self.testRequest.handleData(startResponse(
                headers={'Connection': 'close', 'Content-Length': 128}))
        self.assertEquals(self.testRequest.willClose, True)

    def testWillClose6(self):
        self.testRequest.handleData(startResponse(
                headers={'Connection': 'CLoSe', 'Content-Length': 128}))
        self.assertEquals(self.testRequest.willClose, True)

    def testPipeline(self):
        self.assertEqual(self.testRequest.pipelinedRequest, None)
        self.testRequest.handleData(startResponse(
            headers={'Content-Length': 128}))
        self.assertEquals(self.testRequest.canSendRequest(), True)
        self.testRequest.sendRequest(self.callback, self.errback,
                path="/pipelined/path")
        self.assertEquals(self.testRequest.pipelinedRequest[5],
                '/pipelined/path')
        self.testRequest.handleData('a' * 128)
        self.assert_(self.callbackCalled)
        self.assertEquals(self.data['body'], 'a' * 128)
        self.assertEquals(self.testRequest.state, 'response-status')
        self.assertEquals(self.testRequest.path, '/pipelined/path')
        self.assertEquals(self.testRequest.pipelinedRequest, None)

    def testBadPipeline(self):
        self.testRequest.handleData(startResponse())
        # no content length means we can't pipeline a request
        self.assertEquals(self.testRequest.canSendRequest(), False)
        self.assertRaises(httpclient.NotReadyToSendError,
            self.testRequest.sendRequest, self.callback, self.errback)

    def testPipelineNeverStarted(self):
        self.pipelineError = None
        self.testRequest.handleData(startResponse(
            headers={'Content-Length': 128}))
        def pipelineErrback(error):
            self.pipelineError = error
        self.testRequest.sendRequest(self.callback, pipelineErrback,
                path="/pipelined/path")
        self.testRequest.handleClose(socket.SHUT_RDWR)
        self.assert_(isinstance(self.pipelineError,
            httpclient.PipelinedRequestNeverStarted))

    def testPipelineRetry(self):
        testPool = TestingHTTPConnectionPool()
        testPool.MAX_CONNECTIONS_PER_SERVER = 1
        url = "http://www.foo.com/"
        client = httpclient.HTTPClient(url, self.callback, self.errback) 
        client.connectionPool = testPool
        client.startRequest()
        url = "http://www.foo.com/2"
        self.pipelineResponse = self.pipelineError = None
        def pipelineCallback(response):
            self.pipelineResponse = response
        def pipelineErrback(error):
            self.pipelineError = error
        conn = testPool.getConnection('http', 'www.foo.com')
        conn.handleData(startResponse(headers={'Content-Length': 128}))
        client2 = httpclient.HTTPClient(url, pipelineCallback, 
                pipelineErrback) 
        client2.connectionPool = testPool
        client2.startRequest()
        # client2 should be pipelined here
        conn.closeConnection()
        conn.handleClose(socket.SHUT_RD)
        self.assertEquals(self.pipelineError, None)
        self.assertEquals(self.pipelineResponse, None)
        conn = testPool.getConnection('http', 'www.foo.com')
        conn.handleData(self.fakeResponse)
        self.assertEquals(self.pipelineResponse['body'], "HELLO: WORLD\r\n")

    def testContentLengthHandling(self):
        self.testRequest.handleData(startResponse(
            headers={'Content-Length': '5'}))
        self.testRequest.handleData("12345EXTRASTUFF")
        self.assertEquals(self.testRequest.body, '12345')

    def testTransferEncodingTrumpsContentLength(self):
        self.testRequest.handleData(startResponse(
            headers={'Content-Length': '5', 'Transfer-Encoding': 'chunked'}))
        self.assertEquals(self.testRequest.contentLength, None)

    def testNoBody(self):
        self.testRequest.handleData(startResponse(status=204))
        self.assertEquals(self.testRequest.state, 'closed')
        self.assertEquals(self.testRequest.body, '')

    def testNoBody2(self):
        self.testRequest.handleData(startResponse(status=123))
        self.assertEquals(self.testRequest.state, 'closed')
        self.assertEquals(self.testRequest.body, '')

    def testNoBody3(self):
        self.testRequest.method='HEAD'
        self.testRequest.handleData(startResponse())
        self.assertEquals(self.testRequest.state, 'closed')
        self.assertEquals(self.testRequest.body, '')

    def testNoBody4(self):
        self.testRequest.bodyDataCallback = lambda data: 0
        self.testRequest.handleData(startResponse(
            headers={"Content-Length":'0'}))
        self.assertEquals(self.testRequest.state, 'ready')
        self.assertEquals(self.testRequest.body, '')

    def testSplitUpMessage(self):
        data = self.fakeResponse
        for cutoff in [3, 6, 10, 4, 100, 52]:
            self.testRequest.handleData(data[:cutoff])
            data = data[cutoff:]
        self.testRequest.handleData(data)
        self.testRequest.handleClose(socket.SHUT_RD)
        self.assertEquals(self.testRequest.version, 'HTTP/1.0')
        self.assertEquals(self.testRequest.status, 200)
        self.assertEquals(self.testRequest.reason, 'OK')
        self.assertEquals(self.testRequest.headers['server'],
                'Apache')
        self.assertEquals(self.testRequest.body, 'HELLO: WORLD\r\n')

    def testOneChunk(self):
        self.testRequest.handleData(self.fakeResponse)
        self.testRequest.handleClose(socket.SHUT_RD)
        self.assertEquals(self.testRequest.version, 'HTTP/1.0')
        self.assertEquals(self.testRequest.status, 200)
        self.assertEquals(self.testRequest.reason, 'OK')
        self.assertEquals(self.testRequest.headers['server'],
                'Apache')
        self.assertEquals(self.testRequest.body, 'HELLO: WORLD\r\n')

    def testBadChunkSize(self):
        self.testRequest.handleData(startResponse(
            headers={'Transfer-Encoding': 'chunked'}))
        self.testRequest.handleData("Fifty\r\n")
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.BadChunkSize))

    def testIgnoreChunkExtensions(self):
        self.testRequest.handleData(startResponse(
            headers={'Transfer-Encoding': 'chunked'}))
        self.testRequest.handleData("ff ;ext1=2 ; ext3=4\r\n")
        self.assert_(not self.errbackCalled)
        self.assertEquals(self.testRequest.state, 'chunk-data')
        self.assertEquals(self.testRequest.chunkSize, 255)

    def testChunkWithoutCRLF(self):
        self.testRequest.handleData(startResponse(
            headers={'Transfer-Encoding': 'chunked'}))
        self.testRequest.handleData("5\r\n")
        self.testRequest.handleData("12345RN") # "RN" should have been "\r\n"
        self.assert_(self.errback)
        self.assert_(isinstance(self.data, httpclient.CRLFExpected))

    def testPrematureClose(self):
        data = self.fakeResponse
        self.testRequest.handleData(data[:123])
        self.testRequest.handleClose(socket.SHUT_RD)
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.ServerClosedConnection))

    def testRealRequest(self):
        url = 'http://participatoryculture.org/democracytest/normalpage.txt'
        httpclient.grabURL(url, self.callback, self.errback)
        self.runEventLoop(timeout=10)
        self.assert_(self.callbackCalled)
        self.assertEquals(self.data['body'], "I AM A NORMAL PAGE\n")

    def testConnectionFailure(self):
        httpclient.grabURL("http://slashdot.org:123123", self.callback, 
                self.errback)
        self.runEventLoop()
        self.assert_(self.errbackCalled)
        self.assertEquals(self.data.__class__, httpclient.ConnectionError)

    def testMultipleRequests(self):
        def middleCallback(data):
            self.firstData = data
            req.sendRequest(self.callback, self.errback, method='GET', path='/')

        url = 'http://jigsaw.w3.org/HTTP/'
        req = httpclient.HTTPConnection()
        def stopEventLoop(conn):
            eventloop.quit()
        req.openConnection('www.google.com', 80, stopEventLoop, stopEventLoop)
        self.runEventLoop()
        req.sendRequest(middleCallback, self.errback, method='GET', path='/')
        self.runEventLoop()
        self.assertEquals(self.firstData['body'], self.data['body'])

    def testChunkedData(self):
        url = 'http://jigsaw.w3.org/HTTP/ChunkedScript'
        httpclient.grabURL(url, self.callback, self.errback)
        self.runEventLoop(timeout=5)
        header = """\
This output will be chunked encoded by the server, if your client is HTTP/1.1
Below this line, is 1000 repeated lines of 0-9.
-------------------------------------------------------------------------"""
        bodyLine = """\
01234567890123456789012345678901234567890123456789012345678901234567890"""
        lines = self.data['body'].split('\n')
        headerLines = header.split('\n')
        self.assertEquals(lines[0], headerLines[0])
        self.assertEquals(lines[1], headerLines[1])
        self.assertEquals(lines[2], headerLines[2])
        for x in range(3, 1003):
            self.assertEquals(lines[x], bodyLine)

    def testCookie(self):
        url = 'http://participatoryculture.org/democracytest/cookie.php'
        httpclient.grabURL(url, self.callback, self.errback)
        self.runEventLoop(timeout=5)
        self.assertEquals(len(self.data['cookies']),1)
        self.assert_(self.data['cookies'].has_key('DemocracyTestCookie'))
        self.assertEquals(self.data['cookies']['DemocracyTestCookie']['Value'], 'foobar')
        httpclient.grabURL(url, self.callback, self.errback,cookies = self.data['cookies'])
        self.runEventLoop(timeout=2)
        self.assertNotEqual(self.data['body'].find('DemocracyTestCookie:foobar'),-1)

    def testParseURL(self):
        (scheme, host, port, path) = \
                httpclient.parseURL("https://www.foo.com/abc;123?a=b#4")
        self.assertEquals(scheme, 'https')
        self.assertEquals(host, 'www.foo.com')
        self.assertEquals(port, 443)
        self.assertEquals(path, '/abc;123?a=b')
        (scheme, host, port, path) = \
                httpclient.parseURL("http://www.foo.com/abc;123?a=b#4")
        self.assertEquals(port, 80)
        (scheme, host, port, path) = \
                httpclient.parseURL("http://www.foo.com:5000/abc;123?a=b#4")
        self.assertEquals(port, 5000)
        # I guess some feeds have bad url, with a double port in them, test
        # that we handle these.
        (scheme, host, port, path) = \
                httpclient.parseURL("http://www.foo.com:123:123/abc;123?a=b#4")
        self.assertEquals(port, 123)

    def testBadScheme(self):
        self.assertRaises(ValueError, httpclient.grabURL,
                "rstp://www.foo.com/", self.callback, self.errback)

    def checkRedirect(self, url, redirectUrl, updatedUrl, **extra):
        httpclient.grabURL(url, self.callback, self.errback, **extra)
        self.runEventLoop(timeout=20)
        self.assertEquals(self.data['redirected-url'], redirectUrl)
        self.assertEquals(self.data['updated-url'], updatedUrl)

    def test307Redirect(self):
        self.checkRedirect('http://jigsaw.w3.org/HTTP/300/307.html',
                'http://jigsaw.w3.org/HTTP/300/Overview.html',
                'http://jigsaw.w3.org/HTTP/300/307.html')

    def test301Redirect(self):
        self.checkRedirect('http://jigsaw.w3.org/HTTP/300/301.html',
                'http://jigsaw.w3.org/HTTP/300/Overview.html',
                'http://jigsaw.w3.org/HTTP/300/Overview.html')

    def test302Redirect(self):
        self.checkRedirect('http://jigsaw.w3.org/HTTP/300/302.html',
                'http://jigsaw.w3.org/HTTP/300/Overview.html',
                'http://jigsaw.w3.org/HTTP/300/302.html')

    def test303Redirect(self):
        self.checkRedirect('http://jigsaw.w3.org/HTTP/300/Go_303',
                'http://jigsaw.w3.org/HTTP/300/303_ok.html',
                'http://jigsaw.w3.org/HTTP/300/Go_303', method="POST")
        self.assertEquals(self.data['method'], 'GET')

    def testMultipleRedirect(self):
        # The redirect chain is:
        # redirect.php PERMAMENT REDIRECT -> redirect2.php
        # redirect2.php PERMAMENT REDIRECT -> redirect3.php
        # redirect3.php TEMORARY REDIRECT -> end.txt
        # The updated-url should be redirect3.php, since that it was the 1st
        # redirect that was temporary
        self.checkRedirect(
                'http://participatoryculture.org/democracytest/redirect.php',
                'http://participatoryculture.org/democracytest/end.txt',
                'http://participatoryculture.org/democracytest/redirect3.php')

    def testRedirectLimit(self):
        url = 'http://participatoryculture.org/democracytest/redirect.php'
        client = httpclient.HTTPClient(url, self.callback, self.errback) 
        client.MAX_REDIRECTS = 2
        client.startRequest()
        self.runEventLoop()
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.UnexpectedStatusCode))

    def testGetFilenameFromResponse(self):
        client = httpclient.HTTPClient('http://www.foo.com', self.callback,
                self.errback)
        def getIt(path, cd=None):
            response = {'path': path}
            if cd:
                response['content-disposition'] = cd
            return client.getFilenameFromResponse(response)
        self.assertEquals("unknown", getIt("/"))
        self.assertEquals("index.html", getIt("/index.html"))
        self.assertEquals("index.html", getIt("/path/path2/index.html"))
        self.assertEquals("path2", getIt("/path/path2/"))
        self.assertEquals("myfile.txt", getIt("/", 'filename="myfile.txt"'))
        self.assertEquals("myfile.txt", getIt("/",
            'filename="myfile.txt"; size=45'))
        self.assertEquals("myfile.txt", getIt("/",
            ' filename =  "myfile.txt"'))
        self.assertEquals("myfile.txt", getIt("/", 'filename=myfile.txt'))
        self.assertEquals("myfile.txt", getIt("/index.html",
            cd='filename="myfile.txt"'))
        self.assertEquals("lots.of.extensions",
            getIt("/", 'filename="lots.of.extensions"'))
        self.assertEquals("uncleanfilename", 
                getIt("/index", 'filename="\\un/cl:ean*fi?lena<m>|e"'))
        self.assertEquals("uncleanfilename2", 
                getIt('/uncl*ean"fil?"ena|m""e2"'))

    def testGetCharsetFromResponse(self):
        client = httpclient.HTTPClient('http://www.foo.com', self.callback,
                self.errback)
        def getIt(contentType):
            if contentType:
                response = {'content-type': contentType}
            else:
                response = {}
            return client.getCharsetFromResponse(response)
        self.assertEquals('iso-8859-1', getIt(None))
        self.assertEquals('iso-8859-1', getIt('gabaldigook'))
        self.assertEquals('iso-8859-1', getIt("text/html"))
        self.assertEquals('utf-8', getIt("text/html; charset=utf-8"))
        self.assertEquals('utf-8', getIt("text/html; charset = utf-8"))
        self.assertEquals('utf-8', 
                getIt("text/html; charset=utf-8; extraparam=2"))

    def testHeaderCallback(self):
        def headerCallback(response):
            self.headerResponse = copy(response)
            self.callbackCalledInHeaderCallback = self.callbackCalled
            self.errbackCalledInHeaderCallback = self.errbackCalled
            eventloop.quit()
        url = 'http://participatoryculture.org/democracytest/normalpage.txt'
        httpclient.grabURL(url, self.callback, self.errback,
                headerCallback=headerCallback)
        self.runEventLoop()
        self.assert_(not self.callbackCalledInHeaderCallback)
        self.assert_(not self.errbackCalledInHeaderCallback)
        self.assertEquals(self.headerResponse['content-type'], 
                'text/plain; charset=ISO-8859-1')
        self.assertEquals(self.headerResponse['body'], None)

    def testHeaderCallbackCancel(self):
        def headerCallback(response):
            httpclient.cancelRequest(reqId)
            eventloop.quit()
        url = 'http://participatoryculture.org/democracytest/normalpage.txt'
        reqId = httpclient.grabURL(url, self.callback, self.errback,
                headerCallback=headerCallback)
        self.failedCalled = False
        def fakeFailed(*args, **kwargs):
            self.failedCalled = True
        oldFailed = util.failed
        util.failed = fakeFailed
        self.runEventLoop()
        util.failed = oldFailed
        self.assert_(not self.callbackCalled)
        self.assert_(not self.errbackCalled)
        self.assert_(not self.failedCalled)

    def testBodyDataCallbackCancel(self):
        def bodyDataCallback(response):
            httpclient.cancelRequest(reqId)
            eventloop.quit()
        url = 'http://participatoryculture.org/democracytest/normalpage.txt'
        reqId = httpclient.grabURL(url, self.callback, self.errback,
                bodyDataCallback=bodyDataCallback)
        self.failedCalled = False
        def fakeFailed(*args, **kwargs):
            self.failedCalled = True
        oldFailed = util.failed
        util.failed = fakeFailed
        self.runEventLoop()
        util.failed = oldFailed
        self.assert_(not self.callbackCalled)
        self.assert_(not self.errbackCalled)
        self.assert_(not self.failedCalled)

    def testBodyDataCallback(self):
        self.lastSeen = None
        def bodyDataCallback(data):
            self.lastSeen = data
        self.testRequest.bodyDataCallback = bodyDataCallback
        self.testRequest.handleData(startResponse(
                    headers={'content-length':'20'}))
        self.assertEquals(self.lastSeen, None)
        self.testRequest.handleData("12345")
        self.assertEquals(self.lastSeen, "12345")
        self.testRequest.handleData("1234567890")
        self.assertEquals(self.lastSeen, "1234567890")
        self.testRequest.handleData("1234567890")
        self.assertEquals(self.lastSeen, "12345")

    def testBodyDataCallbackChunked(self):
        self.lastSeen = None
        def bodyDataCallback(data):
            self.lastSeen = data
        self.testRequest.bodyDataCallback = bodyDataCallback
        self.testRequest.handleData(startResponse(
            headers={'transfer-encoding': 'chunked'}))
        self.testRequest.handleData("5\r\nHI")
        self.assertEquals(self.lastSeen, "HI")
        self.testRequest.handleData("BEN\r\n")
        self.assertEquals(self.lastSeen, "BEN")
        self.testRequest.handleData("A\r\n")
        self.assertEquals(self.lastSeen, "BEN")
        self.testRequest.handleData("1234567890\r\n0")
        self.assertEquals(self.lastSeen, "1234567890")
        self.assert_(not self.callbackCalled)
        self.testRequest.handleData("\r\n\r\n")
        self.assert_(self.callbackCalled)

    def testBodyDataCallbackRealRequest(self):
        url = 'http://participatoryculture.org/democracytest/normalpage.txt'
        self.gotData = ''
        def bodyDataCallback(data):
            self.gotData += data
            if self.gotData == 'I AM A NORMAL PAGE\n':
                eventloop.quit()

        httpclient.grabURL(url, self.callback, self.errback,
                bodyDataCallback=bodyDataCallback)
        self.runEventLoop()
        self.assertEquals(self.gotData, 'I AM A NORMAL PAGE\n')

    def testAuth(self):
        self.authDelegate.addLogin('ben', 'baddpassword')
        self.authDelegate.addLogin('guest', 'guest')
        url = 'http://jigsaw.w3.org/HTTP/Basic/'
        client = httpclient.HTTPClient(url, self.callback, self.errback)
        client.startRequest()
        self.runEventLoop()
        self.assert_(self.callbackCalled)
        self.assertEquals(self.data['status'], 200)
        self.assertEquals(client.authAttempts, 2)

    def testBadAuth(self):
        self.authDelegate.addLogin('baduser', 'baddpass')
        self.authDelegate.addLogin('anotherbadtry', 'god')
        self.authDelegate.addLogin('billgates', 'password')
        url = 'http://jigsaw.w3.org/HTTP/Basic/'
        client = httpclient.HTTPClient(url, self.callback, self.errback)
        client.startRequest()
        self.runEventLoop()
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.AuthorizationFailed))
        self.assertEquals(client.authAttempts, 3)

    def testDigestAuth(self):
        # we don't support digest authorization yet, make sure we get the
        # right errback at least
        url = 'http://jigsaw.w3.org/HTTP/Digest/'
        client = httpclient.HTTPClient(url, self.callback, self.errback)
        client.startRequest()
        self.runEventLoop()
        self.assert_(self.errbackCalled)
        self.assert_(isinstance(self.data, httpclient.AuthorizationFailed))
        self.assertEquals(client.authAttempts, 0)

class HTTPConnectionPoolTest(EventLoopTest):
    def setUp(self):
        self.pool = TestingHTTPConnectionPool()
        super(HTTPConnectionPoolTest, self).setUp()

    def addRequest(self, url):
        return self.pool.addRequest((lambda data: 0), (lambda error: 0),
                None, None, url, "GET", {})

    def checkCounts(self, activeCount, freeCount, pendingCount):
        self.assertEquals(self.pool.activeConnectionCount, activeCount)
        self.assertEquals(self.pool.freeConnectionCount, freeCount)
        realFreeCount = realActiveCount = 0
        for key, conns in self.pool.connections.items():
            realFreeCount += len(conns['free'])
            realActiveCount += len(conns['active'])
        self.assertEquals(realActiveCount, activeCount)
        self.assertEquals(realFreeCount, freeCount)
        self.assertEquals(pendingCount, len(self.pool.pendingRequests))

    def testNormalUsage(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.bar.com/")
        self.addRequest("http://www.foo.com/2")
        self.addRequest("http://www.google.com/")
        self.checkCounts(4, 0, 0)

    def testOpenConnectionFailed(self):
        # this is pretty dirty, but it was the only way I could think of to
        # simulate this
        self.pool = httpclient.HTTPConnectionPool()
        def stopEventLoop(error):
            eventloop.quit()
        self.pool.addRequest(stopEventLoop, stopEventLoop,
                None, None, "http://3:-1/", "GET", {})
        self.runEventLoop()
        self.checkCounts(0, 0, 0)

    def testCounts(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.foo.com/2")
        self.addRequest("https://www.foo.com/")
        self.checkCounts(3, 0, 0)
        self.pool.finishConnection('http', 'www.foo.com')
        self.checkCounts(2, 1, 0)
        self.pool.closeConnection('https', 'www.foo.com')
        self.checkCounts(1, 1, 0)
        self.pool.closeConnection('http', 'www.foo.com', type='free')
        self.checkCounts(1, 0, 0)

    def testServerLimit(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.foo.com/2")
        self.addRequest("https://www.foo.com/")
        self.checkCounts(3, 0, 0)
        self.addRequest("http://www.foo.com/3")
        self.checkCounts(3, 0, 1)
        self.pool.assertConnectionNotStarted('http://www.foo.com/3')
        self.pool.finishConnection('https', 'www.foo.com')
        self.pool.assertConnectionNotStarted('http://www.foo.com/3')
        self.pool.finishConnection('http', 'www.foo.com')
        self.pool.assertConnectionStarted('http://www.foo.com/3')
        self.checkCounts(2, 1, 0)

    def testTotalLimit(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.foo.com/2")
        self.addRequest("https://www.bar.com/")
        self.addRequest("http://www.bar.com/2")
        self.addRequest("http://www.baz.com/")
        self.addRequest("http://www.froz.com/")
        self.checkCounts(4, 0, 2)
        self.pool.assertConnectionNotStarted('http://www.baz.com/')
        self.pool.assertConnectionNotStarted('http://www.froz.com/')
        self.pool.finishConnection('http', 'www.foo.com')
        self.pool.assertConnectionStarted('http://www.baz.com/')
        self.pool.assertConnectionNotStarted('http://www.froz.com/')
        self.pool.finishConnection('http', 'www.bar.com')
        self.pool.assertConnectionStarted('http://www.froz.com/')

    def testCancelActive(self):
        reqids = []
        reqids.append(self.addRequest("http://www.foo.com/"))
        reqids.append(self.addRequest("http://www.foo.com/2"))
        reqids.append(self.addRequest("https://www.bar.com/"))
        reqids.append(self.addRequest("http://www.bar.com/2"))
        self.checkCounts(4, 0, 0)
        for x in xrange(4):
            self.pool.cancelRequest(reqids[x])
            self.checkCounts(3-x, 0, 0)

    def testCancelFree(self):
        reqid = self.addRequest("http://www.foo.com/")
        self.checkCounts(1, 0, 0)
        self.pool.finishConnection('http', 'www.foo.com')
        self.checkCounts(0, 1, 0)
        self.pool.cancelRequest(reqid)
        self.checkCounts(0, 0, 0)

    def testCancelPending(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.foo.com/")
        reqids = []
        for x in xrange(10):
            reqids.append(self.addRequest("http://www.foo.com/"))
        self.checkCounts(2, 0, 10)
        for x in xrange(10):
            self.pool.cancelRequest(reqids[x])
            self.checkCounts(2, 0, 9-x)

    def cancelMixed(self):
        req1 = reqids.append(self.addRequest("http://www.foo.com/"))
        req2 = reqids.append(self.addRequest("http://www.foo.com/2"))
        req3 = reqids.append(self.addRequest("http://www.foo.com/3"))
        req4 = reqids.append(self.addRequest("http://www.bar.com/"))
        req5 = reqids.append(self.addRequest("http://www.bar.com/2"))
        req6 = reqids.append(self.addRequest("http://www.bar.com/3"))
        self.checkCounts(4, 0, 2)
        self.pool.cancelRequest(req1)
        self.checkCounts(4, 0, 1)
        self.pool.cancelRequest(req6)
        self.checkCounts(4, 0, 0)
        self.pool.cancelRequest(req3)
        self.checkCounts(3, 0, 0)
        self.pool.cancelRequest(req5)
        self.checkCounts(2, 0, 0)
        self.pool.cancelRequest(req2)
        self.checkCounts(1, 0, 0)
        self.pool.cancelRequest(req4)
        self.checkCounts(0, 0, 0)

    def testBothLimits(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.foo.com/2")
        self.addRequest("http://www.foo.com/3")
        self.checkCounts(2, 0, 1)
        self.pool.assertConnectionNotStarted('http://www.foo.com/3')
        self.addRequest("https://www.bar.com/")
        self.addRequest("http://www.bar.com/2")
        self.addRequest("http://www.baz.com/")
        self.checkCounts(4, 0, 2)
        self.pool.assertConnectionNotStarted('http://www.baz.com/')
        self.pool.finishConnection('http', 'www.bar.com')
        self.pool.finishConnection('https', 'www.bar.com')
        self.checkCounts(3, 1, 1)
        # still hitting the limit on foo.com, but we have space for the
        # baz.com request
        self.pool.assertConnectionStarted('http://www.baz.com/')
        self.pool.assertConnectionNotStarted('http://www.foo.com/3')
        self.pool.finishConnection('http', 'www.foo.com')
        self.checkCounts(3, 1, 0)
        self.pool.assertConnectionStarted('http://www.foo.com/3')
        self.addRequest("http://www.ben.com/")
        self.checkCounts(4, 0, 0)
        self.pool.assertConnectionStarted('http://www.ben.com/')
        self.addRequest("http://www.ben.com/2")
        self.addRequest("http://www.foo.com/4")
        self.checkCounts(4, 0, 2)
        self.pool.assertConnectionNotStarted('http://www.ben.com/2')
        self.pool.assertConnectionNotStarted('http://www.foo.com/4')
        self.pool.finishConnection('http', 'www.foo.com')
        # ben.com is higher in the line, make sure we remove a foo.com
        # connection to let it in
        self.pool.assertConnectionStarted('http://www.ben.com/2')
        self.pool.assertConnectionNotStarted('http://www.foo.com/4')
        fooConns = self.pool.connections['http:www.foo.com:80']
        self.assertEquals(len(fooConns['active']), 1)
        self.assertEquals(len(fooConns['free']), 0)

    def testDropTheLRU(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.foo.com/2")
        self.addRequest("http://www.bar.com/")
        self.addRequest("http://www.baz.com/")
        self.pool.finishConnection('http', 'www.baz.com')
        self.pool.finishConnection('http', 'www.bar.com')
        self.pool.finishConnection('http', 'www.foo.com')
        self.pool.finishConnection('http', 'www.foo.com')
        # the next addRequest will cause us to drop one of our free
        # connections.  This should be baz.com, since it was the 1st to
        # finish.
        self.addRequest("http://www.baz.com/")
        self.pool.assertConnectionStarted('http://www.baz.com/')
        bazConns = self.pool.connections['http:www.baz.com:80']
        self.assertEquals(len(bazConns['free']), 0)

    def testCleanup(self):
        self.addRequest("http://www.foo.com/")
        self.addRequest("http://www.bar.com/")
        self.addRequest("http://www.baz.com/")
        self.addRequest("http://www.qux.com/")
        self.pool.finishConnection('http', 'www.foo.com')
        self.pool.finishConnection('http', 'www.bar.com')
        self.pool.finishConnection('http', 'www.baz.com')
        foo = self.pool.getConnection('http', 'www.foo.com', type='free')
        bar = self.pool.getConnection('http', 'www.bar.com', type='free')
        baz = self.pool.getConnection('http', 'www.baz.com', type='free')
        qux = self.pool.getConnection('http', 'www.qux.com', type='active')
        now = clock()
        foo.idleSince = now-301
        bar.idleSince = now-299
        self.pool.cleanupPool()
        # foo timeout out, bar and baz didn't time out, qux is active, so it
        # shouldn't be dropped
        self.assert_(not foo.socketOpen)
        self.assert_(bar.socketOpen)
        self.assert_(baz.socketOpen)
        self.assert_(qux.socketOpen)
        self.assert_('http:www.foo.com:80' not in self.pool.connections)
        self.assert_('http:www.bar.com:80' in self.pool.connections)
        self.assert_('http:www.baz.com:80' in self.pool.connections)
        self.assert_('http:www.qux.com:80' in self.pool.connections)
        qux.handleData(startResponse(headers={'Content-Length': 128}))
        # qux is now a free connection, but its idleSince is None
        self.pool.cleanupPool()
        self.assert_('http:www.qux.com:80' in self.pool.connections)

class HTTPSConnectionTest(HTTPClientTestBase):
    # We should have more tests here, but I have no idea how to fake SSL
    # connections.  So I just put some attemps to connect to an https site
    # The first https site I found was:
    # WAVE - Web Automated Verification of Enrollment
    # https://www.gibill.va.gov/wave/

    def testScheme(self):
        conn = httpclient.HTTPSConnection()
        self.assertEquals(conn.scheme, 'https')

    def testHTTPSConnection(self):
        conn = httpclient.HTTPSConnection()
        def handleOpen(data):
            conn.sendRequest(self.callback, self.errback, 
                    method="GET", path='/wave/')
        def handleError(error):
            eventloop.quit()
        conn.openConnection("www.gibill.va.gov", 443, handleOpen, handleError)
        self.runEventLoop()
        self.assert_(self.callbackCalled)
        self.assertEquals(self.data['status'], 200)

    def testGrabURL(self):
        httpclient.grabURL('https://www.gibill.va.gov/wave/', self.callback,
                self.errback)
        self.runEventLoop()
        self.assert_(self.callbackCalled)
        self.assertEquals(self.data['status'], 200)

class GrabURLTest(HTTPClientTestBase):
    def testStart(self):
        url = 'http://participatoryculture.org/democracytest/normalpage.txt'
        httpclient.grabURL(url, self.callback, self.errback)
        self.runEventLoop()
        self.origData = self.data
        httpclient.grabURL(url, self.callback, self.errback, start=4)
        self.runEventLoop()
        self.assertEquals(self.data['body'], self.origData['body'][4:])
        self.assertEquals(self.data['status'], 206)

    def testEtag(self):
        url = 'http://jigsaw.w3.org/HTTP/'
        httpclient.grabURL(url, self.callback, self.errback)
        self.runEventLoop()
        etag = self.data['etag']
        httpclient.grabURL(url, self.callback, self.errback, etag=etag)
        self.runEventLoop()
        self.assertEquals(self.data['status'], 304)
        self.assertEquals(self.data['body'], '')

    def testBadEtag(self):
        url = 'http://jigsaw.w3.org/HTTP/'
        httpclient.grabURL(url, self.callback, self.errback)
        self.runEventLoop()
        etag = "aaaaaaa:bbbbbbbb"
        firstBody = self.data['body']
        httpclient.grabURL(url, self.callback, self.errback, etag=etag)
        self.runEventLoop()
        self.assertEquals(self.data['status'], 200)
        self.assertEquals(self.data['body'], firstBody)

    def testModified(self):
        url = 'http://jigsaw.w3.org/HTTP/'
        httpclient.grabURL(url, self.callback, self.errback)
        self.runEventLoop()
        firstBody = self.data['body']
        modifiedTuple = rfc822.parsedate_tz(self.data['last-modified'])
        modifiedTime = rfc822.mktime_tz(modifiedTuple)
        modifiedTime -= 5
        httpclient.grabURL(url, self.callback, self.errback,
                modified=rfc822.formatdate(modifiedTime))
        self.runEventLoop()
        self.assertEquals(self.data['status'], 200)
        self.assertEquals(self.data['body'], firstBody)

