
import asyncio
import ssl
import websockets
import logging

from websockets import InvalidMessage
from websockets.legacy.http import d, read_headers, read_line

from pyrdgw.protocol.state_machine import *


class RDGWWebSocketServerProtocol(websockets.WebSocketServerProtocol):

    def __init__(
            self,
            *args,
            **kwargs):

        self.rdg_connection_id = ''
        self.rdg_correlation_id = ''
        self.rdg_user_id = ''

        self.logger = logging.getLogger('pyrdgw')

        super().__init__(
            *args,
            **kwargs)

    def process_request(self, path, request_headers):

        if 'RDG-Connection-Id' in request_headers:
            self.rdg_connection_id = request_headers['RDG-Connection-Id']

        if 'RDG-Correlation-Id' in request_headers:
            self.rdg_correlation_id = request_headers['RDG-Correlation-Id']

        if 'RDG-User-Id' in request_headers:
            self.rdg_user_id = request_headers['RDG-User-Id']

        msg = self.rdg_correlation_id + ' - ' + LogMessages.WEBSOCKET_RECEIVED_CONNECTION_REQUEST
        self.logger.info(msg)

        return None

    async def read_http_request(self):
        stream = self.reader
        try:
            try:
                request_line = await read_line(stream)
            except EOFError as exc:
                raise EOFError("connection closed while reading HTTP request line") from exc

            try:
                method, raw_path, version = request_line.split(b" ", 2)
            except ValueError:  # not enough values to unpack (expected 3, got 1-2)
                raise ValueError(f"invalid HTTP request line: {d(request_line)}") from None

            if method != b"RDG_OUT_DATA":
                raise ValueError(f"unsupported HTTP method: {d(method)}")
            if version != b"HTTP/1.1":
                raise ValueError(f"unsupported HTTP version: {d(version)}")
            path = raw_path.decode("ascii", "surrogateescape")

            headers = await read_headers(stream)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:
            raise InvalidMessage("did not receive a valid HTTP request") from exc

        if self.debug:
            self.logger.debug("< GET %s HTTP/1.1", path)
            for key, value in headers.raw_items():
                self.logger.debug("< %s: %s", key, value)

        self.path = path
        self.request_headers = headers

        return path, headers


class WebSocketServer:

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger('pyrdgw')

    async def __websocket_handler(self, websocket, path):

        state_machine = ProtocolStateMachine(websocket)
        await state_machine.run()

    def run(self):

        hostname = self.config['websocket_server']['hostname']
        port = self.config['websocket_server']['port']
        cert_path = self.config['websocket_server']['cert_path']
        key_path = self.config['websocket_server']['key_path']

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_path, key_path)

        start_server = websockets.serve(
            self.__websocket_handler, hostname, port, ssl=ssl_context, create_protocol=RDGWWebSocketServerProtocol)

        asyncio.get_event_loop().run_until_complete(start_server)

        msg = LogMessages.WEBSOCKET_SERVER_LISTENING.format(hostname, port)
        self.logger.info(msg)
