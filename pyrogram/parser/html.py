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

import html
import logging
import re
from html.parser import HTMLParser
from typing import Optional

import pyrogram
from pyrogram import raw
from pyrogram.enums import MessageEntityType
from pyrogram.errors import PeerIdInvalid
from . import utils

log = logging.getLogger(__name__)


class Parser(HTMLParser):
    MENTION_RE = re.compile(r"tg://user\?id=(\d+)")
    # Precompiled regex patterns for better performance
    WHITESPACE_START_RE = re.compile(r"^\s*(<[^>]*>)\s*")
    WHITESPACE_END_RE = re.compile(r"\s*(<\/[^>]*>)\s*$")

    def __init__(self, client: "pyrogram.Client"):
        super().__init__()

        self.client = client

        self.text = ""
        self.entities = []
        self.tag_entities = {}

    def handle_starttag(self, tag, attrs):
        try:
            attrs = dict(attrs) if attrs else {}
        except (TypeError, ValueError) as e:
            log.warning("Malformed attributes for tag <%s>: %s", tag, e)
            attrs = {}
            
        extra = {}

        if tag in ["b", "strong"]:
            entity = raw.types.MessageEntityBold
        elif tag in ["i", "em"]:
            entity = raw.types.MessageEntityItalic
        elif tag == "u":
            entity = raw.types.MessageEntityUnderline
        elif tag in ["s", "del", "strike"]:
            entity = raw.types.MessageEntityStrike
        elif tag == "blockquote":
            entity = raw.types.MessageEntityBlockquote
            extra["collapsed"] = "expandable" in attrs
        elif tag == "code":
            entity = raw.types.MessageEntityCode
        elif tag == "pre":
            entity = raw.types.MessageEntityPre
            extra["language"] = attrs.get("language", "")
        elif tag == "spoiler":
            entity = raw.types.MessageEntitySpoiler
        elif tag == "a":
            url = attrs.get("href", "")

            mention = Parser.MENTION_RE.match(url)

            if mention:
                entity = raw.types.InputMessageEntityMentionName
                try:
                    extra["user_id"] = int(mention.group(1))
                except (ValueError, TypeError) as e:
                    log.warning("Invalid user ID in mention: %s", e)
                    return
            else:
                entity = raw.types.MessageEntityTextUrl
                extra["url"] = url
        elif tag == "emoji":
            entity = raw.types.MessageEntityCustomEmoji
            try:
                custom_emoji_id_str = attrs.get("id")
                if custom_emoji_id_str is None:
                    log.warning("Custom emoji ID is missing in HTML tag, skipping entity.")
                    return

                custom_emoji_id = int(custom_emoji_id_str)

                min_val = -9223372036854775808
                max_val = 9223372036854775807

                if not (min_val <= custom_emoji_id <= max_val):
                    log.warning(
                        f"Custom emoji ID {custom_emoji_id} from HTML attribute 'id' is out of 64-bit integer range. "
                        f"Skipping this custom emoji entity to prevent OverflowError."
                    )
                    return

                extra["document_id"] = custom_emoji_id
            except (ValueError, TypeError) as e:
                problematic_id_val = attrs.get('id')
                log.warning(f"Invalid custom emoji ID format in HTML attribute 'id': '{problematic_id_val}'. Error: {e}. Skipping entity.")
                return
            except Exception as e:
                log.warning(f"Unexpected error processing custom emoji ID from HTML: {e}. Skipping entity.")
                return
        else:
            return

        if tag not in self.tag_entities:
            self.tag_entities[tag] = []

        try:
            self.tag_entities[tag].append(entity(offset=len(self.text), length=0, **extra))
        except Exception as e:
            log.warning("Failed to create entity for tag <%s>: %s", tag, e)

    def handle_data(self, data):
        if not data:
            return
            
        data = html.unescape(data)
        data_length = len(data)

        # Optimize: avoid nested loops when possible
        for entities in self.tag_entities.values():
            for entity in entities:
                entity.length += data_length

        self.text += data

    def handle_endtag(self, tag):
        try:
            self.entities.append(self.tag_entities[tag].pop())
        except (KeyError, IndexError):
            line, offset = self.getpos()
            offset += 1

            log.debug("Unmatched closing tag </%s> at line %s:%s", tag, line, offset)
        else:
            if not self.tag_entities[tag]:
                self.tag_entities.pop(tag)

    def error(self, message):
        log.warning("HTML parsing error: %s", message)


class HTML:
    def __init__(self, client: Optional["pyrogram.Client"]):
        self.client = client

    async def parse(self, text: str) -> dict:
        # Input validation
        if not isinstance(text, str):
            raise TypeError("Text must be a string")
        
        if not text:
            return {"message": "", "entities": None}

        # Strip whitespaces from the beginning and the end, but preserve closing tags
        # Use precompiled regex for better performance
        text = Parser.WHITESPACE_START_RE.sub(r"\1", text)
        text = Parser.WHITESPACE_END_RE.sub(r"\1", text)

        parser = Parser(self.client)
        
        try:
            parser.feed(utils.add_surrogates(text))
            parser.close()
        except Exception as e:
            log.error("Failed to parse HTML: %s", e)
            # Return plain text as fallback
            return {"message": text, "entities": None}

        if parser.tag_entities:
            unclosed_tags = []

            for tag, entities in parser.tag_entities.items():
                unclosed_tags.append(f"<{tag}> (x{len(entities)})")

            log.info("Unclosed tags: %s", ", ".join(unclosed_tags))

        entities = []

        for entity in parser.entities:
            if isinstance(entity, raw.types.InputMessageEntityMentionName):
                try:
                    if self.client is not None:
                        entity.user_id = await self.client.resolve_peer(entity.user_id)
                except PeerIdInvalid:
                    continue
                except Exception as e:
                    log.warning("Failed to resolve peer: %s", e)
                    continue

            entities.append(entity)

        # Remove zero-length entities
        entities = [entity for entity in entities if entity.length > 0]

        return {
            "message": utils.remove_surrogates(parser.text),
            "entities": sorted(entities, key=lambda e: e.offset) or None
        }

    @staticmethod
    def unparse(text: str, entities: list) -> str:
        # Input validation
        if not isinstance(text, str):
            raise TypeError("Text must be a string")
        
        if not entities:
            return text
            
        if not isinstance(entities, list):
            raise TypeError("Entities must be a list")

        def parse_one(entity):
            """
            Parses a single entity and returns (start_tag, start), (end_tag, end)
            """
            try:
                entity_type = entity.type
                start = entity.offset
                end = start + entity.length

                if entity_type in (
                    MessageEntityType.BOLD,
                    MessageEntityType.ITALIC,
                    MessageEntityType.UNDERLINE,
                    MessageEntityType.STRIKETHROUGH,
                ):
                    name = entity_type.name[0].lower()
                    start_tag = f"<{name}>"
                    end_tag = f"</{name}>"
                elif entity_type == MessageEntityType.PRE:
                    name = entity_type.name.lower()
                    language = getattr(entity, "language", "") or ""
                    start_tag = f'<{name} language="{language}">' if language else f"<{name}>"
                    end_tag = f"</{name}>"
                elif entity_type == MessageEntityType.BLOCKQUOTE:
                    name = entity_type.name.lower()
                    expandable = getattr(entity, "expandable", False)
                    start_tag = f'<{name}{" expandable" if expandable else ""}>'
                    end_tag = f"</{name}>"
                elif entity_type in (
                    MessageEntityType.CODE,
                    MessageEntityType.SPOILER,
                ):
                    name = entity_type.name.lower()
                    start_tag = f"<{name}>"
                    end_tag = f"</{name}>"
                elif entity_type == MessageEntityType.TEXT_LINK:
                    url = getattr(entity, "url", "")
                    if not url:
                        return None
                    start_tag = f'<a href="{html.escape(url)}">'
                    end_tag = "</a>"
                elif entity_type == MessageEntityType.TEXT_MENTION:
                    user = getattr(entity, "user", None)
                    if not user or not hasattr(user, "id"):
                        return None
                    start_tag = f'<a href="tg://user?id={user.id}">'
                    end_tag = "</a>"
                elif entity_type == MessageEntityType.CUSTOM_EMOJI:
                    custom_emoji_id = getattr(entity, "custom_emoji_id", None)
                    if not custom_emoji_id:
                        return None
                    start_tag = f'<emoji id="{custom_emoji_id}">'
                    end_tag = "</emoji>"
                else:
                    return None

                return (start_tag, start), (end_tag, end)
            except Exception as e:
                log.warning("Failed to parse entity: %s", e)
                return None

        def recursive(entity_i: int) -> int:
            """
            Takes the index of the entity to start parsing from, returns the number of parsed entities inside it.
            Uses entities_offsets as a stack, pushing (start_tag, start) first, then parsing nested entities,
            and finally pushing (end_tag, end) to the stack.
            No need to sort at the end.
            """
            this = parse_one(entities[entity_i])
            if this is None:
                return 1
            (start_tag, start), (end_tag, end) = this
            entities_offsets.append((start_tag, start))
            internal_i = entity_i + 1
            # while the next entity is inside the current one, keep parsing
            while internal_i < len(entities) and entities[internal_i].offset < end:
                internal_i += recursive(internal_i)
            entities_offsets.append((end_tag, end))
            return internal_i - entity_i

        text = utils.add_surrogates(text)

        entities_offsets = []

        # Filter out invalid entities before processing
        valid_entities = [e for e in entities if hasattr(e, 'offset') and hasattr(e, 'length') and e.length > 0]
        
        # Sort entities by offset and length
        valid_entities.sort(key=lambda e: (e.offset, -e.length))

        # main loop for first-level entities
        i = 0
        while i < len(valid_entities):
            i += recursive(i)

        if entities_offsets:
            # Use list for better performance with large texts
            text_parts = []
            last_offset = entities_offsets[-1][1]
            
            # Process entities from end to start to avoid offset shifting
            for entity, offset in reversed(entities_offsets):
                if offset < last_offset:
                    text_parts.append(html.escape(text[offset:last_offset]))
                text_parts.append(entity)
                last_offset = offset
            
            # Add remaining text at the beginning
            if last_offset > 0:
                text_parts.append(text[:last_offset])
            
            # Reverse and join for final result
            text = ''.join(reversed(text_parts))

        return utils.remove_surrogates(text)
