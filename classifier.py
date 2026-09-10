"""Compatibility only. Keyword matches must not masquerade as AI analysis."""
from biz_classifier import classify_and_score

def classify_post_type(title: str) -> str:
    return "UNKNOWN"
