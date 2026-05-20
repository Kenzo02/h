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

from typing import Tuple


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
    5: ("149.154.171.5",)
}


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
