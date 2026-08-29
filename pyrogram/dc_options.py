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
import errno
import json
import logging
import os
import secrets
import stat
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
ENDPOINT_CACHE_DIR_MODE = 0o700
ENDPOINT_CACHE_FILE_MODE = 0o600


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
    path = Path.home() / ".cache" / "kurigram" / "dc_endpoints.json"
    ensure_endpoint_cache_dir(path.parent)
    return path



def ensure_endpoint_cache_dir(path: Path) -> bool:
    directory_fd = _open_endpoint_cache_dir(path)

    if directory_fd is None:
        return False

    try:
        return True
    finally:
        os.close(directory_fd)


def _empty_endpoint_cache() -> dict:
    return {"version": ENDPOINT_CACHE_VERSION, "endpoints": {}}


def _current_user_id() -> Optional[int]:
    getuid = getattr(os, "getuid", None)

    if getuid is None:
        return None

    return getuid()


def _is_owned_by_current_user(file_stat: os.stat_result) -> bool:
    user_id = _current_user_id()
    return user_id is None or file_stat.st_uid == user_id


def _endpoint_cache_open_flags() -> Optional[int]:
    nofollow = getattr(os, "O_NOFOLLOW", None)

    if nofollow is None or not hasattr(os, "fchmod"):
        return None

    return nofollow | getattr(os, "O_CLOEXEC", 0)


def _open_endpoint_cache_dir(path: Path) -> Optional[int]:
    secure_flags = _endpoint_cache_open_flags()

    if secure_flags is None:
        return None

    path = Path(path)
    path_parts = path.parts

    if path.is_absolute():
        root = path.anchor or os.path.sep
        path_parts = path_parts[1:]
    else:
        root = "."

    if any(part in ("", ".", "..") for part in path_parts):
        return None

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | secure_flags

    try:
        directory_fd = os.open(root, directory_flags)
    except OSError:
        return None

    try:
        for index, part in enumerate(path_parts):
            try:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not path_parts:
                    raise

                try:
                    os.mkdir(part, ENDPOINT_CACHE_DIR_MODE, dir_fd=directory_fd)
                except FileExistsError:
                    pass

                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)

            os.close(directory_fd)
            directory_fd = child_fd

            file_stat = os.fstat(directory_fd)

            if not stat.S_ISDIR(file_stat.st_mode):
                raise OSError(f"Unsafe endpoint cache directory: {path}")

            if index < len(path_parts) - 1:
                if stat.S_IMODE(file_stat.st_mode) & 0o022:
                    raise OSError(f"Writable endpoint cache parent: {path}")
            elif not _is_owned_by_current_user(file_stat):
                raise OSError(f"Unsafe endpoint cache directory: {path}")

            if index == len(path_parts) - 1:
                os.fchmod(directory_fd, ENDPOINT_CACHE_DIR_MODE)

                if stat.S_IMODE(os.fstat(directory_fd).st_mode) != ENDPOINT_CACHE_DIR_MODE:
                    raise OSError(f"Unable to secure endpoint cache directory: {path}")

        return directory_fd
    except OSError:
        os.close(directory_fd)
        return None


def _cache_file_name(path: Path) -> Optional[str]:
    name = Path(path).name

    if not name or name in (".", ".."):
        return None

    return name


def _open_endpoint_cache_file(
    directory_fd: int,
    name: str,
    *,
    write: bool = False,
    create: bool = False,
    non_blocking: bool = False,
) -> Optional[int]:
    secure_flags = _endpoint_cache_open_flags()

    if secure_flags is None:
        return None

    flags = (os.O_RDWR if write else os.O_RDONLY) | secure_flags

    if create:
        flags |= os.O_CREAT

    if non_blocking:
        flags |= getattr(os, "O_NONBLOCK", 0)

    try:
        file_fd = os.open(name, flags, ENDPOINT_CACHE_FILE_MODE, dir_fd=directory_fd)
    except OSError:
        return None

    try:
        file_stat = os.fstat(file_fd)

        if not stat.S_ISREG(file_stat.st_mode) or not _is_owned_by_current_user(file_stat):
            raise OSError(f"Unsafe endpoint cache file: {name}")

        os.fchmod(file_fd, ENDPOINT_CACHE_FILE_MODE)

        if stat.S_IMODE(os.fstat(file_fd).st_mode) != ENDPOINT_CACHE_FILE_MODE:
            raise OSError(f"Unable to secure endpoint cache file: {name}")

        return file_fd
    except OSError:
        os.close(file_fd)
        return None


def _cache_file_is_safe_or_missing(directory_fd: int, name: str) -> bool:
    try:
        file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    return stat.S_ISREG(file_stat.st_mode) and _is_owned_by_current_user(file_stat)


def _read_endpoint_cache(path: Path, directory_fd: int) -> Tuple[dict, bool]:
    name = _cache_file_name(path)

    if name is None:
        return _empty_endpoint_cache(), False

    file_fd = _open_endpoint_cache_file(directory_fd, name)

    if file_fd is None:
        if _cache_file_is_safe_or_missing(directory_fd, name):
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return _empty_endpoint_cache(), True
            except OSError:
                pass

        return _empty_endpoint_cache(), False

    try:
        with os.fdopen(file_fd, "r", encoding="utf-8") as cache_file:
            file_fd = None
            data = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return _empty_endpoint_cache(), True
    finally:
        if file_fd is not None:
            os.close(file_fd)

    if not isinstance(data, dict) or data.get("version") != ENDPOINT_CACHE_VERSION:
        return _empty_endpoint_cache(), True

    if not isinstance(data.get("endpoints"), dict):
        data["endpoints"] = {}

    return data, True


def _create_endpoint_cache_temp(directory_fd: int, path: Path) -> Tuple[int, str]:
    secure_flags = _endpoint_cache_open_flags()
    name_prefix = f".{path.name}.{os.getpid()}."

    if secure_flags is None:
        raise OSError("Secure endpoint cache file creation is unavailable")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | secure_flags

    for _ in range(10):
        name = f"{name_prefix}{secrets.token_hex(8)}.tmp"

        try:
            file_fd = os.open(name, flags, ENDPOINT_CACHE_FILE_MODE, dir_fd=directory_fd)
        except OSError as e:
            if e.errno == errno.EEXIST:
                continue

            raise

        try:
            file_stat = os.fstat(file_fd)

            if not stat.S_ISREG(file_stat.st_mode) or not _is_owned_by_current_user(file_stat):
                raise OSError(f"Unsafe endpoint cache temporary file: {name}")

            os.fchmod(file_fd, ENDPOINT_CACHE_FILE_MODE)

            if stat.S_IMODE(os.fstat(file_fd).st_mode) != ENDPOINT_CACHE_FILE_MODE:
                raise OSError(f"Unable to secure endpoint cache temporary file: {name}")

            return file_fd, name
        except OSError:
            os.close(file_fd)

            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass

            raise

    raise OSError("Unable to create endpoint cache temporary file")


def _replace_endpoint_cache(temp_name: str, path: Path, directory_fd: int) -> None:
    supports_dir_fd = getattr(os, "supports_dir_fd", ())

    if os.replace in supports_dir_fd:
        os.replace(
            temp_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    elif os.rename in supports_dir_fd:
        os.rename(
            temp_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    else:
        raise OSError("Secure endpoint cache replacement is unavailable")


def load_endpoint_cache(path: Optional[Path] = None) -> dict:
    path = path or endpoint_cache_path()
    directory_fd = _open_endpoint_cache_dir(path.parent)

    if directory_fd is None:
        return _empty_endpoint_cache()

    try:
        data, safe = _read_endpoint_cache(path, directory_fd)
        return data if safe else _empty_endpoint_cache()
    finally:
        os.close(directory_fd)


@contextmanager
def endpoint_cache_write_lock(path: Path):
    directory_fd = _open_endpoint_cache_dir(path.parent)
    lock_path = path.with_name(f"{path.name}.lock")
    lock_name = _cache_file_name(lock_path)

    if directory_fd is None or lock_name is None:
        log.debug("Unable to secure DC endpoint cache directory %s", path.parent)
        yield None
        return

    lock_fd = _open_endpoint_cache_file(
        directory_fd,
        lock_name,
        write=True,
        create=True,
        non_blocking=True,
    )

    if lock_fd is None:
        log.debug("Unable to secure DC endpoint cache lock %s", lock_path)
        os.close(directory_fd)
        yield None
        return

    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as e:
        log.debug("Unable to lock DC endpoint cache %s: %s", lock_path, e)
        os.close(lock_fd)
        os.close(directory_fd)
        yield None
        return

    try:
        yield directory_fd
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

        os.close(lock_fd)
        os.close(directory_fd)


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

    with endpoint_cache_write_lock(path) as directory_fd:
        if directory_fd is None:
            return

        data, safe = _read_endpoint_cache(path, directory_fd)

        if not safe:
            return

        data.setdefault("endpoints", {})[cache_key] = {
            "server_address": server_address,
            "port": port,
            "updated_at": time.time(),
        }

        temp_fd = None
        temp_name = None

        try:
            if not _cache_file_is_safe_or_missing(directory_fd, path.name):
                raise OSError(f"Unsafe endpoint cache file: {path}")

            temp_fd, temp_name = _create_endpoint_cache_temp(directory_fd, path)

            with os.fdopen(temp_fd, "w", encoding="utf-8") as cache_file:
                temp_fd = None
                json.dump(data, cache_file, sort_keys=True)
                cache_file.flush()
                os.fsync(cache_file.fileno())

            if not _cache_file_is_safe_or_missing(directory_fd, path.name):
                raise OSError(f"Unsafe endpoint cache file: {path}")

            _replace_endpoint_cache(temp_name, path, directory_fd)
            temp_name = None

            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        except OSError as e:
            log.debug("Unable to update DC endpoint cache %s: %s", path, e)
        finally:
            if temp_fd is not None:
                os.close(temp_fd)

            if temp_name is not None:
                try:
                    os.unlink(temp_name, dir_fd=directory_fd)
                except OSError:
                    pass


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
