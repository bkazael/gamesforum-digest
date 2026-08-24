"""
discovery.py - מנגנון איסוף, סינון, דירוג וניהול זיכרון לפודקאסט משחקי מובייל.

מנוע זה מבצע:
1. איסוף כתבות מפידים ב-RSS ועדכוני HTML.
2. סינון ראשוני של כותרות זבל/שיווקיות (BLOCK).
3. מדידת צפיפות נתונים ואיכות כתבה (SIGNAL).
4. דירוג וסינון איכותי באמצעות Gemini 2.5 Flash API.
5. ניהול זיכרון פרקים קודמים (Episode Memory) לאזכורים טבעיים בתסריט.
6. איחוד כתבות כפולות (Deduplication) ושמירת יומן ריצה.
"""

import os
import sys
import json
import re
import tomli
import requests
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# טעינת מפתח API של Gemini מתופסן הסביבה
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def get_gemini_client():
    """מאתחל ומחזיר את לקוח Gemini API."""
    if not GEMINI_API_KEY:
        raise ValueError("שגיאה: GEMINI_API_KEY אינו מוגדר בסביבת העבודה.")
    return genai.Client(api_key=GEMINI_API_KEY)


# --- מודלי נתונים עבור Structured Outputs מול Gemini ---

class ArticleScore(BaseModel):
    score: float = Field(description="ציון האיכות של הכתבה מ-0.0 עד 10.0")
    reasoning: str = Field(description="הסבר קצר בעברית או אנגלית לציון שניתן")
    is_duplicate: bool = Field(description="האם הכתבה כפולה או מכסה נושא שכבר הופיע בכתבה קודמת")


class EpisodeMemory(BaseModel):
    summary_for_next_episodes: str = Field(description="סיכום קצר של נקודות המפתח והדעות שעלו בפרק עבור הזיכרון של הפרקים הבאים")


class DiscoveryEngine:
    def __init__(self, profile_path="profile.toml", memory_path="memory.json"):
        """טעינת הגדרות הפרופיל והזיכרון ההיסטורי."""
        with open(profile_path, "rb") as f:
            self.config = tomli.load(f)
        
        self.max_age_days = self.config["discovery"]["max_age_days"]
        self.max_articles = self.config["discovery"]["max_articles"]
        self.sources = self.config.get("sources", [])
        self.memory_path = memory_path
        self.memory_data = self.load_memory()

    def load_memory(self) -> list:
        """טעינת הזיכרון של הפרקים הקודמים מקובץ JSON."""
        if os.path.exists(self.memory_path):
            try:
                with open(self.memory_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"אזהרה: לא ניתן לקרוא את קובץ הזיכרון: {e}")
                return []
        return []

    def get_recent_memory_context(self, limit=3) -> str:
        """שליפת הזיכרון מ-N הפרקים האחרונים בפורמט טקסטואלי לפרומפט."""
        recent = self.memory_data[-limit:]
        if not recent:
            return "אין פרקים קודמים בזיכרון עדיין."
        
        context_str = "זיכרון מפרקים קודמים (להתייחסות ואזכורים במידת הרלוונטיות):\n"
        for ep in recent:
            context_str += f"- פרק מ- {ep.get('date', 'עבר')}: {ep.get('key_takeaways', '')}\n"
        return context_str

    def filter_blacklisted_titles(self, title: str) -> bool:
        """שלב BLOCK: סינון כותרות המכילות מילות מפתח לא רלוונטיות."""
        blacklist = [
            "webinar", "register now", "game jam", "hiring", "job moves",
            "sponsored", "event recap", "ticket", "discount code"
        ]
        title_lower = title.lower()
        for word in blacklist:
            if word in title_lower:
                return False
        return True

    def calculate_signal_density(self, text: str) -> float:
        """שלב SIGNAL: חישוב צפיפות נתונים (מספרים, אחוזים, סימני מטבע)."""
        if not text:
            return 0.0
        matches = re.findall(r'(\d+|%|\$|€|£|billion|million)', text, re.IGNORECASE)
        words = text.split()
        if not words:
            return 0.0
        return min(10.0, (len(matches) / len(words)) * 100)

    def score_article_with_gemini(self, title: str, summary: str, context_articles: list) -> ArticleScore:
        """שלב SCORE: פנייה ל-Gemini API לקבלת ציון התאמה וזיהוי כפילויות."""
        client = get_gemini_client()

        prompt = f"""
        אתה עורך ראשי של פודקאסט מובייל גיימינג מקצועי.
        תפקידך לדרג את החשיבות של הכתבה הבאה עבור מנהלים ויזמים בתעשייה.

        פרטי הכתבה:
        כותרת: {title}
        תקציר/תוכן: {summary}

        כתבות שכבר אושרו לפרק זה:
        {json.dumps(context_articles, ensure_ascii=False, indent=2)}

        הנחיות:
        1. דרג מ-0.0 עד 10.0 את חשיבות הכתבה (עדיפות לנושאי מונטיזציה, UA, רווחיות, Skill Gaming, ומהלכי מתחרים).
        2. סמן is_duplicate=true אם הכתבה מכסה את אותו הסיפור בדיוק שכבר מופיע בכתבות שאושרו.
        """

        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ArticleScore,
                    temperature=0.2,
                ),
            )
            data = json.loads(response.text)
            return ArticleScore(**data)
        except Exception as e:
            print(f"שגיאה בדירוג הכתבה מול Gemini: {e}")
            return ArticleScore(score=0.0, reasoning=f"Error: {e}", is_duplicate=False)

    def run_discovery(self, mock_items=None):
        """מריץ את תהליך האיסוף, הסינון והזיכרון המלא."""
        print(f"[{datetime.now().isoformat()}] מתחיל תהליך Discovery שבועי...")
        
        raw_items = mock_items if mock_items is not None else []
        approved_articles = []
        rejected_articles = []

        for item in raw_items:
            title = item.get("title", "")
            summary = item.get("summary", "")

            if not self.filter_blacklisted_titles(title):
                rejected_articles.append({"title": title, "reason": "Blocked by title filter"})
                continue

            density = self.calculate_signal_density(summary)
            score_result = self.score_article_with_gemini(title, summary, approved_articles)

            if score_result.is_duplicate:
                rejected_articles.append({"title": title, "reason": "Duplicate story"})
                continue

            if score_result.score >= 6.0:
                approved_articles.append({
                    "title": title,
                    "summary": summary,
                    "score": score_result.score,
                    "reasoning": score_result.reasoning,
                    "signal_density": density
                })
            else:
                rejected_articles.append({
                    "title": title,
                    "reason": f"Low score: {score_result.score} ({score_result.reasoning})"
                })

            if len(approved_articles) >= self.max_articles:
                break

        print(f"תהליך Discovery הסתיים: אושרו {len(approved_articles)} כתבות, נדחו {len(rejected_articles)}.")
        return approved_articles, rejected_articles


if __name__ == "__main__":
    engine = DiscoveryEngine()
    print("Memory context loaded:")
    print(engine.get_recent_memory_context())