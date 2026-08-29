"""Result quality verification — auto-detect and handle Perplexity's known issues.

Based on community-reported problems (297+ Reddit posts):
- 37% of cited URLs are broken/wrong
- Answers may contain fabricated claims
- Self-correction only triggers when challenged
"""

import logging
import re
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# Patterns that indicate potential fabrication
_WEAK_INDICATORS = [
    "i could not find",
    "i'm not sure",
    "i don't have",
    "note:",
    "disclaimer:",
    "as an ai",
    "i apologize",
    "may not be accurate",
    "please verify",
    "无法找到",
    "我不确定",
    "请注意",
    "可能不准确",
]


def verify_sources(sources: List[Dict], timeout: float = 5.0, max_workers: int = 5) -> Dict:
    """Check if cited URLs actually exist (HTTP HEAD).

    Returns:
        {total, valid, broken, broken_urls: [{text, href, status}]}
    """
    if not sources:
        return {"total": 0, "valid": 0, "broken": 0, "broken_urls": []}

    def check_url(src):
        url = src.get("href", "")
        if not url or not url.startswith("http"):
            return src, 0
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return src, resp.status
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
            status = getattr(e, "code", 0)
            return src, status

    broken = []
    valid = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(check_url, s): s for s in sources}
        for f in as_completed(futures):
            src, status = f.result()
            if 200 <= status < 400:
                valid += 1
            else:
                broken.append({
                    "text": src.get("text", "")[:80],
                    "href": src.get("href", ""),
                    "status": status,
                })

    result = {
        "total": len(sources),
        "valid": valid,
        "broken": len(broken),
        "broken_urls": broken,
    }
    if broken:
        logger.warning("Broken sources: %d/%d URLs unreachable", len(broken), len(sources))
        for b in broken:
            logger.warning("  ✗ %s [%d] %s", b["text"][:50], b["status"], b["href"])
    else:
        logger.info("All %d sources verified", valid)
    return result


def check_answer_quality(answer: str, sources: List[Dict]) -> Dict:
    """Analyze answer quality and flag potential issues.

    Returns:
        {score: 0-100, issues: [str], needs_verification: bool}
    """
    issues = []
    score = 100

    # Empty or very short
    if not answer:
        return {"score": 0, "issues": ["Empty answer"], "needs_verification": True}
    if len(answer) < 100:
        issues.append("Very short answer (< 100 chars)")
        score -= 30

    # No sources
    if not sources:
        issues.append("No sources cited")
        score -= 25

    # Weak/hedging language
    answer_lower = answer.lower()
    weak_count = sum(1 for p in _WEAK_INDICATORS if p in answer_lower)
    if weak_count >= 3:
        issues.append(f"Multiple hedging phrases ({weak_count})")
        score -= 20

    # Suspiciously specific claims without numbers
    # (fabricated answers often have very specific but wrong details)
    if re.search(r'\d{4}', answer) and not re.search(r'\[\d+\]', answer):
        issues.append("Year references without citation markers")
        score -= 10

    # Answer is mostly questions (not actually answering)
    question_count = answer.count('?') + answer.count('？')
    if question_count > 3 and len(answer) < 500:
        issues.append("Answer contains many questions — may not be answering directly")
        score -= 15

    score = max(0, score)
    needs_verification = score < 70

    if issues:
        logger.warning("Answer quality issues (score=%d): %s", score, "; ".join(issues))
    else:
        logger.info("Answer quality: %d/100 — clean", score)

    return {"score": score, "issues": issues, "needs_verification": needs_verification}


def generate_verification_prompt(answer: str, query: str) -> str:
    """Generate a follow-up prompt to trigger Perplexity's self-correction.

    Community finding: Perplexity only self-corrects when explicitly challenged.
    """
    return (
        f"请验证你上面关于「{query[:50]}」的回答。"
        f"特别检查：1) 所有引用链接是否真实可访问且内容相关；"
        f"2) 数据和日期是否准确；3) 是否有编造或推测的内容。"
        f"如果有错误，请给出正确答案。"
    )


def generate_cross_check_queries(query: str, answer: str) -> List[str]:
    """Generate alternative queries to cross-validate the answer.

    Returns 2 queries phrased differently to catch inconsistencies.
    """
    # Extract key claims from answer (sentences with numbers or specific names)
    claims = re.findall(r'[A-Z][^.]*?\d+[^.]*\.', answer)
    if not claims:
        claims = [answer[:100]]

    queries = []
    # Rephrase from different angle
    queries.append(f"{query} — 验证准确性，请引用可靠来源")
    # Ask for counter-evidence
    queries.append(f"{query} 常见误解和错误是什么？")

    return queries[:2]


def verify_result(result: dict, verify_urls: bool = True) -> dict:
    """Run all quality checks on a search result and annotate it.

    Adds a 'quality' field to the result dict with verification results.
    """
    answer = result.get("answer", "")
    sources = result.get("sources", [])

    quality = {
        "answer_check": check_answer_quality(answer, sources),
    }

    if verify_urls and sources:
        quality["source_check"] = verify_sources(sources)
    else:
        quality["source_check"] = {"total": 0, "valid": 0, "broken": 0, "broken_urls": []}

    # Overall quality verdict
    answer_score = quality["answer_check"]["score"]
    broken_ratio = 0
    if quality["source_check"]["total"] > 0:
        broken_ratio = quality["source_check"]["broken"] / quality["source_check"]["total"]

    if answer_score < 50 or broken_ratio > 0.5:
        quality["verdict"] = "poor"
        quality["suggestion"] = "Answer likely unreliable. Consider re-searching or cross-checking."
    elif answer_score < 70 or broken_ratio > 0.3:
        quality["verdict"] = "questionable"
        quality["suggestion"] = "Some issues detected. Verify key claims before using."
    else:
        quality["verdict"] = "good"
        quality["suggestion"] = "Answer appears reliable."

    logger.info("Quality verdict: %s (answer=%d, broken_sources=%d/%d)",
                quality["verdict"], answer_score,
                quality["source_check"]["broken"], quality["source_check"]["total"])

    result["quality"] = quality
    return result
