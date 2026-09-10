"""Retired keyword classifier. Legacy MBUSTER knowledge has been removed.
Business classification is now generated only by validated Gemini analysis.
"""

def classify_and_score(title: str, content: str = "") -> dict:
    return {"등급": "분석 대기", "카테고리": "판단 어려움",
            "추천솔루션": "분석 대기", "매칭키워드": "-"}
