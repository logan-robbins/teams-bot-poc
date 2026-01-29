"""
Python Transcript Receiver with Agent Integration Support
Based on Part I (I4) and Part D (D12) of the validated guide

This receives real-time transcript events from the C# bot and provides
an async queue for agent framework integration.
"""
from fastapi import FastAPI, Request
import uvicorn
import asyncio
from datetime import datetime
from typing import Optional
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Teams Transcript Sink", version="1.0.0")

# Async queue for agent consumption
transcript_queue: asyncio.Queue[dict] = asyncio.Queue()

# Stats tracking
stats = {
    "events_received": 0,
    "partial_transcripts": 0,
    "final_transcripts": 0,
    "errors": 0,
    "session_started": 0,
    "session_stopped": 0,
    "started_at": datetime.utcnow().isoformat()
}


@app.post("/transcript")
async def receive_transcript(req: Request):
    """
    Receive transcript events from C# bot
    
    Event format:
    {
        "Kind": "recognizing" | "recognized" | "session_started" | "session_stopped" | "canceled",
        "Text": "transcript text" | null,
        "TsUtc": "2026-01-28T20:33:12.3456789Z",
        "Details": "optional error details" | null
    }
    """
    try:
        payload = await req.json()
        
        # Normalize field names (C# uses PascalCase, handle both cases)
        kind = payload.get("Kind") or payload.get("kind", "unknown")
        text = payload.get("Text") or payload.get("text")
        ts_utc = payload.get("TsUtc") or payload.get("tsUtc")
        details = payload.get("Details") or payload.get("details")
        
        # Update stats
        stats["events_received"] += 1
        
        if kind == "recognizing":
            stats["partial_transcripts"] += 1
            logger.debug(f"[PARTIAL] {text}")
        elif kind == "recognized":
            stats["final_transcripts"] += 1
            logger.info(f"[FINAL] {text}")
        elif kind == "session_started":
            stats["session_started"] += 1
            logger.info("Speech recognition session started")
        elif kind == "session_stopped":
            stats["session_stopped"] += 1
            logger.info("Speech recognition session stopped")
        elif kind == "canceled":
            stats["errors"] += 1
            logger.error(f"Speech recognition error: {details}")
        
        # Push to agent queue for processing
        await transcript_queue.put({
            "kind": kind,
            "text": text,
            "timestamp": ts_utc,
            "details": details
        })
        
        return {"ok": True, "received_at": datetime.utcnow().isoformat()}
        
    except Exception as e:
        logger.error(f"Error processing transcript: {e}", exc_info=True)
        stats["errors"] += 1
        return {"ok": False, "error": str(e)}, 500


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Teams Transcript Sink",
        "timestamp": datetime.utcnow().isoformat(),
        "stats": stats
    }


@app.get("/stats")
async def get_stats():
    """Get current statistics"""
    return {
        "stats": stats,
        "queue_size": transcript_queue.qsize()
    }


async def agent_processing_loop():
    """
    Background task that processes transcript events
    
    This is where you integrate with your agent framework.
    Example integrations:
    - LangChain agent
    - Custom agent logic
    - External API calls
    - Database storage
    """
    logger.info("Agent processing loop started")
    
    while True:
        try:
            # Wait for next transcript event
            evt = await transcript_queue.get()
            
            kind = evt["kind"]
            text = evt["text"]
            
            # Only process final transcripts for agent input
            # (ignore partial/recognizing events to reduce noise)
            if kind == "recognized" and text:
                logger.info(f"AGENT_INPUT: {text}")
                
                # TODO: Replace with your agent framework integration
                # Examples:
                # - await agent.process_input(text)
                # - await langchain_agent.arun(text)
                # - await custom_agent.handle_transcript(text)
                # - await db.save_transcript(text, evt["timestamp"])
                
                # For now, just log it
                pass
                
        except Exception as e:
            logger.error(f"Error in agent processing loop: {e}", exc_info=True)
            await asyncio.sleep(1)  # Brief pause on error


if __name__ == "__main__":
    logger.info("Starting Teams Transcript Sink on http://127.0.0.1:8765")
    logger.info("Transcript endpoint: POST /transcript")
    logger.info("Health check: GET /health")
    logger.info("Stats: GET /stats")
    
    # Start background agent processing loop
    loop = asyncio.get_event_loop()
    loop.create_task(agent_processing_loop())
    
    # Start FastAPI server
    uvicorn.run(
        app,
        host="0.0.0.0",  # Bind to all interfaces (allows Windows VM to connect)
        port=8765,
        log_level="info"
    )
