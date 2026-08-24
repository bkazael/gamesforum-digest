import os
import json
from discovery import DiscoveryEngine
# ייבוא המנועים הקיימים בפרויקט שלך (לפי שמות הקבצים אצלך):
# from script_generator import ScriptGenerator
# from tts_engine import TTSEngine

def run_isolated_test():
    print("=== מתחיל הרצת ניסיון מבודדת (Local Test) ===")
    
    # 1. טעינת Discovery עם קובץ הקונפיגורציה של הטסט
    engine = DiscoveryEngine(profile_path="profile_test.toml", memory_path="memory.json")
    
    # 2. הרצת איסוף וסינון (Gemini יבדוק כפילויות מול memory.json)
    # הערה: העבר את הפידים/mock_items של הרגע
    approved, rejected = engine.run_discovery()
    
    print(f"\n[Discovery Completed] אושרו {len(approved)} כתבות חדשות (לאחר סינון כפילויות מול פרק הבוקר).")
    
    if not approved:
        print("לא נמצאו כתבות חדשות שלא סוקרו בפרק הבוקר. הטסט הסתיים.")
        return

    # 3. יצירת תיקיית פלט מקומית לבדיקה (אינה נחשפת ל-RSS)
    output_dir = "./test_output"
    os.makedirs(output_dir, exist_ok=True)
    
    # שמירת תוצאות ה-Discovery לביקורת
    with open(f"{output_dir}/test_articles.json", "w", encoding="utf-8") as f:
        json.dump(approved, f, ensure_ascii=False, indent=2)
        
    print(f"\nהתוצאות נשמרו בתיקייה: {output_dir}")
    print("כעת ניתן להזרים את הכתבות למנוע התסריט וה-TTS של הפרויקט שלך ולשמור את קובץ ה-MP3 בתיקייה זו.")

if __name__ == "__main__":
    run_isolated_test()