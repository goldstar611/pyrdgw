
from pyrdgw.protocol.enumerations import *
from pyrdgw.protocol.parser import *
from pyrdgw.protocol.serializer import *
from pyrdgw.protocol.messages import *
from pyrdgw.log_messages import *

import asyncio
import logging
from typing import Union


class ProtocolStateMachine:

    def __init__(self, websocket):

        self.websocket = websocket

        self.rdg_connection_id = websocket.rdg_connection_id
        self.rdg_correlation_id = websocket.rdg_correlation_id
        self.rdg_user_id = websocket.rdg_user_id

        self.state: ProtocolState = ProtocolState.INITIAL
        self.parser = ProtocolParser()
        self.serializer = ProtocolSerializer()
        self.target_reader: Union[None, asyncio.StreamReader] = None
        self.target_writer: Union[None, asyncio.StreamWriter] = None
        self.server_to_client_task = None

        self.logger = logging.getLogger('pyrdgw')

    async def run(self):
        
        try:
        
            self.__transition_to_state(ProtocolState.RECEIVING_HANDSHAKE_REQUEST)

            while True:

                if self.state == ProtocolState.RECEIVING_HANDSHAKE_REQUEST:
                    await self.__handle_receiving_handshake_request()

                elif self.state == ProtocolState.RECEIVING_TUNNEL_CREATE:
                    await self.__handle_receiving_tunnel_create()

                elif self.state == ProtocolState.RECEIVING_TUNNEL_AUTHORIZE:
                    await self.__handle_receiving_tunnel_authorize()

                elif self.state == ProtocolState.RECEIVING_CHANNEL_CREATE:
                    await self.__handle_receiving_channel_create()

                elif self.state == ProtocolState.DATA_TRANSFER:
                    await self.__handle_data_transfer()

                else:
                    logging.error("Invalid state detected")

        except Exception as ex:

            msg = str(ex)
            self.logger.error(self.__msg_with_correlation_id(msg))
            
            if self.server_to_client_task is not None:
                await self.__close_target_connection()

            if self.websocket.open:
                await self.__send_close_response_packet(ReturnCode.E_PROXY_INTERNALERROR)
                await self.websocket.close()
                await self.websocket.wait_closed()

    async def __handle_receiving_handshake_request(self):

        recv_buf = await self.websocket.recv()
        handshake_request = self.parser.read_handshake_request(recv_buf)

        self.__log_received_protocol_message(HttpPacketType.PKT_TYPE_HANDSHAKE_REQUEST)

        if handshake_request.ver_major != ProtocolVersion.VER_MAJOR:
            raise Exception(LogMessages.PROTOCOL_INVALID_MAJOR_VERSION)

        if handshake_request.ver_minor != ProtocolVersion.VER_MINOR:
            raise Exception(LogMessages.PROTOCOL_INVALID_MINOR_VERSION)

        if handshake_request.extended_auth != HttpExtendedAuth.HTTP_EXTENDED_AUTH_PAA:
            raise Exception(LogMessages.PROTOCOL_INVALID_AUTHENTICATION_METHOD)

        handshake_response = HandshakeResponse(
            0,
            ProtocolVersion.VER_MAJOR,
            ProtocolVersion.VER_MINOR,
            HttpExtendedAuth.HTTP_EXTENDED_AUTH_PAA)

        send_buf = self.serializer.write_handshake_response(handshake_response)

        self.__log_sending_protocol_message(HttpPacketType.PKT_TYPE_HANDSHAKE_RESPONSE)

        await self.websocket.send(send_buf)

        self.__transition_to_state(ProtocolState.RECEIVING_TUNNEL_CREATE)

    async def __handle_receiving_tunnel_create(self):

        recv_buf = await self.websocket.recv()
        tunnel_create = self.parser.read_tunnel_create(recv_buf)

        # Do something with PAA cookie here
        paa_cookie = tunnel_create.paa_cookie

        self.__log_received_protocol_message(HttpPacketType.PKT_TYPE_TUNNEL_CREATE)

        fields_present = \
            HttpTunnelResponseFieldsPresentFlags.HTTP_TUNNEL_RESPONSE_FIELD_TUNNEL_ID | \
            HttpTunnelResponseFieldsPresentFlags.HTTP_TUNNEL_RESPONSE_FIELD_CAPS

        tunnel_response = TunnelResponse(
            ProtocolVersion.SERVER_VERSION, 0, fields_present, 0, 0x3F)

        send_buf = self.serializer.write_tunnel_response(tunnel_response)

        self.__log_sending_protocol_message(HttpPacketType.PKT_TYPE_TUNNEL_RESPONSE)

        await self.websocket.send(send_buf)

        self.__transition_to_state(ProtocolState.RECEIVING_TUNNEL_AUTHORIZE)

    async def __handle_receiving_tunnel_authorize(self):

        recv_buf = await self.websocket.recv()
        tunnel_authorize = self.parser.read_tunnel_authorize(recv_buf)

        self.__log_received_protocol_message(HttpPacketType.PKT_TYPE_TUNNEL_AUTH)

        msg = LogMessages.PROTOCOL_RECEIVED_CLIENT_NAME.format(tunnel_authorize.client_name)
        self.logger.info(self.__msg_with_correlation_id(msg))

        fields_present = \
            HttpTunnelAuthResponseFieldsPresentFlags.HTTP_TUNNEL_AUTH_RESPONSE_FIELD_REDIR_FLAGS | \
            HttpTunnelAuthResponseFieldsPresentFlags.HTTP_TUNNEL_AUTH_RESPONSE_FIELD_IDLE_TIMEOUT

        redir_flags = HttpTunnelRedirFlags.HTTP_TUNNEL_REDIR_ENABLE_ALL
        tunnel_authorize_response = TunnelAuthorizeResponse(0, fields_present, redir_flags, 0)

        send_buf = self.serializer.write_tunnel_authorize_response(tunnel_authorize_response)

        self.__log_sending_protocol_message(HttpPacketType.PKT_TYPE_TUNNEL_AUTH_RESPONSE)

        await self.websocket.send(send_buf)

        self.__transition_to_state(ProtocolState.RECEIVING_CHANNEL_CREATE)

    async def __handle_receiving_channel_create(self):

        recv_buf = await self.websocket.recv()
        channel_create = self.parser.read_channel_create(recv_buf)

        self.__log_received_protocol_message(HttpPacketType.PKT_TYPE_CHANNEL_CREATE)

        if channel_create.num_resources <= 0:
            raise Exception(LogMessages.PROTOCOL_NO_RESOURCES_FOR_CHANNEL)

        connected = False
        for resource in channel_create.resources:

            # Authorize resource here

            try:
                self.target_reader, self.target_writer = await asyncio.open_connection(
                    resource, channel_create.port)

                connected = True

                msg = LogMessages.COMM_CONNECTED_RESOURCE.format(resource, channel_create.port)
                self.logger.info(self.__msg_with_correlation_id(msg))

                break

            except Exception as ex:
                msg = LogMessages.COMM_FAILED_CONNECT_RESOURCE.format(resource, channel_create.port, str(ex))
                self.logger.warning(self.__msg_with_correlation_id(msg))

        if not connected:
            raise Exception(LogMessages.COMM_FAILED_CONNECT_ANY_RESOURCE)

        fields_present = HttpChannelResponseFieldsPresentFlags.HTTP_CHANNEL_RESPONSE_FIELD_CHANNELID
        channel_response = ChannelResponse(0, fields_present, 0)

        send_buf = self.serializer.write_channel_response(channel_response)

        self.__log_sending_protocol_message(HttpPacketType.PKT_TYPE_CHANNEL_RESPONSE)

        await self.websocket.send(send_buf)

        self.__transition_to_state(ProtocolState.DATA_TRANSFER)

    async def __handle_data_transfer(self):
        
        if self.server_to_client_task is None:
            self.server_to_client_task = asyncio.ensure_future(self.__forward_data_server_to_client())

        recv_buf = await self.websocket.recv()
        packet_type = self.parser.peek_packet_type(recv_buf)

        if packet_type == HttpPacketType.PKT_TYPE_DATA:
            data_packet = self.parser.read_data_packet(recv_buf)
            self.target_writer.write(data_packet.data)

        elif packet_type == HttpPacketType.PKT_TYPE_CLOSE_CHANNEL:

            close_packet = self.parser.read_close_packet(recv_buf)

            self.__log_received_protocol_message(HttpPacketType.PKT_TYPE_CLOSE_CHANNEL)
            
            if self.server_to_client_task is not None:
                await self.__close_target_connection()

            await self.__send_close_response_packet(close_packet.status_code)

            self.__transition_to_state(ProtocolState.RECEIVING_CHANNEL_CREATE)

    async def __forward_data_server_to_client(self):

        while True:
            buffer_length = 10240
            data = await self.target_reader.read(buffer_length)
            #if len(data) != buffer_length:
            #    print(f"short data: {len(data)}")
            if len(data) >= buffer_length:
                print(f"exact data {buffer_length}")
            #data = await self.target_reader.read()
            data_packet = DataPacket(data)
            send_buf = self.serializer.write_data_packet(data_packet)
            await self.websocket.send(send_buf)

    async def __close_target_connection(self):
        logging.info("__close_target_connection")

        self.server_to_client_task.cancel()
        self.server_to_client_task = None

        self.target_writer.close()
        await self.target_writer.wait_closed()
        
        self.target_writer = None
        self.target_reader = None

    async def __send_close_response_packet(self, status_code: int):
        logging.info("__send_close_response_packet")

        close_response_packet = CloseResponsePacket(status_code)
        send_buf = self.serializer.write_close_response_packet(close_response_packet)

        self.__log_sending_protocol_message(HttpPacketType.PKT_TYPE_CLOSE_CHANNEL_RESPONSE)

        await self.websocket.send(send_buf)

    def __transition_to_state(self, new_state: ProtocolState):

        msg = LogMessages.PROTOCOL_TRANSITION_STATE.format(self.state.name, new_state.name)
        self.logger.debug(self.__msg_with_correlation_id(msg))

        self.state = new_state

    def __log_received_protocol_message(self, packet_type: HttpPacketType):
        msg = LogMessages.PROTOCOL_RECEIVED_MESSAGE.format(packet_type.name)
        self.logger.debug(self.__msg_with_correlation_id(msg))

    def __log_sending_protocol_message(self, packet_type: HttpPacketType):
        msg = LogMessages.PROTOCOL_SENDING_MESSAGE.format(packet_type.name)
        self.logger.debug(self.__msg_with_correlation_id(msg))

    def __msg_with_correlation_id(self, msg: str):
        return self.rdg_correlation_id + ' - ' + msg
