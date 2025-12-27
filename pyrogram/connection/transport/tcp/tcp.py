#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.
#
#  Pyrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Tuple, TypedDict

import socks

from pyrogram import utils

log = logging.getLogger(__name__)

proxy_type_by_scheme: Dict[str, int] = {
    "SOCKS4": socks.SOCKS4,
    "SOCKS5": socks.SOCKS5,
    "HTTP": socks.HTTP,
}


class Proxy(TypedDict):
    scheme: str
    hostname: str
    port: int
    username: Optional[str]
    password: Optional[str]


class TCP:
    TIMEOUT = 10

    def __init__(
        self,
        ipv6: bool = False,
        proxy: Proxy = None,
        crypto_executor_workers: int = 1,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.ipv6 = ipv6
        self.proxy = proxy

        self.crypto_executor_workers = crypto_executor_workers
        self.crypto_executor = ThreadPoolExecutor(max_workers=self.crypto_executor_workers, thread_name_prefix="CryptoWorker")

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

        self.marker_event = asyncio.Event()

        self.lock = asyncio.Lock()

        if isinstance(loop, asyncio.AbstractEventLoop):
            self.loop = loop
        else:
            self.loop = utils.get_event_loop()

    async def _connect_via_proxy(
        self,
        destination: Tuple[str, int]
    ) -> None:
        scheme = self.proxy.get("scheme")
        if scheme is None:
            raise ValueError("No scheme specified")

        proxy_type = proxy_type_by_scheme.get(scheme.upper())
        if proxy_type is None:
            raise ValueError(f"Unknown proxy type {scheme}")

        hostname = self.proxy.get("hostname")
        port = self.proxy.get("port")
        username = self.proxy.get("username")
        password = self.proxy.get("password")

        try:
            ip_address = ipaddress.ip_address(hostname)
        except ValueError:
            is_proxy_ipv6 = False
        else:
            is_proxy_ipv6 = isinstance(ip_address, ipaddress.IPv6Address)

        proxy_family = socket.AF_INET6 if is_proxy_ipv6 else socket.AF_INET
        sock = socks.socksocket(proxy_family)

        sock.set_proxy(
            proxy_type=proxy_type,
            addr=hostname,
            port=port,
            username=username,
            password=password
        )
        sock.settimeout(TCP.TIMEOUT)

        await self.loop.run_in_executor(self.crypto_executor, sock.connect, destination)

        sock.setblocking(False)

        self.reader, self.writer = await asyncio.open_connection(
            sock=sock
        )

    async def _connect_via_direct(
        self,
        destination: Tuple[str, int]
    ) -> None:
        host, port = destination
        family = socket.AF_INET6 if self.ipv6 else socket.AF_INET
        self.reader, self.writer = await asyncio.open_connection(
            host=host,
            port=port,
            family=family
        )

    async def _connect(self, destination: Tuple[str, int]) -> None:
        if self.proxy:
            await self._connect_via_proxy(destination)
        else:
            await self._connect_via_direct(destination)

    async def connect(self, address: Tuple[str, int]) -> None:
        try:
            await asyncio.wait_for(self._connect(address), timeout=TCP.TIMEOUT)
        except asyncio.TimeoutError:  # Re-raise as TimeoutError. asyncio.TimeoutError is deprecated in 3.11
            raise TimeoutError("Connection timed out")

    async def close(self) -> None:
        async with self.lock:
            if self.writer is None or self.writer.is_closing():
                return None

            try:
                if self.writer.transport is not None:
                    self.writer.transport.abort()

                self.writer.close()

                await asyncio.wait_for(self.writer.wait_closed(), timeout=TCP.TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("Disconnect timed out")
            except Exception as e:
                log.info("Close exception: %s %s", type(e).__name__, e)
            finally:
                self.writer = None

    async def send(self, data: bytes, wait_for_marker: bool = True) -> None:
        async with self.lock:
            if self.writer is None or self.writer.is_closing():
                return None

            if wait_for_marker:
                try:
                    await asyncio.wait_for(self.marker_event.wait(), timeout=TCP.TIMEOUT)
                except asyncio.TimeoutError:
                    raise TimeoutError

            try:
                self.writer.write(data)
                await self.writer.drain()
            except Exception as e:
                log.info("Send exception: %s %s", type(e).__name__, e)
                raise OSError(e)

    async def recv(self, length: int = 0) -> Optional[bytes]:
        if not self.reader:
            return None

        data = b""

        while len(data) < length:
            try:
                chunk = await asyncio.wait_for(
                    self.reader.read(length - len(data)),
                    timeout=TCP.TIMEOUT
                )
            except (OSError, asyncio.TimeoutError):
                return None
            else:
                if chunk:
                    data += chunk
                else:
                    return None

        return data
