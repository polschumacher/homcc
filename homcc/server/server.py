# Copyright (c) 2023 Celonis SE
# Covered under the included MIT License:
#   https://github.com/celonis/homcc/blob/main/LICENSE

"""Main logic for the homcc server."""
import logging
import os
import random
import socketserver
import threading
from functools import singledispatchmethod
from pathlib import Path
from socket import SHUT_RDWR
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Dict, List, Optional, Tuple

from homcc.common.arguments import Arguments, AsyncCompilationTimeoutError
from homcc.common.constants import TCP_BUFFER_SIZE
from homcc.common.errors import (
    ClientDisconnectedError,
    ServerInitializationError,
    UnsupportedCompilerError,
)
from homcc.common.hashing import hash_file_with_bytes
from homcc.common.messages import (
    ArgumentMessage,
    CompilationResultMessage,
    ConnectionRefusedMessage,
    DependencyReplyMessage,
    DependencyRequestMessage,
    Message,
)
from homcc.server.cache import Cache
from homcc.server.docker import is_docker_available, is_valid_docker_container
from homcc.server.environment import (
    COMPILATION_TIMEOUT,
    Environment,
    create_root_temp_folder,
)
from homcc.server.parsing import (
    DEFAULT_ADDRESS,
    DEFAULT_LIMIT,
    DEFAULT_PORT,
    ServerConfig,
)
from homcc.server.schroot import (
    get_schroot_profiles,
    is_schroot_available,
    is_valid_schroot_profile,
)

logger = logging.getLogger(__name__)


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """TCP Server instance, holding data relevant across compilations."""

    def __init__(self, address: Optional[str], port: Optional[int], limit: Optional[int]):
        address = address or DEFAULT_ADDRESS
        port = port or DEFAULT_PORT

        super().__init__((address, port), TCPRequestHandler)

        # default 1 job per (available) CPU, +2 to enable more concurrency while waiting for disk or network IO
        self.connections_limit: int = limit or (DEFAULT_LIMIT + 2)

        if DEFAULT_LIMIT == -1:
            logger.error(
                "A meaningful CPU count could not be determined and the maximum job limit is set to %i.\n"
                "Please provide the job limit explicitly either via the CLI or the configuration file!",
                self.connections_limit,
            )

        self.root_temp_folder: TemporaryDirectory = create_root_temp_folder()

        self.current_amount_connections: int = 0  # indicates the amount of clients that are currently connected
        self.current_amount_connections_mutex: Lock = Lock()

        self.cache = Cache(Path(self.root_temp_folder.name))

    @staticmethod
    def send_message(request, message: Message):
        """Sends a response to the request."""
        try:
            request.sendall(message.to_bytes())
        except ConnectionError as err:
            logger.error("Connection error while trying to send '%s' message. %s", message.message_type, err)
            logger.debug(
                "The following message could not be sent due to a connection error:\n%s", message.get_json_str()
            )

    @staticmethod
    def close_connection_for_request(request, info: str):
        """Close a connection for a certain request."""
        conn_refused_message = ConnectionRefusedMessage(info)
        TCPServer.send_message(request, conn_refused_message)

        request.shutdown(SHUT_RDWR)

        def is_ready_to_close() -> bool:
            try:
                read_data = request.recv(TCP_BUFFER_SIZE)
                return len(read_data) == 0
            except ConnectionError as error:
                logger.debug("Exception while waiting for zero-len TCP packet: %s", error)
                return True

        # Wait until we receive an error or zero-sized package to finally close the socket.
        # See https://stackoverflow.com/questions/4160347/close-vs-shutdown-socket/23483487#23483487
        while not is_ready_to_close():
            pass

        request.close()

    def verify_request(self, request, _) -> bool:
        with self.current_amount_connections_mutex:
            accept_connection = self.current_amount_connections < self.connections_limit

        if not accept_connection:
            logger.info(
                "Not accepting new connection, as max limit of #%i connections is already reached.",
                self.connections_limit,
            )

            self.close_connection_for_request(request, f"Limit {self.connections_limit} reached")

        return accept_connection

    def __del__(self):
        try:
            root_temp_folder = self.root_temp_folder
        except AttributeError:
            # root_temp_folder may not have been initialized yet, then we do not have to clean it up
            return

        root_temp_folder.cleanup()


class TCPRequestHandler(socketserver.BaseRequestHandler):
    """Handles all requests received from the client."""

    mapped_dependencies: Dict[str, str]
    """All dependencies for the current compilation, mapped to server paths."""
    needed_dependencies: Dict[str, str]
    """Further dependencies needed from the client."""
    needed_dependency_keys: List[str]
    """Shuffled list of keys for the needed dependencies dict."""
    compiler_arguments: Arguments
    """List of compiler arguments."""
    instance_path: str
    """Path to the current compilation inside /tmp/."""
    mapped_cwd: str
    """Absolute path to the working directory."""
    server: TCPServer
    """The TCP server belonging to this handler. (redefine for typing)"""
    environment: Environment
    """Environment created for this compilation request."""
    terminate: bool
    """Flag to indicate closing the connection from the server side."""

    @singledispatchmethod
    def _handle_message(self, message):
        raise NotImplementedError("Unsupported message type.")

    @_handle_message.register
    def _handle_argument_message(self, message: ArgumentMessage):
        logger.info("Handling ArgumentMessage...")

        # construct Arguments from args
        self.compiler_arguments: Arguments = Arguments.from_vargs(*message.get_args())

        target = message.target
        schroot_profile = message.schroot_profile
        docker_container = message.docker_container

        if compression := message.get_compression():
            logger.info("Using %s compression.", compression.name())

        self.environment = Environment(
            root_folder=Path(self.server.root_temp_folder.name),
            cwd=message.get_cwd(),
            schroot_profile=schroot_profile,
            docker_container=docker_container,
            compression=compression,
            sock_fd=self.request.fileno(),
        )

        if not self.check_client_request_satisfiability(
            self.compiler_arguments, target, schroot_profile, docker_container
        ):
            return

        if (
            target is not None
            and self.compiler_arguments.get_compiler_target_triple(self.environment.shell_env) != target
        ):
            self.compiler_arguments = self.compiler_arguments.add_target(target)
            logger.info("Using explicit target '%s' for compilation.", target)

        self.compiler_arguments = self.environment.map_args(self.compiler_arguments)
        logger.debug("Mapped compiler args: %s", str(self.compiler_arguments))

        self.mapped_dependencies = self.environment.map_dependency_paths(message.get_dependencies())
        logger.debug("Mapped dependencies: %s", self.mapped_dependencies)

        self.needed_dependencies = self.environment.get_needed_dependencies(self.mapped_dependencies, self.server.cache)
        logger.debug("Needed dependencies: %s", self.needed_dependencies)

        # shuffle the keys so we request them at a different order later to avoid
        # transmitting the same files for simultaneous requests
        self.needed_dependency_keys = list(self.needed_dependencies.keys())
        random.shuffle(self.needed_dependency_keys)

        logger.info(
            "#%i cached dependencies, #%i missing dependencies.",
            len(self.mapped_dependencies) - len(self.needed_dependencies),
            len(self.needed_dependencies),
        )

        self.check_dependencies_exist()

    @_handle_message.register
    def _handle_dependency_request_message(self, _: DependencyRequestMessage):
        logger.warning("Received DependencyRequestMessage, but this message is only sent by the server!")

    @_handle_message.register
    def _handle_dependency_reply_message(self, message: DependencyReplyMessage):
        logger.debug("Handling DependencyReplyMessage...")
        logger.debug("Len of dependency reply payload is %i", message.get_further_payload_size())

        dependency_content = message.get_content()
        dependency_path = next(iter(self.needed_dependency_keys))
        dependency_hash = self.needed_dependencies[dependency_path]

        retrieved_dependency_hash = hash_file_with_bytes(dependency_content)

        # verify that the hashes match
        if dependency_hash != retrieved_dependency_hash:
            logger.error(
                """Assertion failed: Hashes of requested file and received file (path: %s) do not match!
                This should not happen.""",
                dependency_path,
            )
        else:
            del self.needed_dependencies[dependency_path]
            self.needed_dependency_keys.pop(0)

            self.server.cache.put(dependency_hash, dependency_content)

            self.environment.link_dependency_to_cache(dependency_path, dependency_hash, self.server.cache)

        self.check_dependencies_exist()

    @_handle_message.register
    def _handle_compilation_result_message(self, _: CompilationResultMessage):
        logger.warning("Received CompilationResultMessage, but this message is only sent by the server!")

    def _request_next_dependency(self) -> bool:
        """Requests a dependency with the given sha1sum from the client.
        Returns False if there was nothing to request any more."""

        while len(self.needed_dependencies) > 0:
            next_needed_file: str = next(iter(self.needed_dependency_keys))
            next_needed_hash: str = self.needed_dependencies[next_needed_file]

            logger.debug("#%i needed dependencies left.", len(self.needed_dependencies))

            if next_needed_hash in self.server.cache:
                logger.debug("Dependency with hash '%s' is in cache.", next_needed_hash)
                self.environment.link_dependency_to_cache(next_needed_file, next_needed_hash, self.server.cache)

                del self.needed_dependencies[next_needed_file]
                self.needed_dependency_keys.pop(0)
            else:
                request_message = DependencyRequestMessage(next_needed_hash)

                logger.debug("Sending request for dependency with hash '%s'.", str(request_message.get_sha1sum()))
                self.send_message(request_message)
                return len(self.needed_dependencies) > 0

        return False

    def check_dependencies_exist(self):
        """Checks if all dependencies exist. If yes, starts compiling. If no, requests missing dependencies."""
        if self._request_next_dependency():
            logger.debug("Waiting for a dependency to be sent by the client.")
            return

        # no further dependencies needed, compile now
        try:
            result_message = self.environment.do_compilation(self.compiler_arguments)
        except ClientDisconnectedError:
            return
        except AsyncCompilationTimeoutError as error:
            logger.error("Error during compilation: %s", error)

            result_message = CompilationResultMessage(
                object_files=[],
                stdout="",
                stderr=f"Remote compilation is limited to {COMPILATION_TIMEOUT}s.",
                return_code=os.EX_TEMPFAIL,
                compression=self.environment.compression,
                dwarf_files=[],
            )
        except IOError as error:
            logger.error("Error during compilation: %s", error)

            result_message = CompilationResultMessage(
                object_files=[],
                stdout="",
                stderr=f"Invocation of compiler failed:\n{error}",
                return_code=os.EX_IOERR,
                compression=self.environment.compression,
                dwarf_files=[],
            )

        self.send_message(result_message)

    def _try_parse_message(self, message_bytes: bytearray) -> int:
        bytes_needed, parsed_message = Message.from_bytes(message_bytes)

        if bytes_needed < 0:
            logger.debug("Received message, but having #%i bytes too much supplied.", abs(bytes_needed))
        elif bytes_needed > 0:
            logger.debug("Supplied buffer does not suffice to parse the message, need further #%i bytes!", bytes_needed)

        if parsed_message is not None:
            logger.debug("Received message of type %s!", parsed_message.message_type)
            self._handle_message(parsed_message)

        return bytes_needed

    def check_client_request_satisfiability(
        self,
        arguments: Arguments,
        target: Optional[str],
        schroot_profile: Optional[str],
        docker_container: Optional[str],
    ) -> bool:
        """Checks whether a request from a client can be satisfied."""
        if not self.check_schroot_profile_argument(schroot_profile):
            return False

        if not self.check_docker_container_argument(docker_container):
            return False

        if not self.check_compiler_arguments(arguments):
            return False

        if not self.check_target_argument(arguments, target):
            return False

        return True

    def check_target_argument(self, arguments: Arguments, target: Optional[str]) -> bool:
        """Checks whether the local compiler supports the specified target."""
        if target is None:
            return True

        try:
            supports_target = self.environment.compiler_supports_target(arguments, target)
            if not supports_target:
                logger.warning(
                    "Compiler '%s' does not support requested target '%s', declining compilation.",
                    arguments.compiler,
                    target,
                )
                self.close_connection(f"Compiler '{arguments.compiler}' does not support target '{target}'.")

            return supports_target
        except UnsupportedCompilerError:
            logger.error(
                "Could not retrieve information about targets for the compiler '%s', "
                "will still try use the given target.",
                arguments.compiler,
            )
            return True

    def check_compiler_arguments(self, arguments: Arguments) -> bool:
        """Checks whether the specified compiler is available on the system."""
        if not self.environment.compiler_exists(arguments):
            logger.warning(
                "Compilation with compiler '%s' requested, but this compiler is not installed"
                " on the system using shell environment '%s'.",
                arguments.compiler,
                type(self.environment.shell_env).__name__,
            )
            self.close_connection(
                f"Compiler '{arguments.compiler}' is not available on the server, can not compile remotely"
            )
            return False

        return True

    def check_schroot_profile_argument(self, schroot_profile: Optional[str]) -> bool:
        """Checks whether the specified schroot profile requested by the client can be used.
        It can not be used if the schroot profile with the given name is not set up on the server."""
        if schroot_profile is None:
            return True

        if not is_schroot_available():
            logger.info(
                "Refusing client because 'schroot' compilation could not be executed as 'schroot' "
                "is not installed on the server."
            )
            self.close_connection(
                f"Profile {schroot_profile} could not be used as 'schroot' is not installed on the server",
            )
            return False

        if not is_valid_schroot_profile(schroot_profile):
            logger.info("Refusing client because 'schroot' environment '%s' is not provided.", schroot_profile)
            self.close_connection(
                f"Profile {schroot_profile} could not be used as it is not a provided profile "
                f"[{', '.join(get_schroot_profiles())}].",
            )
            return False

        logger.info("Using '%s' schroot profile.", schroot_profile)
        return True

    def check_docker_container_argument(self, docker_container: Optional[str]) -> bool:
        """Checks whether the docker container request given by the client can be served."""
        if docker_container is None:
            return True

        if not is_docker_available():
            logger.warning(
                "Refusing client because docker is not installed on the server "
                "but dockerized compilation was requested."
            )
            self.close_connection(
                f"Docker container '{docker_container}' could not be used as the server hasn't installed docker"
            )
            return False

        if not is_valid_docker_container(docker_container):
            logger.info(
                "Refusing client because docker container '%s' is not a valid or running container on the server.",
                docker_container,
            )
            self.close_connection(
                f"Docker container '{docker_container}' could not be used as it is not a valid or "
                "running container on the server"
            )
            return False

        logger.info("Compiling inside docker container '%s'.", docker_container)
        return True

    def send_message(self, message: Message):
        """Sends a message using the associated TCP socket."""
        self.server.send_message(self.request, message)

    def close_connection(self, info: str):
        """Closes the connection for this particular request."""
        self.server.close_connection_for_request(self.request, info)
        self.terminate = True

    def recv(self) -> bytearray:
        """Function that receives from the connection and returns an empty
        bytearray when the connection has been closed."""
        try:
            return self.request.recv(TCP_BUFFER_SIZE)
        except ConnectionError:
            return bytearray()

    def recv_loop(self):
        """Indefinitely tries to receive data and parse messages until the connection has been closed."""
        self.terminate = False

        while not self.terminate:
            recv_bytes: bytearray = self.recv()

            if len(recv_bytes) == 0:
                try:
                    logger.info("Connection '%s' closed.", self.environment.instance_folder)
                except AttributeError:
                    logger.info("Connection without existing environment closed.")

                return

            bytes_needed: int = Message.MINIMUM_SIZE_BYTES
            while bytes_needed != 0 and len(recv_bytes) > 0:
                bytes_needed = self._try_parse_message(recv_bytes)

                if bytes_needed < 0:
                    # Parsed a message, we still have further messages in the buffer.
                    # Remove parsed bytes form the buffer.
                    recv_bytes = recv_bytes[len(recv_bytes) - abs(bytes_needed) :]
                elif bytes_needed > 0:
                    # A message is only partly contained in the current buffer and we need more data
                    further_recv_bytes = self.recv()

                    if len(further_recv_bytes) == 0:
                        logger.error("Connection closed while only partly received a message. Ungraceful disconnect.")
                        return

                    recv_bytes += further_recv_bytes

    def handle(self):
        """Handles incoming requests. Returning from this function means
        that the connection will be closed from the server side."""
        self.request.settimeout(COMPILATION_TIMEOUT)

        with self.server.current_amount_connections_mutex:
            self.server.current_amount_connections += 1

        try:
            self.recv_loop()
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception("Error when handling client: %s", ex)
        finally:
            with self.server.current_amount_connections_mutex:
                self.server.current_amount_connections -= 1


def start_server(config: ServerConfig) -> Tuple[TCPServer, threading.Thread]:
    try:
        server: TCPServer = TCPServer(config.address, config.port, config.limit)
    except OSError as err:
        logger.error("Could not start TCP server: %s", err)
        raise ServerInitializationError from err

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    return server, server_thread


def stop_server(server: TCPServer):
    server.shutdown()
