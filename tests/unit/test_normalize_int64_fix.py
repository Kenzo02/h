#  Test file for document_id overflow fix
import pytest
from io import BytesIO

from pyrogram import utils
from pyrogram.types.user_and_chats.emoji_status import EmojiStatus
from pyrogram.types.messages_and_media.upgraded_gift_attribute_id_symbol import UpgradedGiftAttributeIdSymbol
from pyrogram.types.messages_and_media.upgraded_gift_attribute_id_model import UpgradedGiftAttributeIdModel


class TestNormalizeInt64Fix:
    """Test that utils.normalize_int64() is applied correctly to document_id values."""
    
    def test_emoji_status_write_preserves_large_id(self):
        """EmojiStatus.write() should preserve large positive int64 values."""
        test_id = 5319161050128459957
        status = EmojiStatus(custom_emoji_id=test_id)
        result = status.write()
        assert result.document_id == test_id, f"Expected {test_id}, got {result.document_id}"

    def test_upgraded_gift_attribute_symbol_write_preserves_large_id(self):
        """UpgradedGiftAttributeIdSymbol.write() should preserve large positive int64 values."""
        test_id = 5319161050128459957
        symbol = UpgradedGiftAttributeIdSymbol(sticker_id=test_id)
        result = symbol.write()
        assert result.document_id == test_id, f"Expected {test_id}, got {result.document_id}"

    def test_upgraded_gift_attribute_model_write_preserves_large_id(self):
        """UpgradedGiftAttributeIdModel.write() should preserve large positive int64 values."""
        test_id = 5319161050128459957
        model = UpgradedGiftAttributeIdModel(sticker_id=test_id)
        result = model.write()
        assert result.document_id == test_id, f"Expected {test_id}, got {result.document_id}"

    def test_normalize_int64_function(self):
        """utils.normalize_int64() should correctly handle large positive int64 values."""
        test_id = 5319161050128459957
        result = utils.normalize_int64(test_id)
        assert result == test_id, f"Expected {test_id}, got {result}"
        
        # Also test small values stay unchanged
        small_id = 12345
        result_small = utils.normalize_int64(small_id)
        assert result_small == small_id, f"Expected {small_id}, got {result_small}"


class TestNormalizeInt64BoundaryValues:
    """Test boundary values for int64 normalization."""
    
    def test_normalize_int64_at_signed_max(self):
        """Value at signed max (2^63 - 1) should NOT be normalized."""
        max_signed = (1 << 63) - 1  # 9223372036854775807
        result = utils.normalize_int64(max_signed)
        assert result == max_signed, f"Expected {max_signed}, got {result}"

    def test_normalize_int64_at_signed_min(self):
        """Value at signed min (-2^63) should NOT be normalized."""
        min_signed = -(1 << 63)  # -9223372036854775808
        result = utils.normalize_int64(min_signed)
        assert result == min_signed, f"Expected {min_signed}, got {result}"

    def test_normalize_int64_just_above_signed_max(self):
        """Value just above signed max (2^63) SHOULD be normalized."""
        just_over = 1 << 63  # 9223372036854775808
        expected = just_over - (1 << 64)  # -9223372036854775808
        result = utils.normalize_int64(just_over)
        assert result == expected, f"Expected {expected}, got {result}"

    def test_normalize_int64_large_unsigned(self):
        """Large unsigned value (> 2^63) SHOULD be normalized to signed."""
        large_unsigned = 10953737161318060097
        expected_signed = large_unsigned - (1 << 64)  # becomes negative: -7493006912391491519
        result = utils.normalize_int64(large_unsigned)
        assert result == expected_signed, f"Expected {expected_signed}, got {result}"

    def test_normalize_int64_already_negative(self):
        """Negative values should pass through unchanged."""
        neg_value = -7492987012391491519
        result = utils.normalize_int64(neg_value)
        assert result == neg_value, f"Expected {neg_value}, got {result}"

    def test_normalize_int64_zero(self):
        """Zero should pass through unchanged."""
        result = utils.normalize_int64(0)
        assert result == 0, f"Expected 0, got {result}"

    def test_normalize_int64_none(self):
        """None should pass through unchanged."""
        result = utils.normalize_int64(None)
        assert result is None, f"Expected None, got {result}"

    def test_normalize_int64_small_positive(self):
        """Small positive values should pass through unchanged."""
        small_val = 42
        result = utils.normalize_int64(small_val)
        assert result == small_val, f"Expected {small_val}, got {result}"

    def test_normalize_int64_max_unsigned(self):
        """Test max unsigned value (2^64 - 1)"""
        from pyrogram.utils import normalize_int64
        
        max_unsigned = (1 << 64) - 1  # 18446744073709551615
        expected = max_unsigned - (1 << 64)  # -1
        result = normalize_int64(max_unsigned)
        assert result == expected, f"Expected {expected}, got {result}"

    def test_normalize_int64_just_below_boundary(self):
        """Test value just below normalization boundary (2^63 - 2)"""
        from pyrogram.utils import normalize_int64
        
        just_below = (1 << 63) - 2  # 9223372036854775806
        result = normalize_int64(just_below)
        # Should NOT be normalized
        assert result == just_below, f"Expected {just_below}, got {result}"


class TestHTMLParserCustomEmoji:
    """Test HTML parser correctly normalizes large custom emoji IDs."""

    def test_html_parser_custom_emoji_large_id(self):
        """Test HTML parser normalizes large custom emoji IDs to signed values."""
        from pyrogram.parser.html import Parser
        
        large_id = 10953737161318060097  # unsigned, > 2^63
        html_text = f'<emoji id="{large_id}">🎉</emoji>'
        
        parser = Parser(None)  # client is None for parsing
        parser.feed(html_text)
        parser.close()
        
        assert parser.text == "🎉"
        assert len(parser.entities) == 1
        
        entity = parser.entities[0]
        # The document_id should be normalized to signed
        expected_signed = large_id - (1 << 64)  # -7493006912391491519
        assert entity.document_id == expected_signed, f"Expected {expected_signed}, got {entity.document_id}"

    def test_html_parser_custom_emoji_normal_id(self):
        """Test HTML parser preserves normal (within signed range) emoji IDs."""
        from pyrogram.parser.html import Parser
        
        normal_id = 5319161050128459957  # within signed range
        html_text = f'<emoji id="{normal_id}">⭐</emoji>'
        
        parser = Parser(None)
        parser.feed(html_text)
        parser.close()
        
        assert parser.text == "⭐"
        assert len(parser.entities) == 1
        
        entity = parser.entities[0]
        assert entity.document_id == normal_id, f"Expected {normal_id}, got {entity.document_id}"

    def test_html_parser_custom_emoji_at_boundary(self):
        """Test HTML parser correctly handles value at 2^63 boundary."""
        from pyrogram.parser.html import Parser
        
        boundary_id = 1 << 63  # 9223372036854775808
        html_text = f'<emoji id="{boundary_id}">🔥</emoji>'
        
        parser = Parser(None)
        parser.feed(html_text)
        parser.close()
        
        assert len(parser.entities) == 1
        
        entity = parser.entities[0]
        expected_signed = boundary_id - (1 << 64)  # -9223372036854775808
        assert entity.document_id == expected_signed, f"Expected {expected_signed}, got {entity.document_id}"


class TestMessageEntityWrite:
    """Test MessageEntity._write() normalizes custom_emoji_id."""

    def test_message_entity_write_pre_with_language(self):
        """Test MessageEntity.write() preserves PRE language entities."""
        import asyncio
        import inspect
        from pyrogram import raw
        from pyrogram.types import MessageEntity
        from pyrogram.enums import MessageEntityType

        entity = MessageEntity(
            type=MessageEntityType.PRE,
            offset=0,
            length=4,
            language="py"
        )

        result = entity.write()
        if inspect.iscoroutine(result):
            raw_entity = asyncio.run(result)
        else:
            raw_entity = result

        assert isinstance(raw_entity, raw.types.MessageEntityPre)
        assert raw_entity.language == "py"

    def test_message_entity_write_normalizes_large_id(self):
        """Test MessageEntity.write() normalizes large custom_emoji_id."""
        import asyncio
        import inspect
        from pyrogram.types import MessageEntity
        from pyrogram.enums import MessageEntityType
        
        large_id = 10953737161318060097  # unsigned, > 2^63
        
        entity = MessageEntity(
            type=MessageEntityType.CUSTOM_EMOJI,
            offset=0,
            length=2,
            custom_emoji_id=large_id
        )
        
        # Mock client - MessageEntity.write() is async but doesn't need client for custom emoji
        class MockClient:
            pass
        
        entity._client = MockClient()
        
        # Call write to get raw entity - handle both sync and async returns
        result = entity.write()
        if inspect.iscoroutine(result):
            raw_entity = asyncio.run(result)
        else:
            raw_entity = result
        
        # The document_id should be normalized
        expected_signed = large_id - (1 << 64)  # -7493006912391491519
        assert raw_entity.document_id == expected_signed, f"Expected {expected_signed}, got {raw_entity.document_id}"

    def test_message_entity_write_normalizes_large_string_id(self):
        """Test MessageEntity.write() normalizes large string custom_emoji_id values."""
        import asyncio
        import inspect
        from pyrogram.types import MessageEntity
        from pyrogram.enums import MessageEntityType

        large_id = "10953737161318060097"

        entity = MessageEntity(
            type=MessageEntityType.CUSTOM_EMOJI,
            offset=0,
            length=2,
            custom_emoji_id=large_id
        )

        class MockClient:
            pass

        entity._client = MockClient()

        result = entity.write()
        if inspect.iscoroutine(result):
            raw_entity = asyncio.run(result)
        else:
            raw_entity = result

        expected_signed = int(large_id) - (1 << 64)
        assert raw_entity.document_id == expected_signed, f"Expected {expected_signed}, got {raw_entity.document_id}"

    def test_message_entity_write_preserves_normal_id(self):
        """Test MessageEntity.write() preserves normal (within signed range) IDs."""
        import asyncio
        import inspect
        from pyrogram.types import MessageEntity
        from pyrogram.enums import MessageEntityType
        
        normal_id = 5319161050128459957  # within signed range
        
        entity = MessageEntity(
            type=MessageEntityType.CUSTOM_EMOJI,
            offset=0,
            length=2,
            custom_emoji_id=normal_id
        )
        
        class MockClient:
            pass
        
        entity._client = MockClient()
        
        result = entity.write()
        if inspect.iscoroutine(result):
            raw_entity = asyncio.run(result)
        else:
            raw_entity = result

        assert raw_entity.document_id == normal_id, f"Expected {normal_id}, got {raw_entity.document_id}"

    def test_message_entity_write_with_already_negative_id(self):
        """Test MessageEntity.write() preserves already-negative IDs."""
        import asyncio
        import inspect
        from pyrogram.types import MessageEntity
        from pyrogram.enums import MessageEntityType
        
        negative_id = -7493006912391491519  # already negative (normalized)
        
        entity = MessageEntity(
            type=MessageEntityType.CUSTOM_EMOJI,
            offset=0,
            length=2,
            custom_emoji_id=negative_id
        )
        
        class MockClient:
            pass
        
        entity._client = MockClient()
        
        result = entity.write()
        if inspect.iscoroutine(result):
            raw_entity = asyncio.run(result)
        else:
            raw_entity = result

        assert raw_entity.document_id == negative_id, f"Expected {negative_id}, got {raw_entity.document_id}"


class TestLongSerialization:
    """Test Long primitive serialization with normalized values."""

    def test_long_serialization_with_normalized_value(self):
        """Test Long primitive accepts normalized (signed) values."""
        from pyrogram.raw.core.primitives import Long
        
        # Normalized (signed) value
        normalized_value = -7492987012391491519
        
        # This should NOT raise OverflowError
        serialized = Long(normalized_value)
        
        # Deserialize and verify
        deserialized = Long.read(BytesIO(serialized))
        assert deserialized == normalized_value, f"Expected {normalized_value}, got {deserialized}"

    def test_long_serialization_positive_max(self):
        """Test Long primitive accepts maximum positive signed value."""
        from pyrogram.raw.core.primitives import Long
        
        max_signed = (1 << 63) - 1  # 9223372036854775807
        
        serialized = Long(max_signed)
        deserialized = Long.read(BytesIO(serialized))
        assert deserialized == max_signed, f"Expected {max_signed}, got {deserialized}"

    def test_long_serialization_negative_min(self):
        """Test Long primitive accepts minimum negative signed value."""
        from pyrogram.raw.core.primitives import Long
        
        min_signed = -(1 << 63)  # -9223372036854775808
        
        serialized = Long(min_signed)
        deserialized = Long.read(BytesIO(serialized))
        assert deserialized == min_signed, f"Expected {min_signed}, got {deserialized}"

    def test_long_serialization_fails_with_unnormalized(self):
        """Test Long primitive fails with unnormalized unsigned values > 2^63-1."""
        from pyrogram.raw.core.primitives import Long
        
        # Unnormalized (unsigned) value > 2^63-1
        unsigned_value = 10953737161318060097
        
        # This SHOULD raise OverflowError because Long uses signed=True by default
        with pytest.raises(OverflowError):
            Long(unsigned_value)

    def test_long_roundtrip_with_normalize(self):
        """Test full roundtrip: normalize -> serialize -> deserialize."""
        from pyrogram.raw.core.primitives import Long
        
        # Start with unsigned value
        unsigned_value = 10953737161318060097
        
        # Normalize it
        normalized = utils.normalize_int64(unsigned_value)
        
        # Serialize
        serialized = Long(normalized)
        
        # Deserialize
        deserialized = Long.read(BytesIO(serialized))
        
        # Should match the normalized value
        assert deserialized == normalized, f"Expected {normalized}, got {deserialized}"
        
        # And the normalized value should be negative
        assert normalized < 0, f"Expected negative value, got {normalized}"


class TestFullSerializationFlow:
    """Test full serialization flow from user input to raw types."""

    def test_html_to_raw_entity_serialization(self):
        """Test HTML parsing produces entities that can be serialized."""
        from pyrogram.parser.html import Parser
        from pyrogram.raw.core.primitives import Long
        
        large_id = 10953737161318060097
        html_text = f'<emoji id="{large_id}">🎉</emoji>'
        
        parser = Parser(None)
        parser.feed(html_text)
        parser.close()
        
        entity = parser.entities[0]
        
        # The document_id should be serializable as a signed Long
        # This should NOT raise OverflowError
        serialized = Long(entity.document_id)
        deserialized = Long.read(BytesIO(serialized))
        
        assert deserialized == entity.document_id

    def test_equivalent_values_after_normalization(self):
        """Test that unsigned and signed representations are equivalent after normalization."""
        unsigned_value = 10953737161318060097
        expected_signed = -7493006912391491519  # unsigned_value - (1 << 64)
        
        # Normalize the unsigned value
        normalized = utils.normalize_int64(unsigned_value)
        
        # They should be equal
        assert normalized == expected_signed, f"Expected {expected_signed}, got {normalized}"
        
        # And both should serialize to the same bytes
        from pyrogram.raw.core.primitives import Long
        
        serialized_normalized = Long(normalized)
        serialized_expected = Long(expected_signed)
        
        assert serialized_normalized == serialized_expected
