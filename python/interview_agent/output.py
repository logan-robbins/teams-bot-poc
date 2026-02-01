"""
Analysis Output Writer.

Handles persistence of interview analysis results to JSON files.

Last Grunted: 01/31/2026
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import AnalysisItem, SessionAnalysis


logger = logging.getLogger(__name__)


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
        """Create output directory if it doesn't exist."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Output directory ready: {self.output_dir}")
    
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
            "written_at": datetime.utcnow().isoformat() + "Z",
            "version": "1.0",
        }
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Wrote analysis to {output_path}")
        return output_path
    
    def append_item(self, session_id: str, item: AnalysisItem) -> Path:
        """
        Append a new analysis item to an existing session file.
        
        If no file exists for this session, creates a new one with
        minimal session data.
        
        Args:
            session_id: The session identifier.
            item: The AnalysisItem to append.
            
        Returns:
            Path to the updated file.
            
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
        
        # Load existing data or create minimal structure
        if output_path.exists():
            with open(output_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            # Create minimal structure
            data = {
                "session_id": session_id,
                "candidate_name": "Unknown",
                "started_at": datetime.utcnow().isoformat() + "Z",
                "ended_at": None,
                "analysis_items": [],
                "overall_relevance": None,
                "overall_clarity": None,
                "total_responses_analyzed": 0,
                "_meta": {
                    "created_at": datetime.utcnow().isoformat() + "Z",
                    "version": "1.0",
                },
            }
        
        # Append new item
        data["analysis_items"].append(item.model_dump())
        data["total_responses_analyzed"] = len(data["analysis_items"])
        
        # Recompute overall scores
        items = data["analysis_items"]
        if items:
            data["overall_relevance"] = sum(i["relevance_score"] for i in items) / len(items)
            data["overall_clarity"] = sum(i["clarity_score"] for i in items) / len(items)
        
        # Update metadata
        data["_meta"]["last_updated_at"] = datetime.utcnow().isoformat() + "Z"
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Appended item {item.response_id} to {output_path}")
        return output_path
    
    def load_analysis(self, session_id: str) -> Optional[SessionAnalysis]:
        """
        Load an existing analysis from file.
        
        Args:
            session_id: The session identifier.
            
        Returns:
            SessionAnalysis if file exists, None otherwise.
        """
        output_path = self._get_output_path(session_id)
        
        if not output_path.exists():
            logger.debug(f"No analysis file found for session {session_id}")
            return None
        
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Remove metadata before parsing
        data.pop("_meta", None)
        
        return SessionAnalysis.model_validate(data)
    
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
        """
        output_path = self._get_output_path(session_id)
        
        if output_path.exists():
            output_path.unlink()
            logger.info(f"Deleted analysis file for session {session_id}")
            return True
        
        return False
