"""
Analysis Output Writer.

Handles persistence of interview analysis results to JSON files.

Thread Safety:
    File operations are atomic at the write level but not at the read-modify-write
    level. For concurrent access to the same session file, external locking is required.

Last Grunted: 02/05/2026
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from .models import AnalysisItem, SessionAnalysis


__all__ = ["AnalysisOutputWriter", "OutputWriteError", "OutputReadError"]


logger = logging.getLogger(__name__)


class OutputWriteError(Exception):
    """Raised when writing analysis output fails."""
    
    def __init__(self, path: Path, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"Failed to write to {path}: {cause}")


class OutputReadError(Exception):
    """Raised when reading analysis output fails."""
    
    def __init__(self, path: Path, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"Failed to read from {path}: {cause}")


def _format_utc_timestamp() -> str:
    """Return current UTC timestamp as ISO 8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _merge_by_id(data: dict, key: str, incoming: list[dict]) -> None:
    """Upsert items into a list-of-dicts on ``data[key]`` by their ``id`` field."""
    if not incoming:
        return
    existing = list(data.get(key) or [])
    by_id: dict[str, dict] = {}
    for entry in existing:
        entry_id = entry.get("id")
        if entry_id:
            by_id[str(entry_id)] = entry
    for entry in incoming:
        entry_id = entry.get("id")
        if entry_id:
            by_id[str(entry_id)] = entry
    data[key] = list(by_id.values())[-200:]


class AnalysisOutputWriter:
    """
    Writes interview analysis results to JSON files.
    
    Supports both full session analysis writes and incremental
    item appends for real-time analysis updates.
    
    Output files are named: {session_id}_analysis.json
    
    Example:
        >>> writer = AnalysisOutputWriter(Path("./output"))
        >>> writer.write_analysis("int_20260131_103000", analysis)
        >>> writer.append_item("int_20260131_103000", new_item)
    """
    
    def __init__(self, output_dir: Path) -> None:
        """
        Initialize the output writer.
        
        Args:
            output_dir: Directory where analysis JSON files will be written.
                       Created if it doesn't exist.
        """
        self.output_dir = Path(output_dir)
        self._ensure_output_dir()
    
    def _ensure_output_dir(self) -> None:
        """
        Create output directory if it doesn't exist.
        
        Raises:
            OutputWriteError: If directory creation fails.
        """
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            logger.debug("Output directory ready: %s", self.output_dir)
        except OSError as e:
            raise OutputWriteError(self.output_dir, e) from e
    
    def _get_output_path(self, session_id: str) -> Path:
        """Get the output file path for a session."""
        return self.output_dir / f"{session_id}_analysis.json"
    
    def write_analysis(self, session_id: str, analysis: SessionAnalysis) -> Path:
        """
        Write a complete session analysis to a JSON file.
        
        Overwrites any existing file for this session.
        
        Args:
            session_id: The session identifier.
            analysis: The complete SessionAnalysis to write.
            
        Returns:
            Path to the written file.
            
        Raises:
            OutputWriteError: If file write fails.
            
        Example:
            >>> analysis = SessionAnalysis(
            ...     session_id="int_20260131_103000",
            ...     candidate_name="John Smith",
            ...     started_at="2026-01-31T10:30:00.000Z"
            ... )
            >>> path = writer.write_analysis("int_20260131_103000", analysis)
            >>> print(path)
            PosixPath('./output/int_20260131_103000_analysis.json')
        """
        output_path = self._get_output_path(session_id)
        
        # Compute overall scores before writing
        analysis.compute_overall_scores()
        
        # Convert to dict and add metadata
        data = analysis.model_dump()
        data["_meta"] = {
            "written_at": _format_utc_timestamp(),
            "version": "1.0",
        }
        
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            raise OutputWriteError(output_path, e) from e
        
        logger.info("Wrote analysis to %s", output_path)
        return output_path
    
    def append_item(
        self,
        session_id: str,
        item: AnalysisItem,
        checklist_state: Optional[list[dict[str, str | None]]] = None,
    ) -> Path:
        """
        Append a new analysis item to an existing session file.
        
        If no file exists for this session, creates a new one with
        minimal session data.
        
        Args:
            session_id: The session identifier.
            item: The AnalysisItem to append.
            
        Returns:
            Path to the updated file.
            
        Raises:
            OutputWriteError: If file read or write fails.
            OutputReadError: If existing file contains invalid JSON.
            
        Example:
            >>> item = AnalysisItem(
            ...     response_id="resp_001",
            ...     response_text="I have experience with...",
            ...     relevance_score=0.85,
            ...     clarity_score=0.90
            ... )
            >>> path = writer.append_item("int_20260131_103000", item)
        """
        output_path = self._get_output_path(session_id)
        current_timestamp = _format_utc_timestamp()
        
        # Load existing data or create minimal structure
        if output_path.exists():
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                raise OutputReadError(output_path, e) from e
            except OSError as e:
                raise OutputReadError(output_path, e) from e
        else:
            # Create minimal structure
            data = {
                "session_id": session_id,
                "candidate_name": "Unknown",
                "started_at": current_timestamp,
                "ended_at": None,
                "analysis_items": [],
                "overall_relevance": None,
                "overall_clarity": None,
                "total_responses_analyzed": 0,
                "checklist_state": checklist_state or [],
                "running_summary": "",
                "topics": [],
                "notes": [],
                "decisions": [],
                "open_questions": [],
                "action_items": [],
                "risks": [],
                "_meta": {
                    "created_at": current_timestamp,
                    "version": "1.0",
                },
            }
        
        # Append new item
        data["analysis_items"].append(item.model_dump())
        data["total_responses_analyzed"] = len(data["analysis_items"])

        extraction = item.extraction
        if extraction is not None:
            if extraction.running_summary:
                data["running_summary"] = extraction.running_summary
            if extraction.topics:
                data["topics"] = list(extraction.topics)
            if extraction.notes:
                notes = list(data.get("notes") or [])
                notes.extend(extraction.notes)
                data["notes"] = notes[-200:]
            # Intent-alignment rolling state: merge by id.
            _merge_by_id(data, "decisions", [d.model_dump() for d in extraction.decisions])
            _merge_by_id(data, "open_questions", [q.model_dump() for q in extraction.open_questions])
            _merge_by_id(data, "action_items", [a.model_dump() for a in extraction.action_items])
            _merge_by_id(data, "risks", [r.model_dump() for r in extraction.risks])

        # Recompute overall scores using only scored items.
        scored_items = [
            analysis_item
            for analysis_item in data["analysis_items"]
            if analysis_item.get("relevance_score") is not None
            and analysis_item.get("clarity_score") is not None
        ]
        if scored_items:
            data["overall_relevance"] = (
                sum(float(i["relevance_score"]) for i in scored_items) / len(scored_items)
            )
            data["overall_clarity"] = (
                sum(float(i["clarity_score"]) for i in scored_items) / len(scored_items)
            )
        else:
            data["overall_relevance"] = None
            data["overall_clarity"] = None

        if checklist_state is not None:
            data["checklist_state"] = checklist_state
        
        # Update metadata
        data["_meta"]["last_updated_at"] = current_timestamp
        
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            raise OutputWriteError(output_path, e) from e
        
        logger.info("Appended item %s to %s", item.response_id, output_path)
        return output_path
    
    def load_analysis(self, session_id: str) -> Optional[SessionAnalysis]:
        """
        Load an existing analysis from file.
        
        Args:
            session_id: The session identifier.
            
        Returns:
            SessionAnalysis if file exists, None otherwise.
            
        Raises:
            OutputReadError: If file read fails or contains invalid JSON/data.
        """
        output_path = self._get_output_path(session_id)
        
        if not output_path.exists():
            logger.debug("No analysis file found for session %s", session_id)
            return None
        
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise OutputReadError(output_path, e) from e
        except OSError as e:
            raise OutputReadError(output_path, e) from e
        
        # Remove metadata before parsing
        data.pop("_meta", None)
        
        try:
            return SessionAnalysis.model_validate(data)
        except ValidationError as e:
            raise OutputReadError(output_path, e) from e
    
    def list_sessions(self) -> list[str]:
        """
        List all session IDs with analysis files.
        
        Returns:
            List of session IDs.
        """
        self._ensure_output_dir()
        
        session_ids = []
        for path in self.output_dir.glob("*_analysis.json"):
            # Extract session_id from filename
            session_id = path.stem.replace("_analysis", "")
            session_ids.append(session_id)
        
        return sorted(session_ids)
    
    def delete_analysis(self, session_id: str) -> bool:
        """
        Delete an analysis file.
        
        Args:
            session_id: The session identifier.
            
        Returns:
            True if file was deleted, False if it didn't exist.
            
        Raises:
            OutputWriteError: If file deletion fails due to permissions or other OS error.
        """
        output_path = self._get_output_path(session_id)
        
        if not output_path.exists():
            logger.debug("No analysis file to delete for session %s", session_id)
            return False
        
        try:
            output_path.unlink()
            logger.info("Deleted analysis file for session %s", session_id)
            return True
        except OSError as e:
            raise OutputWriteError(output_path, e) from e
