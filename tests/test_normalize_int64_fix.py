#  Test file for document_id overflow fix
import pytest

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
