# -*- coding: utf-8 -*-
"""
test_http11.py
~~~~~~~~~~~~~~

Unit tests for hyper's HTTP/1.1 implementation.
"""
import os
import zlib

from collections import namedtuple
from io import BytesIO, StringIO

import pytest

import hyper
from hyper.http11.connection import HTTP11Connection
from hyper.http11.response import HTTP11Response
from hyper.http20.exceptions import ConnectionResetError
from hyper.common.headers import HTTPHeaderMap
from hyper.common.exceptions import ChunkedDecodeError
from hyper.compat import bytes, zlib_compressobj


class TestHTTP11Connection(object):
    def test_pycohttpparser_installs_correctly(self):
        # This test is a debugging tool: if pycohttpparser is being tested by
        # Travis, we need to confirm it imports correctly. Hyper will normally
        # hide the import failure, so let's discover it here.
        # Alternatively, if we are *not* testing with nghttp2, this test should
        # confirm that it's not available.
        if os.environ.get('HYPER_FAST_PARSE') == 'true':
            import pycohttpparser
        else:
            with pytest.raises(ImportError):
                import pycohttpparser

        assert True

    def test_initialization_no_port(self):
        c = HTTP11Connection('http2bin.org')

        assert c.host == 'http2bin.org'
        assert c.port == 80
        assert not c.secure

    def test_initialization_inline_port(self):
        c = HTTP11Connection('http2bin.org:443')

        assert c.host == 'http2bin.org'
        assert c.port == 443
        assert c.secure

    def test_initialization_separate_port(self):
        c = HTTP11Connection('localhost', 8080)

        assert c.host == 'localhost'
        assert c.port == 8080
        assert not c.secure

    def test_can_override_security(self):
        c = HTTP11Connection('localhost', 443, secure=False)

        assert c.host == 'localhost'
        assert c.port == 443
        assert not c.secure

    def test_basic_request(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = sock = DummySocket()

        c.request('GET', '/get', headers={'User-Agent': 'hyper'})

        expected = (
            b"GET /get HTTP/1.1\r\n"
            b"User-Agent: hyper\r\n"
            b"host: http2bin.org\r\n"
            b"\r\n"
        )
        received = b''.join(sock.queue)

        assert received == expected

    def test_request_with_bytestring_body(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = sock = DummySocket()

        c.request(
            'POST',
            '/post',
            headers=HTTPHeaderMap([('User-Agent', 'hyper')]),
            body=b'hi'
        )

        expected = (
            b"POST /post HTTP/1.1\r\n"
            b"User-Agent: hyper\r\n"
            b"content-length: 2\r\n"
            b"host: http2bin.org\r\n"
            b"\r\n"
            b"hi"
        )
        received = b''.join(sock.queue)

        assert received == expected

    def test_request_with_file_body(self):
        # Testing this is tricksy: in practice, we do this by passing a fake
        # file and monkeypatching out 'os.fstat'. This makes it look like a
        # real file.
        FstatRval = namedtuple('FstatRval', ['st_size'])
        def fake_fstat(*args):
            return FstatRval(16)

        old_fstat = hyper.http11.connection.os.fstat

        try:
            hyper.http11.connection.os.fstat = fake_fstat
            c = HTTP11Connection('http2bin.org')
            c._sock = sock = DummySocket()

            f = DummyFile(b'some binary data')
            c.request('POST', '/post',  body=f)

            expected = (
                b"POST /post HTTP/1.1\r\n"
                b"content-length: 16\r\n"
                b"host: http2bin.org\r\n"
                b"\r\n"
                b"some binary data"
            )
            received = b''.join(sock.queue)

            assert received == expected

        finally:
            # Put back the monkeypatch.
            hyper.http11.connection.os.fstat = old_fstat

    def test_request_with_generator_body(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = sock = DummySocket()
        def body():
            yield b'hi'
            yield b'there'
            yield b'sir'

        c.request('POST', '/post', body=body())

        expected = (
            b"POST /post HTTP/1.1\r\n"
            b"transfer-encoding: chunked\r\n"
            b"host: http2bin.org\r\n"
            b"\r\n"
            b"2\r\nhi\r\n"
            b"5\r\nthere\r\n"
            b"3\r\nsir\r\n"
            b"0\r\n\r\n"
        )
        received = b''.join(sock.queue)

        assert received == expected

    def test_content_length_overrides_generator(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = sock = DummySocket()
        def body():
            yield b'hi'
            yield b'there'
            yield b'sir'

        c.request(
            'POST', '/post', headers={b'content-length': b'10'}, body=body()
        )

        expected = (
            b"POST /post HTTP/1.1\r\n"
            b"content-length: 10\r\n"
            b"host: http2bin.org\r\n"
            b"\r\n"
            b"hitheresir"
        )
        received = b''.join(sock.queue)

        assert received == expected

    def test_chunked_overrides_body(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = sock = DummySocket()

        f = DummyFile(b'oneline\nanotherline')

        c.request(
            'POST',
            '/post',
            headers={'transfer-encoding': 'chunked'},
            body=f
        )

        expected = (
            b"POST /post HTTP/1.1\r\n"
            b"transfer-encoding: chunked\r\n"
            b"host: http2bin.org\r\n"
            b"\r\n"
            b"8\r\noneline\n\r\n"
            b"b\r\nanotherline\r\n"
            b"0\r\n\r\n"
        )
        received = b''.join(sock.queue)

        assert received == expected

    def test_get_response(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = sock = DummySocket()

        sock._buffer= BytesIO(
            b"HTTP/1.1 201 No Content\r\n"
            b"Connection: close\r\n"
            b"Server: Socket\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )

        r = c.get_response()

        assert r.status == 201
        assert r.reason == b'No Content'
        assert list(r.headers.iter_raw()) == [
            (b'Connection', b'close'),
            (b'Server', b'Socket'),
            (b'Content-Length', b'0')
        ]
        assert r.read() == b''

    def test_response_short_reads(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = sock = DummySocket()

        sock._buffer= BytesIO(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 15\r\n"
            b"\r\n"
            b"hellotherechamp"
        )

        r = c.get_response()

        assert r.status == 200
        assert r.reason == b'OK'
        assert r.read(5) == b'hello'
        assert r.read(5) == b'there'
        assert r.read(5) == b'champ'
        assert r.read(5) == b''

    def test_request_with_unicodestring_body(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = DummySocket()

        with pytest.raises(ValueError):
            c.request(
                'POST',
                '/post',
                headers=HTTPHeaderMap([('User-Agent', 'hyper')]),
                body=u'hi'
            )

    def test_request_with_file_body_in_text_mode(self):
        # Testing this is tricksy: in practice, we do this by passing a fake
        # file and monkeypatching out 'os.fstat'. This makes it look like a
        # real file.
        FstatRval = namedtuple('FstatRval', ['st_size'])
        def fake_fstat(*args):
            return FstatRval(16)

        old_fstat = hyper.http11.connection.os.fstat

        try:
            hyper.http11.connection.os.fstat = fake_fstat
            c = HTTP11Connection('http2bin.org')
            c._sock = DummySocket()

            f = DummyFile(b'')
            f.buffer = StringIO(u'some binary data')

            with pytest.raises(ValueError):
                c.request('POST', '/post',  body=f)
        finally:
            # Put back the monkeypatch.
            hyper.http11.connection.os.fstat = old_fstat

    def test_request_with_unicode_generator_body(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = DummySocket()
        def body():
            yield u'hi'
            yield u'there'
            yield u'sir'

        with pytest.raises(ValueError):
            c.request('POST', '/post', body=body())

    def test_content_length_overrides_generator_unicode(self):
        c = HTTP11Connection('http2bin.org')
        c._sock = DummySocket()
        def body():
            yield u'hi'
            yield u'there'
            yield u'sir'

        with pytest.raises(ValueError):
            c.request(
                'POST',
                '/post',
                headers={b'content-length': b'10'},
                body=body()
            )


class TestHTTP11Response(object):
    def test_short_circuit_read(self):
        r = HTTP11Response(200, 'OK', {b'content-length': [b'0']}, None)

        assert r.read() == b''

    def test_aborted_reads(self):
        d = DummySocket()
        r = HTTP11Response(200, 'OK', {b'content-length': [b'15']}, d)

        with pytest.raises(ConnectionResetError):
            r.read()

    def test_read_expect_close(self):
        d = DummySocket()
        r = HTTP11Response(200, 'OK', {b'connection': [b'close']}, d)

        assert r.read() == b''

    def test_response_as_context_manager(self):
        r = HTTP11Response(
            200, 'OK', {b'content-length': [b'0']}, DummySocket()
        )

        with r:
            assert r.read() == b''

        assert r._sock == None

    def test_response_transparently_decrypts_gzip(self):
        d = DummySocket()
        headers = {b'content-encoding': [b'gzip'], b'connection': [b'close']}
        r = HTTP11Response(200, 'OK', headers, d)

        c = zlib_compressobj(wbits=24)
        body = c.compress(b'this is test data')
        body += c.flush()
        d._buffer = BytesIO(body)

        assert r.read() == b'this is test data'

    def test_response_transparently_decrypts_real_deflate(self):
        d = DummySocket()
        headers = {b'content-encoding': [b'deflate'], b'connection': [b'close']}
        r = HTTP11Response(200, 'OK', headers, d)
        c = zlib_compressobj(wbits=zlib.MAX_WBITS)
        body = c.compress(b'this is test data')
        body += c.flush()
        d._buffer = BytesIO(body)

        assert r.read() == b'this is test data'

    def test_response_transparently_decrypts_wrong_deflate(self):
        c = zlib_compressobj(wbits=-zlib.MAX_WBITS)
        body = c.compress(b'this is test data')
        body += c.flush()
        body_len = ('%s' % len(body)).encode('ascii')

        headers = {
            b'content-encoding': [b'deflate'], b'content-length': [body_len]
        }
        d = DummySocket()
        d._buffer = BytesIO(body)
        r = HTTP11Response(200, 'OK', headers, d)

        assert r.read() == b'this is test data'

    def test_basic_chunked_read(self):
        d = DummySocket()
        r = HTTP11Response(200, 'OK', {b'transfer-encoding': [b'chunked']}, d)

        data = (
            b'4\r\nwell\r\n'
            b'4\r\nwell\r\n'
            b'4\r\nwhat\r\n'
            b'4\r\nhave\r\n'
            b'2\r\nwe\r\n'
            b'a\r\nhereabouts\r\n'
            b'0\r\n\r\n'
        )
        d._buffer = BytesIO(data)
        results = [
            b'well', b'well', b'what', b'have', b'we', b'hereabouts'
        ]

        for c1, c2 in zip(results, r.read_chunked()):
            assert c1 == c2

        assert not list(r.read_chunked())

    def test_chunked_read_of_non_chunked(self):
        r = HTTP11Response(200, 'OK', {b'content-length': [b'0']}, None)

        with pytest.raises(ChunkedDecodeError):
            list(r.read_chunked())

    def test_chunked_read_aborts_early(self):
        r = HTTP11Response(
            200, 'OK', {b'transfer-encoding': [b'chunked']}, None
        )

        assert not list(r.read_chunked())

    def test_response_transparently_decrypts_chunked_gzip(self):
        d = DummySocket()
        headers = {
            b'content-encoding': [b'gzip'],
            b'transfer-encoding': [b'chunked'],
        }
        r = HTTP11Response(200, 'OK', headers, d)

        c = zlib_compressobj(wbits=24)
        body = c.compress(b'this is test data')
        body += c.flush()

        data = b''
        for index in range(0, len(body), 2):
            data += '2\r\n' + body[index:index+2] + '\r\n'

        data += b'0\r\n\r\n'
        d._buffer = BytesIO(data)

        received_body = b''
        for chunk in r.read_chunked():
            received_body += chunk

        assert received_body == b'this is test data'

class DummySocket(object):
    def __init__(self):
        self.queue = []
        self._buffer = BytesIO()
        self.can_read = False

    @property
    def buffer(self):
        return memoryview(self._buffer.getvalue())

    def advance_buffer(self, amt):
        self._buffer.read(amt)

    def send(self, data):
        if not isinstance(data, bytes):
            raise TypeError()

        self.queue.append(data)

    def recv(self, l):
        return memoryview(self._buffer.read(l))

    def close(self):
        pass

    def readline(self):
        return memoryview(self._buffer.readline())

    def fill(self):
        pass


class DummyFile(object):
    def __init__(self, data):
        self.buffer = BytesIO(data)

    def read(self, amt=None):
        return self.buffer.read(amt)

    def fileno(self):
        return -1

    def readline(self):
        self.buffer.readline()

    def __iter__(self):
        return self.buffer.__iter__()

