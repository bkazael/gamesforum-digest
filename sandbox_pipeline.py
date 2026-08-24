#!/usr/bin/env python3
"""
Sandbox Pipeline for GamesForum Digest
--------------------------------------
An isolated test runner based on the production engine.
Designed to run in GitHub Actions / local sandbox without touching production feed.xml.
"""

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path

# External dependencies
import tomli
from google import genai
from google.genai import types

# Project imports
from discovery import DiscoveryEngine

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("sandbox_pipeline")

OUTPUT_DIR = Path("./output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def load_profile(profile_path="profile.toml"):
    """Loads profile configuration."""
    with open(profile_path, "rb") as f:
        return tomli.load(f)

def run_sandbox():
    logger.info("=== Starting Sandbox Pipeline Episode Generation ===")

    # 1. Check API Key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable is missing!")
        sys.exit(1)

    # 2. Discovery Phase
    logger.info("Step 1: Running Discovery Engine...")
    engine = DiscoveryEngine(profile_path="profile.toml", memory_path="memory.json")
    articles, _ = engine.run_discovery()

    if not articles:
        logger.warning("No new articles discovered. Using fallback/recent articles if available.")
    else:
        logger.info(f"Discovered {len(articles)} eligible articles.")

    # 3. Load Profile
    profile = load_profile("profile.toml")

    # 4. Generate Script via Gemini
    logger.info("Step 2: Generating Episode Script via Gemini...")
    client = genai.Client(api_key=api_key)

    system_instruction = f"""
    You are an expert podcast scriptwriter for 'GamesForum Digest'.
    Target Audience: {profile.get('target_audience', 'Mobile Gaming Professionals')}
    Host Voices: Dana & Yoni
    Format: Conversational Hebrew podcast script in JSON.
    """

    prompt = f"""
    Create a sandbox test episode script based on these articles:
    {json.dumps(articles[:5], ensure_ascii=False, indent=2)}
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.7,
            )
        )
        script_data = json.loads(response.text)
        logger.info("Script generated successfully.")
    except Exception as e:
        logger.error(f"Failed to generate script: {e}")
        sys.exit(1)

    # Save Script Artifact
    script_file = OUTPUT_DIR / f"test_script_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(script_file, "w", encoding="utf-8") as f:
        json.dump(script_data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved script artifact to {script_file}")

    # 5. TTS Audio Generation Mock / Call
    logger.info("Step 3: Processing Audio Generation...")
    audio_file = OUTPUT_DIR / "test_episode.mp3"
    
    # Simple placeholder audio generation log or actual TTS call
    with open(audio_file, "wb") as f:
        f.write(b"MOCK_AUDIO_DATA_FOR_SANDBOX")
    
    logger.info(f"Saved test audio artifact to {audio_file}")
    logger.info("=== Sandbox Run Completed Successfully (Production feed.xml untouched) ===")

if __name__ == "__main__":
    run_sandbox()