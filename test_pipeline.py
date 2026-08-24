"""
test_pipeline.py - מערכת בדיקות מהירה, חסכונית ובטוחה (כולל זיכרון פרקים).

איך מריצים:
python test_pipeline.py
"""

import unittest
from unittest.mock import MagicMock, patch
import os
import json
from discovery import DiscoveryEngine


class TestPodcastPipelineDryRun(unittest.TestCase):

    def setUp(self):
        """הכנת נתוני דמה לבדיקה."""
        self.mock_articles = [
            {
                "title": "Skillz and Papaya Gaming Lawsuit Updates: Q3 2026",
                "summary": "Court documents reveal new metrics on ROAS, UA efficiency, and SDK integration.",
                "url": "https://example.com/article1"
            },
            {
                "title": "REGISTER NOW for the Gaming Conference 2026",
                "summary": "Join us for a web event with free tickets and job opportunities.",
                "url": "https://example.com/article2"
            }
        ]

    def test_block_filter(self):
        """בדיקה שמנגנון ה-BLOCK מסנן כותרות זבל."""
        engine = DiscoveryEngine()
        self.assertFalse(engine.filter_blacklisted_titles(self.mock_articles[1]["title"]))
        self.assertTrue(engine.filter_blacklisted_titles(self.mock_articles[0]["title"]))

    def test_memory_context_loading(self):
        """בדיקת טעינת קטע הזיכרון מפרקים קודמים."""
        engine = DiscoveryEngine()
        context = engine.get_recent_memory_context()
        self.assertIsInstance(context, str)

    @patch("discovery.get_gemini_client")
    def test_full_discovery_pipeline_mocked(self, mock_gemini_client):
        """בדיקת ה-Discovery המלא בסימולציה ללא חיוב טוקנים."""
        mock_client_instance = MagicMock()
        mock_gemini_client.return_value = mock_client_instance

        response_good = MagicMock()
        response_good.text = json.dumps({
            "score": 8.5,
            "reasoning": "High relevance to skill gaming litigation.",
            "is_duplicate": False
        })

        mock_client_instance.models.generate_content.return_value = response_good

        engine = DiscoveryEngine()
        approved, rejected = engine.run_discovery(mock_items=[self.mock_articles[0]])

        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["score"], 8.5)


if __name__ == "__main__":
    print("=== מריץ בדיקות Dry-Run כולל זיכרון (Zero-Token) ===")
    unittest.main()