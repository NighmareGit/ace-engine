import re
import logging
import datetime
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

@dataclass
class EditBlock:
    path: str
    old_string: str
    new_string: str

@dataclass
class EditOp:
    target_file: str
    blocks: List[EditBlock]
    raw_response: str

class EditOpExtractor:
    """Parses LLM responses into structured EditOp dataclasses."""

    AIDER_PATTERN = re.compile(r"<<<<<<< SEARCH\s*(.*?)\s*=======\s*(.*?)\s*>>>>>>> REPLACE", re.DOTALL)
    XML_PATTERN = re.compile(r"<edit>\s*<old>(.*?)</old>\s*<new>(.*?)</new>\s*</edit>", re.DOTALL)

    @staticmethod
    def extract(raw_response: str, target_file: str) -> EditOp:
        blocks: List[EditBlock] = []

        try:
            # 1. Try Aider-style SEARCH/REPLACE fences
            for match in EditOpExtractor.AIDER_PATTERN.finditer(raw_response):
                try:
                    old_str = match.group(1).strip()
                    new_str = match.group(2).strip()
                    if not old_str or not new_str:
                        logger.warning("Skipping malformed Aider block: missing old or new string.")
                        continue
                    blocks.append(EditBlock(path=target_file, old_string=old_str, new_string=new_str))
                except Exception as e:
                    logger.warning(f"Skipping malformed Aider block due to parsing error: {e}")

            # If Aider format yielded blocks, return early
            if blocks:
                EditOpExtractor._log_session_row(target_file, len(blocks), raw_response)
                return EditOp(target_file=target_file, blocks=blocks, raw_response=raw_response)

            # 2. Fallback to XML <edit><old>/<new> tags
            for match in EditOpExtractor.XML_PATTERN.finditer(raw_response):
                try:
                    old_str = match.group(1).strip()
                    new_str = match.group(2).strip()
                    if not old_str or not new_str:
                        logger.warning("Skipping malformed XML block: missing old or new string.")
                        continue
                    blocks.append(EditBlock(path=target_file, old_string=old_str, new_string=new_str))
                except Exception as e:
                    logger.warning(f"Skipping malformed XML block due to parsing error: {e}")

        except Exception as e:
            logger.warning(f"Unexpected error during edit block extraction: {e}")

        EditOpExtractor._log_session_row(target_file, len(blocks), raw_response)
        return EditOp(target_file=target_file, blocks=blocks, raw_response=raw_response)

    @staticmethod
    def _log_session_row(target_file: str, blocks_count: int, raw_response: str) -> None:
        """Writes a session-log row per LLM call."""
        row = {
            "event": "edit_op_extract",
            "target_file": target_file,
            "blocks_parsed": blocks_count,
            "raw_response_length": len(raw_response),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        logger.info(row)