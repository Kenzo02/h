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

import re
from struct import unpack

# SMP = Supplementary Multilingual Plane: https://en.wikipedia.org/wiki/Plane_(Unicode)#Overview
SMP_RE = re.compile(r"[\U00010000-\U0010FFFF]")


def add_surrogates(text: str) -> str:
    # Replace each SMP code point with a surrogate pair
    return SMP_RE.sub(
        lambda match:  # Split SMP in two surrogates
        "".join(chr(i) for i in unpack("<HH", match.group().encode("utf-16le"))),
        text
    )


def remove_surrogates(text: str) -> str:
    # Handle surrogate characters more comprehensively
    # This preserves URL encoding while fixing surrogate issues
    try:
        # First attempt: standard approach for well-formed text
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        # Fallback: handle malformed surrogates more carefully
        result = []
        i = 0
        while i < len(text):
            char = text[i]
            char_code = ord(char)
            
            # Check if it's a high surrogate
            if 0xD800 <= char_code <= 0xDBFF:
                # Look for corresponding low surrogate
                if i + 1 < len(text):
                    next_char = text[i + 1]
                    next_code = ord(next_char)
                    if 0xDC00 <= next_code <= 0xDFFF:
                        # Valid surrogate pair - reconstruct
                        try:
                            reconstructed = (char + next_char).encode("utf-16", "surrogatepass").decode("utf-16")
                            result.append(reconstructed)
                            i += 2
                            continue
                        except UnicodeError:
                            pass
                
                # Invalid or orphaned high surrogate - skip it
                i += 1
            elif 0xDC00 <= char_code <= 0xDFFF:
                # Orphaned low surrogate - skip it
                i += 1
            else:
                # Normal character - keep it
                result.append(char)
                i += 1
        
        return ''.join(result)


def replace_once(source: str, old: str, new: str, start: int):
    return source[:start] + source[start:].replace(old, new, 1)
