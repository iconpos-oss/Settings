"""
매일 아침 구글 실시간 트렌드(대한민국)를 가져와 Blogger에 자동으로 글을 발행하는 스크립트.

필요한 환경변수 (GitHub Actions Secrets로 주입):
  - BLOGGER_BLOG_ID      : 글을 올릴 블로그의 ID (숫자)
  - GOOGLE_CLIENT_ID     : OAuth 클라이언트 ID
  - GOOGLE_CLIENT_SECRET : OAuth 클라이언트 시크릿
  - GOOGLE_REFRESH_TOKEN : get_refresh_token.py로 최초 1회 발급받은 리프레시 토큰
  - DRY_RUN              : true/1이면 발행하지 않고 결과만 출력
  - FORCE_PUBLISH        : true/1이면 같은 날짜 글 중복 검사를 무시하고 강제 발행
  - FETCH_DESCRIPTIONS   : 0/false면 기사 원문 메타 설명 수집을 생략(테스트·오프라인용)

동작 순서:
  1. 구글 트렌드 RSS(한국)에서 오늘의 실시간 인기 검색어를 가져온다.
     - 태국어/키릴/아랍어 등 비한국어 검색어와 해외 축구 스팸은 걸러낸다.
     - 키워드마다 <ht:news_item> 목록에서 키워드와 실제로 겹치는 기사를 골라 붙인다.
     - RSS의 <ht:news_item_snippet>이 비어 있으면 기사 원문의 메타 description을 읽어
       2~3문장 분량의 이슈 요약을 만든다(실패하면 제목 기반 안내 문구로 대체).
  2. 검색어 목록으로 글 제목/본문(HTML)을 생성한다.
     - 서론(날짜·오늘의 검색어 요약) → 한눈에 보는 목록 → 순위별 소제목·검색 관심도·
       이슈 요약·관련 기사 링크 → 마무리 문단 구조로 조립한다.
  3. (발행 시) 같은 날짜(KST) 글이 이미 블로그에 있으면 중복 발행하지 않고 건너뛴다.
  4. Blogger API로 새 글을 발행한다.

외부에서 온 값(검색어, 기사 제목, 요약, URL)은 모두 텍스트로만 취급한다.
  - HTML은 html.escape로 이스케이프한다.
  - 링크는 http/https URL만 남기고, 그 밖의 스킴(javascript: 등)은 버린다.
  - 기사 원문은 요약(메타 description)만 읽고 본문 전체를 옮기지 않는다.
"""

import os
import re
import sys
import html
import time
import argparse
import datetime
from urllib.parse import urlsplit

import requests
import xml.etree.ElementTree as ET

TRENDS_RSS_URLS = [
    # 구글 트렌드 새 RSS 엔드포인트 (200 OK 확인). 이 주소를 먼저 사용한다.
    "https://trends.google.com/trending/rss?geo=KR",
    # 아래 예전 주소들은 현재 404를 반환한다. 혹시 되살아날 경우를 위한 폴백으로만 남겨 둔다.
    "https://trends.google.co.kr/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
    "https://trends.google.com/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
    "https://www.google.com/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
]
MAX_ITEMS = 10

KST = datetime.timezone(datetime.timedelta(hours=9))

HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
# 한글 없이 영문/숫자/일반 기호로만 이루어진 검색어(예: KBO, F1, BTS) 판별용
LATIN_LIKE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&+\-/:()\[\]]*")
TOKEN_SPLIT_RE = re.compile(r"[\s\-–—·,\.\?!\|/\"'“”‘’]+")

DATE_TITLE_FORMAT = "%Y년 %m월 %d일"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/115.0 Safari/537.36")

# 기사 원문 메타 설명 수집 설정
DESCRIPTION_TIMEOUT = 6
DESCRIPTION_MAX_BYTES = 400_000
DESCRIPTION_BUDGET_SECONDS = 60
SUMMARY_MAX_CHARS = 320
SUMMARY_MAX_SENTENCES = 3

# 외부 텍스트 정리용 정규식
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TAG_RE = re.compile(r"<[^>]{0,400}>")
WS_RE = re.compile(r"\s+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？…])\s*")
# 기사 요약 끝에 붙는 ' - 언론사 | 뉴스 | 사진' 같은 사이트 꼬리표
SITE_SUFFIX_RE = re.compile(r"^(?P<body>.*[.!?。！？…\"”’'])\s*[-–—|·]\s*[^-–—|]{1,60}$")
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
META_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
VISIBLE_CHAR_RE = re.compile(r"[0-9A-Za-z가-힣]")
# '[서울=뉴시스]', '(엑스포츠뉴스 유희은 기자)', '홍길동 기자 =' 같은 머리말
DATELINE_RE = re.compile(
    r"^\s*(?:\[[^\]]{1,20}\]\s*)?(?:\([^)]{0,30}기자\)\s*)?"
    r"(?:[가-힣]{2,5}\s*기자\s*=\s*)?"
)
PHOTO_CREDIT_RE = re.compile(r"/\s*사진\s*=\s*[^\s\[/]{1,20}\s*")
SENTENCE_END_CHARS = ".!?。！？…\"”’'"


# ---------------------------------------------------------------------------
# RSS 파싱
# ---------------------------------------------------------------------------
def _local_name(tag):
    """'{namespace}tag' -> 'tag'"""
    return tag.split('}')[-1] if '}' in tag else tag


def _news_text(news):
    return " ".join([
        news.get("news_item_title") or "",
        news.get("news_item_source") or "",
        news.get("news_item_snippet") or "",
    ])


def _keyword_tokens(keyword):
    tokens = [t for t in TOKEN_SPLIT_RE.split(keyword.strip().lower()) if t]
    # 한 글자짜리 검색어(예: '집')도 매칭에 쓸 수 있도록 원문을 토큰으로 되돌려 준다.
    return tokens or [keyword.strip().lower()]


def score_news(keyword, news):
    """키워드와 기사(제목/출처/요약)가 얼마나 겹치는지 점수화한다."""
    text = _news_text(news).lower()
    kw = keyword.strip().lower()
    if not text or not kw:
        return 0

    score = 0
    if kw in text:
        score += 3
    for token in _keyword_tokens(keyword):
        if token in text:
            score += 2
        elif len(token) >= 2 and HANGUL_RE.search(token) and token[:2] in text:
            # '수능날' -> '수능' 처럼 복합어가 줄어든 경우를 부분 매칭으로 인정
            score += 1
    return score


def select_news(keyword, news_items):
    """<ht:news_item> 들 중 키워드와 가장 잘 맞는 기사 하나를 고른다. 없으면 None."""
    best, best_score = None, 0
    for news in news_items:
        s = score_news(keyword, news)
        if s > best_score:
            best, best_score = news, s
    return best


def is_korean_trend(keyword, news_items):
    """한국어 트렌드로 볼 수 있는 검색어인지 판단한다.

    - 한글이 들어 있으면 통과 (한국어 검색어)
    - 한글이 없고 태국어/키릴/아랍어/한자/가나 등 비라틴 문자면 제외
    - 한글 없는 영문·숫자 검색어(KBO, F1 등)는 국내 기사가 붙어 있을 때만 통과
    """
    kw = keyword.strip()
    if not kw:
        return False
    if HANGUL_RE.search(kw):
        return True
    if not LATIN_LIKE_RE.fullmatch(kw):
        return False
    return any(HANGUL_RE.search(_news_text(n)) for n in news_items)


def reject_reason(keyword, news_items):
    kw = keyword.strip()
    if not kw:
        return "빈 검색어"
    if HANGUL_RE.search(kw):
        return None
    if not LATIN_LIKE_RE.fullmatch(kw):
        return "비한국어 문자(태국어/키릴/아랍어 등)"
    if not any(HANGUL_RE.search(_news_text(n)) for n in news_items):
        return "해외 기사만 연결된 영문 검색어(해외 스포츠 스팸 등)"
    return None


def parse_trends_xml(xml_bytes, limit=MAX_ITEMS):
    """RSS 원문을 파싱해 (남길 항목 리스트, 제외한 (키워드, 사유) 리스트)를 돌려준다."""
    root = ET.fromstring(xml_bytes)

    kept, rejected = [], []
    for item in [el for el in root.iter() if _local_name(el.tag) == 'item']:
        keyword = ''
        traffic = ''
        news_items = []
        for child in item:
            name = _local_name(child.tag)
            if name == 'title' and child.text:
                keyword = child.text.strip()
            elif name == 'approx_traffic' and child.text:
                traffic = child.text.strip()
            elif name == 'news_item':
                news_items.append({
                    _local_name(n.tag): (n.text or '').strip() for n in child
                })
        if not keyword:
            continue

        reason = reject_reason(keyword, news_items)
        if reason:
            rejected.append((keyword, reason))
            continue

        best = select_news(keyword, news_items)
        kept.append({
            "keyword": keyword,
            "traffic": traffic,
            "news_title": best.get("news_item_title", "") if best else "",
            "news_url": best.get("news_item_url", "") if best else "",
            "news_source": best.get("news_item_source", "") if best else "",
            "news_snippet": best.get("news_item_snippet", "") if best else "",
        })
        if len(kept) >= limit:
            break

    return kept, rejected


def fetch_trends(limit=MAX_ITEMS, enrich=None):
    """구글 트렌드 RSS를 받아 (항목 리스트, 제외 목록)을 반환. 실패하면 ([], []).

    enrich가 None이면 환경변수 FETCH_DESCRIPTIONS(기본 켜짐)를 따른다.
    """
    headers = {"User-Agent": USER_AGENT}
    resp = None
    last_err = None
    for url in TRENDS_RSS_URLS:
        try:
            r = requests.get(url, timeout=15, headers=headers)
            r.raise_for_status()
            resp = r
            break
        except Exception as e:
            last_err = e
            continue

    if resp is None:
        print(f"트렌드 RSS를 가져오지 못했습니다: {last_err}")
        return [], []

    try:
        items, rejected = parse_trends_xml(resp.content, limit=limit)
    except ET.ParseError as e:
        print(f"트렌드 RSS 응답을 해석하지 못했습니다: {e}")
        return [], []

    if enrich is None:
        enrich = env_flag("FETCH_DESCRIPTIONS", default=True)
    if enrich:
        write_summaries(items)
    return items, rejected


# ---------------------------------------------------------------------------
# 외부 텍스트 정리·요약
# ---------------------------------------------------------------------------
def clean_text(raw, max_chars=0):
    """RSS/웹에서 온 텍스트를 화면에 그대로 보여도 되는 한 줄 텍스트로 정리한다.

    - HTML 엔티티를 풀고(&amp;nbsp;처럼 이중으로 이스케이프된 경우도 한 번 더 푼다)
    - 태그, 제어문자, 깨진 문자(U+FFFD), 줄바꿈/연속 공백을 없앤다
    - 남는 글자가 없으면 빈 문자열을 돌려준다(호출 쪽에서 안내 문구로 대체).
    """
    if raw is None:
        return ""
    text = html.unescape(str(raw))
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ").replace("\ufffd", " ")
    text = CONTROL_CHARS_RE.sub(" ", text)
    text = WS_RE.sub(" ", text).strip()
    text = text.strip("·|")
    if text and not VISIBLE_CHAR_RE.search(text):
        return ""
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def safe_url(url):
    """http/https URL만 통과시킨다. 그 밖의 스킴·자격증명 포함·IP 직접 지정은 버린다."""
    url = clean_text(url, max_chars=2048)
    if not url or len(url) > 2048:
        return ""
    if any(ord(ch) < 32 or ch == " " for ch in url):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not parts.netloc or "@" in parts.netloc:
        return ""
    host = (parts.hostname or "").strip()
    if not host or "." not in host:
        return ""
    if re.fullmatch(r"[0-9.]+", host):
        return ""
    return url


def _decode_html(content, content_type):
    charset = None
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    candidates = [c for c in (charset, "utf-8", "euc-kr", "cp949") if c]
    for cs in candidates:
        try:
            return content.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="ignore")


def extract_meta_description(doc):
    """HTML 문서의 og:description / twitter:description / description 메타 값을 꺼낸다."""
    if not doc:
        return ""
    metas = {}
    for tag in META_TAG_RE.findall(doc[:200000]):
        attrs = {}
        for m in META_ATTR_RE.finditer(tag):
            value = m.group(2) if m.group(2) is not None else (m.group(3) or "")
            attrs[m.group(1).lower()] = value
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = attrs.get("content") or ""
        if key and content and key not in metas:
            metas[key] = content

    for key in ("og:description", "twitter:description", "description"):
        text = clean_text(metas.get(key, ""), max_chars=600)
        text = dedupe_text(strip_site_suffix(text))
        if len(text) >= 20:
            return text
    return ""


def strip_site_suffix(text):
    """'본문… - 언론사 | 뉴스 | 사진' 처럼 끝에 붙은 사이트 꼬리표를 떼어 낸다."""
    if not text:
        return ""
    m = SITE_SUFFIX_RE.match(text)
    if m:
        body = m.group("body").strip()
        if len(body) >= 20:
            return body
    return text


def dedupe_text(text):
    """'…했다.,같은 문장이 한 번 더' 형태로 중복된 요약에서 앞부분만 남긴다."""
    if not text or ".," not in text:
        return text
    head, _, tail = text.partition(".,")
    head = head.strip()
    if head and head[:20] in tail:
        return head + "."
    return text


def fetch_meta_description(url):
    """기사 원문에서 메타 description만 읽어 온다. 실패하면 빈 문자열."""
    url = safe_url(url)
    if not url:
        return ""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
    try:
        with requests.get(url, timeout=DESCRIPTION_TIMEOUT, headers=headers, stream=True) as r:
            if r.status_code != 200:
                return ""
            content_type = r.headers.get("Content-Type", "")
            if content_type and "html" not in content_type.lower():
                return ""
            chunks, size = [], 0
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= DESCRIPTION_MAX_BYTES:
                    break
            doc = _decode_html(b"".join(chunks), content_type)
    except Exception as e:  # 네트워크·TLS·인코딩 등 어떤 실패도 글 작성은 계속한다.
        print(f"  기사 요약을 읽지 못했습니다({type(e).__name__}): {url[:80]}")
        return ""
    return extract_meta_description(doc)


def write_summaries(items):
    """각 항목의 이슈 요약을 채운다 (RSS 스니펫 우선, 없으면 기사 메타 description)."""
    deadline = time.time() + DESCRIPTION_BUDGET_SECONDS
    for item in items:
        snippet = clean_text(item.get("news_snippet") or "", max_chars=1200)
        item["summary"] = dedupe_text(strip_site_suffix(snippet))
        if item["summary"]:
            item["summary_source"] = "rss"
            continue
        if item.get("news_url") and time.time() < deadline:
            desc = fetch_meta_description(item["news_url"])
            if desc:
                item["summary"] = desc
                item["summary_source"] = "article"
                continue
        item["summary"] = ""
        item["summary_source"] = ""


def tidy_summary_text(text):
    """기사 메타 설명에서 잘린 꼬리·머리말·사진 표기 같은 자잘한 조각을 걷어 낸다."""
    if not text:
        return ""
    text = PHOTO_CREDIT_RE.sub(" ", text)
    text = DATELINE_RE.sub("", text)
    text = text.replace("…", " ")
    text = re.sub(r"\.{2,}", " ", text)
    text = re.sub(r"\s+([.,!?])", r"\1", text)
    text = WS_RE.sub(" ", text).strip(" ,;·|")
    if not text:
        return ""
    # '화제입니다 . .' 처럼 마침표만 남은 조각 정리
    text = re.sub(r"(?:\.\s*){2,}", ". ", text).strip()
    return text


def _split_sentences(text):
    """문장 단위로 나눈다. 마지막이 마침표 없이 잘린 조각이면 따로 돌려준다."""
    parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(text) if p.strip()]
    tail = ""
    if parts and parts[-1][-1] not in SENTENCE_END_CHARS:
        tail = parts.pop()
    return parts, tail


def _shorten_sentences(text, max_chars=SUMMARY_MAX_CHARS, max_sentences=SUMMARY_MAX_SENTENCES):
    """요약문을 2~3문장, SUMMARY_MAX_CHARS 안으로 줄인다."""
    parts, tail = _split_sentences(text)
    if not parts:
        # 문장이 하나도 온전하지 않으면(메타 설명이 통째로 잘린 경우) 발췌임을 밝히고 쓴다.
        if tail:
            return tail[:max_chars].rstrip(" ,·-–—") + "…"
        return ""
    out, length = [], 0
    for part in parts:
        if out and (len(out) >= max_sentences or length + len(part) > max_chars):
            break
        if not out and len(part) > max_chars:
            part = part[:max_chars].rstrip(" ,·-–—") + "…"
        out.append(part)
        length += len(part) + 1
    joined = " ".join(out).strip()
    if joined and joined[-1] not in SENTENCE_END_CHARS:
        joined += "."
    return joined


def build_summary(item):
    """한 검색어의 '관련 이슈 요약' 문단(2~3문장)을 만든다.

    기사 요약이 있으면 그 문장을 쓰고, 없으면 기사 제목·출처만으로 사실대로 안내한다.
    없는 내용을 지어내지 않는다.
    """
    keyword = clean_text(item.get("keyword") or "", max_chars=80)
    news_title = clean_text(item.get("news_title") or "", max_chars=200)
    news_source = clean_source(item.get("news_source") or "")
    url = safe_url(item.get("news_url") or "")
    summary = tidy_summary_text(clean_text(item.get("summary") or "", max_chars=1200))

    sentences = []
    body = _shorten_sentences(summary) if summary else ""
    if body:
        sentences.append(body)
        # 요약이 한 문장뿐이면 원문 안내 문장을 더해 2문장으로 만든다.
        # (이미 2문장 이상이면 같은 안내 문구를 반복하지 않는다)
        if url and len(_split_sentences(body)[0]) < 2:
            if news_source:
                sentences.append(f"자세한 내용은 아래 {news_source} 기사 원문에서 확인하실 수 있습니다.")
            else:
                sentences.append("자세한 내용은 아래 기사 원문에서 확인하실 수 있습니다.")
    elif news_title:
        lead = f"‘{keyword}’ 검색과 함께 "
        lead += f"{news_source}의 " if news_source else ""
        lead += f"「{news_title}」 기사가 함께 노출되었습니다."
        sentences.append(lead)
        sentences.append(
            "언론사가 공개한 요약이 없어 제목으로만 정리했으며, 자세한 내용은 아래 기사 원문에서 확인하실 수 있습니다."
            if url else
            "언론사가 공개한 요약과 원문 링크가 없어 제목으로만 정리했습니다."
        )
    else:
        sentences.append(f"구글 트렌드에서 ‘{keyword}’가 오늘의 실시간 인기 검색어로 올랐습니다.")
        sentences.append(
            f"아직 구글 트렌드가 연결한 관련 기사가 없어, 자세한 배경은 포털 뉴스에서 ‘{keyword}’를 검색해 확인하실 수 있습니다."
        )
    return " ".join(sentences)


# ---------------------------------------------------------------------------
# 글 작성
# ---------------------------------------------------------------------------
def clean_source(source):
    """언론사 이름 표기 정리: 도메인으로만 온 경우 앞의 'www.'를 떼어 낸다."""
    source = clean_text(source, max_chars=60)
    if source and not HANGUL_RE.search(source):
        source = re.sub(r"^www\.", "", source, flags=re.I)
    return source


def _traffic_text(item):
    traffic = clean_text(item.get("traffic") or "", max_chars=40)
    if not traffic:
        return "구글 트렌드 표시값 없음"
    return f"{traffic} (구글 트렌드 집계 기준)"


def build_item_html(index, item):
    """순위 한 건: 소제목 + 검색 관심도 + 이슈 요약 + 관련 기사 링크."""
    keyword = clean_text(item.get("keyword") or "", max_chars=100)
    news_title = clean_text(item.get("news_title") or "", max_chars=200)
    news_source = clean_source(item.get("news_source") or "")
    url = safe_url(item.get("news_url") or "")

    lines = [
        f"<h3>{index}. {html.escape(keyword)}</h3>",
        f"<p><strong>검색 관심도</strong>: {html.escape(_traffic_text(item))}</p>",
        f"<p>{html.escape(build_summary(item))}</p>",
    ]

    if news_title:
        label = f"{news_title} — {news_source}" if news_source else news_title
        if url:
            lines.append(
                '<p>관련 기사: <a href="' + html.escape(url, quote=True) +
                '" target="_blank" rel="noopener noreferrer">' + html.escape(label) + "</a></p>"
            )
        else:
            lines.append(f"<p>관련 기사: {html.escape(label)} (원문 링크 제공 없음)</p>")
    else:
        lines.append(
            "<p>관련 기사: 구글 트렌드가 연결한 기사 없음</p>"
        )
    return "\n".join(lines)


def build_post(items):
    """글 제목과 본문 HTML을 만든다.

    구성: 서론(날짜·오늘의 검색어 요약) → 한눈에 보는 목록 → 순위별 상세 → 마무리.
    문단과 소제목이 분명히 나뉘도록 모든 블록을 개별 <p>/<h3>로 조립한다.
    """
    # GitHub Actions 러너는 UTC라서 그대로 두면 아침 7시 발행 글이 전날 날짜로 찍힌다.
    # 발행 기준 날짜는 항상 한국 시간(KST)으로 계산한다.
    today = datetime.datetime.now(KST).date().strftime(DATE_TITLE_FORMAT)
    title = f"{today} 실시간 인기 검색어 TOP {len(items)}"

    keywords = [clean_text(item.get("keyword") or "", max_chars=60) for item in items]
    top_keywords = [k for k in keywords[:3] if k]

    intro = [f"{today} 기준, 구글 트렌드(대한민국)에 오른 실시간 인기 검색어를 정리했습니다."]
    if top_keywords:
        intro[0] += f" 오늘은 {', '.join(html.escape(k) for k in top_keywords)} 등 " \
                    f"{len(items)}개의 검색어가 눈에 띄었습니다."
    intro.append(
        "순위별로 검색 관심도와 관련 기사 요약, 원문 링크를 함께 담았습니다. "
        "검색 관심도는 구글 트렌드가 '100+'처럼 표시한 대략적인 검색량이고, "
        "기사 요약은 각 언론사가 공개한 설명을 인용해 정리한 것입니다."
    )

    list_lines = ["<h2>오늘의 인기 검색어 목록</h2>", "<ul>"]
    for idx, item in enumerate(items, start=1):
        keyword = keywords[idx - 1]
        traffic = clean_text(item.get("traffic") or "", max_chars=40)
        line = f"<li>{idx}. {html.escape(keyword)}"
        if traffic:
            line += f" — 관심도 {html.escape(traffic)}"
        list_lines.append(line + "</li>")
    list_lines.append("</ul>")

    detail_lines = ["<h2>순위별 검색어와 관련 소식</h2>"]
    for idx, item in enumerate(items, start=1):
        detail_lines.append(build_item_html(idx, item))

    closing = [
        f"<p>오늘의 실시간 인기 검색어 {len(items)}개를 순위별로 정리했습니다. "
        "인기 검색어 순위는 하루 안에도 계속 바뀌므로, 이후 흐름이 궁금하시면 구글 트렌드에서 바로 확인하실 수 있습니다.</p>",
        "<p><em>이 글은 구글 트렌드 RSS를 바탕으로 매일 아침 자동으로 작성됩니다. "
        "인용한 기사 내용과 사진의 저작권은 각 언론사에 있으며, 원문은 위 링크에서 확인하실 수 있습니다.</em></p>",
    ]

    blocks = [f"<p>{html.escape(intro[0])}</p>"]
    blocks.append(f"<p>{html.escape(intro[1])}</p>")
    blocks.append("<hr />")
    blocks.extend(list_lines)
    blocks.append("<hr />")
    blocks.extend(detail_lines)
    blocks.append("<hr />")
    blocks.extend(closing)
    return title, "\n".join(blocks)


def print_summary(items, rejected):
    print(f"키워드 {len(items)}개 / 제외 {len(rejected)}개")
    for idx, item in enumerate(items, start=1):
        news = "관련 기사 없음"
        if item.get("news_title"):
            news = f"{item.get('news_source')}: {item.get('news_title')}"
        source = item.get("summary_source") or "-"
        print(f"{idx:2d}. {item['keyword']}  |  {news}  |  요약:{source}")
    for keyword, reason in rejected:
        print(f"  - 제외: {keyword!r} ({reason})")


# ---------------------------------------------------------------------------
# 중복 발행 방지
# ---------------------------------------------------------------------------
def get_blogger_service():
    # dry-run/파싱 테스트만 할 때는 구글 라이브러리가 없어도 되도록 지연 import
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/blogger"],
    )
    return build("blogger", "v3", credentials=creds)


def has_credentials():
    return all(os.environ.get(k) for k in
               ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"))


def find_existing_post(service, blog_id, date_label, max_checked=100):
    """블로그 최근 글을 훑어 제목에 date_label(예: '2026년 11월 16일')이 들어간 글을 찾는다.

    블로그 표준 시간대가 미국 서부로 설정되어 있어 날짜 필터(startDate/endDate)는
    하루가 밀릴 수 있으므로, 제목의 날짜 문자열로 비교한다.
    """
    want = date_label.replace(" ", "")
    # '11월 1일' 제목이 '11월 16일'에 걸리지 않도록 날짜 뒤에 숫자가 오지 않는 경우만 인정한다.
    pattern = re.compile(r"^" + re.escape(want) + r"(?!\d)")
    token = None
    checked = 0
    while checked < max_checked:
        kwargs = {"blogId": blog_id, "status": "LIVE", "maxResults": 50, "fetchBodies": False}
        if token:
            kwargs["pageToken"] = token
        resp = service.posts().list(**kwargs).execute()
        for post in resp.get("items", []):
            checked += 1
            title = (post.get("title") or "").replace(" ", "").lstrip("\ufeff")
            if pattern.match(title):
                return post
        token = resp.get("nextPageToken")
        if not token:
            break
    print(f"최근 글 {checked}개를 확인했습니다. 오늘({date_label}) 글은 없습니다.")
    return None


def find_existing_post_with_retry(service, blog_id, date_label, attempts=3):
    last_err = None
    for i in range(attempts):
        try:
            return find_existing_post(service, blog_id, date_label)
        except Exception as e:  # HttpError, 네트워크 오류 등
            last_err = e
            print(f"중복 검사 실패({i + 1}/{attempts}): {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    raise last_err


# ---------------------------------------------------------------------------
def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print post content and skip publishing")
    parser.add_argument("--force", action="store_true", help="같은 날짜 글이 이미 있어도 강제로 발행")
    parser.add_argument("--no-fetch", action="store_true",
                        help="기사 원문 요약 수집을 생략(오프라인 테스트용)")
    args = parser.parse_args()

    blog_id = os.environ.get("BLOGGER_BLOG_ID")
    dry_run = args.dry_run or env_flag("DRY_RUN")
    force = args.force or env_flag("FORCE_PUBLISH")
    enrich = False if args.no_fetch else None

    items, rejected = fetch_trends(enrich=enrich)
    if not items:
        print("트렌드를 가져오지 못했습니다. 종료합니다.", file=sys.stderr)
        sys.exit(1)

    title, body = build_post(items)
    date_label = datetime.datetime.now(KST).date().strftime(DATE_TITLE_FORMAT)

    print_summary(items, rejected)

    # dry-run: 출력만 하고 발행하지 않음
    if dry_run:
        print("--- DRY RUN: 게시를 수행하지 않습니다 ---")
        print(f"제목: {title}")
        print("본문:\n" + body)
        if blog_id and has_credentials():
            try:
                service = get_blogger_service()
                existing = find_existing_post_with_retry(service, blog_id, date_label)
                if existing:
                    print(f"중복 방지 점검: 오늘({date_label}) 글이 이미 있습니다 → 실제 발행 시 건너뜁니다. "
                          f"[{existing.get('title')}] {existing.get('url')}")
                else:
                    print(f"중복 방지 점검: 오늘({date_label}) 글이 없습니다 → 실제 발행 시 새로 발행합니다.")
            except Exception as e:
                print(f"중복 방지 점검을 하지 못했습니다: {e}", file=sys.stderr)
        else:
            print("중복 방지 점검: Blogger 자격 증명이 없어 건너뜁니다.")
        return

    if not blog_id:
        print("환경변수 BLOGGER_BLOG_ID가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    service = get_blogger_service()

    # 같은 날(KST) 글이 이미 있으면 중복 발행하지 않는다.
    # 검사 자체가 실패하면 중복 위험을 피하기 위해 발행하지 않고 실패로 끝낸다.
    # (일시적 오류였다면 force=true로 수동 실행)
    try:
        existing = find_existing_post_with_retry(service, blog_id, date_label)
    except Exception as e:
        print(f"중복 발행 검사를 완료하지 못해 발행을 중단합니다: {e}", file=sys.stderr)
        print("실제로 오늘 글이 없는 것이 확실하다면 workflow_dispatch에서 force=true로 실행하세요.", file=sys.stderr)
        sys.exit(1)

    if existing and not force:
        print(f"오늘({date_label}) 글이 이미 게시되어 있어 중복 발행을 건너뜁니다.")
        print(f"기존 글: {existing.get('title')} / {existing.get('url')}")
        return

    if existing and force:
        print(f"force 지정: 오늘({date_label}) 글이 이미 있지만 강제로 발행합니다. "
              f"[{existing.get('title')}] {existing.get('url')}")

    post_body = {"title": title, "content": body}
    result = service.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()

    print(f"발행 완료: {result.get('url')}")


if __name__ == "__main__":
    main()
