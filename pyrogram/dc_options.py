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
from contextlib import contextmanager
import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple


log = logging.getLogger(__name__)


try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


TEST = {
    1: "149.154.175.10",
    2: "149.154.167.40",
    3: "149.154.175.117"
}

PROD = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
    203: "91.105.192.100"
}

PROD_FALLBACKS = {
    1: ("149.154.175.50",),
    2: ("95.161.76.100",),
    5: ("149.154.171.5",)
}

DC_ENDPOINT_PROBE_TIMEOUT = 1.0
ENDPOINT_CACHE_VERSION = 1
SHARED_ENDPOINT_CACHE_PATH = Path("/var/tmp/kurigram/dc_endpoints.json")
ENDPOINT_CACHE_DIR_MODE = 0o1777
ENDPOINT_CACHE_FILE_MODE = 0o666


def get_dc_endpoint(dc_id: int, test_mode: bool) -> Tuple[str, int]:
    if test_mode:
        return TEST[dc_id], 80

    return PROD[dc_id], 443


def get_dc_endpoints(dc_id: int, test_mode: bool) -> Tuple[Tuple[str, int], ...]:
    endpoint = get_dc_endpoint(dc_id, test_mode)
    endpoints = [endpoint]

    if not test_mode:
        port = endpoint[1]

        for address in PROD_FALLBACKS.get(dc_id, ()):
            fallback = (address, port)

            if fallback not in endpoints:
                endpoints.append(fallback)

    return tuple(endpoints)


def endpoint_cache_key(
    dc_id: int,
    test_mode: bool,
    ipv6: bool,
    is_media: bool,
    is_cdn: bool,
) -> str:
    mode = "test" if test_mode else "prod"
    family = "v6" if ipv6 else "v4"

    if is_cdn:
        kind = "cdn"
    elif is_media:
        kind = "media"
    else:
        kind = "api"

    return f"{mode}:{family}:dc{dc_id}:{kind}"


def endpoint_cache_path() -> Path:
    if ensure_endpoint_cache_dir(SHARED_ENDPOINT_CACHE_PATH.parent):
        return SHARED_ENDPOINT_CACHE_PATH

    return Path.home() / ".cache" / "kurigram" / "dc_endpoints.json"


def ensure_endpoint_cache_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, ENDPOINT_CACHE_DIR_MODE)
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def load_endpoint_cache(path: Optional[Path] = None) -> dict:
    path = path or endpoint_cache_path()

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": ENDPOINT_CACHE_VERSION, "endpoints": {}}

    if not isinstance(data, dict) or data.get("version") != ENDPOINT_CACHE_VERSION:
        return {"version": ENDPOINT_CACHE_VERSION, "endpoints": {}}

    endpoints = data.get("endpoints")

    if not isinstance(endpoints, dict):
        data["endpoints"] = {}

    return data


@contextmanager
def endpoint_cache_write_lock(path: Path):
    if fcntl is None:
        yield
        return

    lock_file = None
    lock_path = path.with_name(f"{path.name}.lock")

    try:
        if not ensure_endpoint_cache_dir(path.parent):
            raise OSError(f"Unable to use cache directory {path.parent}")

        lock_file = lock_path.open("a")

        try:
            os.chmod(lock_path, ENDPOINT_CACHE_FILE_MODE)
        except OSError:
            pass

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        log.debug("Unable to lock DC endpoint cache %s: %s", lock_path, e)
        yield
        return

    try:
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

        lock_file.close()


def cached_dc_endpoint(cache_key: Optional[str], endpoints: Tuple[Tuple[str, int], ...]) -> Optional[Tuple[str, int]]:
    if not cache_key:
        return None

    cached = load_endpoint_cache().get("endpoints", {}).get(cache_key)

    if not isinstance(cached, dict):
        return None

    server_address = cached.get("server_address")
    port = cached.get("port")

    if not isinstance(server_address, str) or not isinstance(port, int):
        return None

    endpoint = (server_address, port)

    if endpoint not in endpoints:
        return None

    return endpoint


def reorder_with_cached_endpoint(
    cache_key: Optional[str],
    endpoints: Tuple[Tuple[str, int], ...],
) -> Tuple[Tuple[str, int], ...]:
    endpoints = tuple(dict.fromkeys(endpoints))
    cached_endpoint = cached_dc_endpoint(cache_key, endpoints)

    if not cached_endpoint:
        return endpoints

    return tuple(dict.fromkeys((cached_endpoint,) + endpoints))


def update_endpoint_cache(cache_key: Optional[str], endpoint: Tuple[str, int]) -> None:
    if not cache_key:
        return

    server_address, port = endpoint
    path = endpoint_cache_path()

    with endpoint_cache_write_lock(path):
        data = load_endpoint_cache(path)
        data.setdefault("endpoints", {})[cache_key] = {
            "server_address": server_address,
            "port": port,
            "updated_at": time.time(),
        }

        try:
            if not ensure_endpoint_cache_dir(path.parent):
                raise OSError(f"Unable to use cache directory {path.parent}")

            with path.open("w") as cache_file:
                json.dump(data, cache_file, sort_keys=True)

            try:
                os.chmod(path, ENDPOINT_CACHE_FILE_MODE)
            except OSError:
                pass
        except OSError as e:
            log.debug("Unable to update DC endpoint cache %s: %s", path, e)


async def probe_tcp_endpoint(server_address: str, port: int, timeout: float) -> Tuple[bool, float, Optional[Exception]]:
    writer = None
    started_at = time.monotonic()

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(server_address, port),
            timeout=timeout
        )
    except Exception as e:
        return False, time.monotonic() - started_at, e
    else:
        return True, time.monotonic() - started_at, None
    finally:
        if writer is not None:
            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass


def dc_option_endpoint(dc_option) -> Tuple[str, int]:
    return dc_option.ip_address, dc_option.port


def dedupe_dc_options(options: List) -> List:
    seen = set()
    deduped = []

    for option in options:
        endpoint = dc_option_endpoint(option)

        if endpoint in seen:
            continue

        seen.add(endpoint)
        deduped.append(option)

    return deduped


def static_dc_options(dc_id: int) -> List:
    try:
        endpoints = get_dc_endpoints(dc_id, False)
    except KeyError:
        return []

    from pyrogram import raw

    return [
        raw.types.DcOption(
            id=dc_id,
            ip_address=server_address,
            port=port,
            ipv6=False,
            media_only=False,
            cdn=False,
            static=True,
            tcpo_only=False,
            this_port_only=False,
        )
        for server_address, port in endpoints
    ]


async def select_dc_option(
    dc_id: int,
    options: List,
    proxy: Optional[dict] = None,
    preferred_endpoint: Optional[Tuple[str, int]] = None,
):
    if not options:
        raise ValueError(f"DC{dc_id} not found")

    preferred_option = next(
        (option for option in options if dc_option_endpoint(option) == preferred_endpoint),
        None
    )

    if proxy:
        if preferred_option:
            return preferred_option

        return options[0]

    if len(options) == 1:
        return options[0]

    async def run_probe(index, option):
        try:
            return index, option, await probe_tcp_endpoint(
                option.ip_address,
                option.port,
                DC_ENDPOINT_PROBE_TIMEOUT
            )
        except Exception as e:
            return index, option, (False, 0.0, e)

    probe_results = await asyncio.gather(*(
        run_probe(index, option)
        for index, option in enumerate(options)
    ))
    reachable = []

    for index, option, (ok, elapsed, error) in probe_results:
        if ok:
            reachable.append((
                elapsed,
                0 if dc_option_endpoint(option) == preferred_endpoint else 1,
                index,
                option
            ))
            continue

        log.warning(
            "DC%s endpoint candidate %s:%s failed TCP probe after %.3fs with %s: %s",
            dc_id,
            option.ip_address,
            option.port,
            elapsed,
            error.__class__.__name__ if error else "UnknownError",
            error or "",
        )

    if reachable:
        if preferred_endpoint:
            for elapsed, _, _, option in reachable:
                if dc_option_endpoint(option) == preferred_endpoint:
                    if dc_option_endpoint(option) != dc_option_endpoint(options[0]):
                        log.info(
                            "Keeping preferred DC%s endpoint %s:%s after TCP probe %.3fs; original endpoint was %s:%s",
                            dc_id,
                            option.ip_address,
                            option.port,
                            elapsed,
                            options[0].ip_address,
                            options[0].port,
                        )

                    return option

        reachable.sort(key=lambda result: (result[0], result[1], result[2]))
        elapsed, _, _, selected = reachable[0]
        original = options[0]

        if dc_option_endpoint(selected) != dc_option_endpoint(original):
            log.info(
                "Selected DC%s endpoint %s:%s after TCP probe %.3fs; original endpoint was %s:%s",
                dc_id,
                selected.ip_address,
                selected.port,
                elapsed,
                original.ip_address,
                original.port,
            )

        return selected

    if preferred_option:
        log.warning(
            "All DC%s endpoint probes failed; keeping preferred endpoint %s:%s",
            dc_id,
            preferred_option.ip_address,
            preferred_option.port,
        )
        return preferred_option

    log.warning(
        "All DC%s endpoint probes failed; keeping original endpoint %s:%s",
        dc_id,
        options[0].ip_address,
        options[0].port,
    )

    return options[0]


async def order_dc_endpoints(
    dc_id: int,
    endpoints: Tuple[Tuple[str, int], ...],
    proxy: Optional[dict] = None,
    preferred_endpoint: Optional[Tuple[str, int]] = None,
    cache_key: Optional[str] = None,
) -> Tuple[Tuple[str, int], ...]:
    endpoints = tuple(dict.fromkeys(endpoints))

    if proxy or len(endpoints) <= 1:
        return endpoints

    cached_endpoint = cached_dc_endpoint(cache_key, endpoints)

    if cached_endpoint:
        if cached_endpoint != endpoints[0]:
            log.info(
                "Using cached DC%s endpoint %s:%s before original endpoint %s:%s",
                dc_id,
                cached_endpoint[0],
                cached_endpoint[1],
                endpoints[0][0],
                endpoints[0][1],
            )

        return tuple(dict.fromkeys((cached_endpoint,) + endpoints))

    async def run_probe(index, endpoint):
        server_address, port = endpoint

        try:
            return index, endpoint, await probe_tcp_endpoint(
                server_address,
                port,
                DC_ENDPOINT_PROBE_TIMEOUT
            )
        except Exception as e:
            return index, endpoint, (False, 0.0, e)

    probe_results = await asyncio.gather(*(
        run_probe(index, endpoint)
        for index, endpoint in enumerate(endpoints)
    ))
    reachable = []
    failed = []

    for index, endpoint, (ok, elapsed, error) in probe_results:
        if ok:
            reachable.append((
                elapsed,
                0 if endpoint == preferred_endpoint else 1,
                index,
                endpoint
            ))
            continue

        failed.append(endpoint)
        log.warning(
            "DC%s endpoint candidate %s:%s failed TCP probe after %.3fs with %s: %s",
            dc_id,
            endpoint[0],
            endpoint[1],
            elapsed,
            error.__class__.__name__ if error else "UnknownError",
            error or "",
        )

    if not reachable:
        return endpoints

    reachable.sort(key=lambda result: (result[0], result[1], result[2]))
    ordered = tuple(result[3] for result in reachable) + tuple(failed)

    if ordered[0] != endpoints[0]:
        log.info(
            "Selected DC%s endpoint %s:%s after TCP probes; original endpoint was %s:%s",
            dc_id,
            ordered[0][0],
            ordered[0][1],
            endpoints[0][0],
            endpoints[0][1],
        )

    return ordered
