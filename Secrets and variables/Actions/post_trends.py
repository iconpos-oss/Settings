"""
매일 아침 구글 실시간 트렌드(대한민국)를 가져와 Blogger에 자동으로 글을 발행하는 스크립트.

필요한 환경변수 (GitHub Actions Secrets로 주입):
  - BLOGGER_BLOG_ID      : 글을 올릴 블로그의 ID (숫자)
  - GOOGLE_CLIENT_ID     : OAuth 클라이언트 ID
  - GOOGLE_CLIENT_SECRET : OAuth 클라이언트 시크릿
  - GOOGLE_REFRESH_TOKEN : get_refresh_token.py로 최초 1회 발급받은 리프레시 토큰
  - DRY_RUN              : true/1이면 발행하지 않고 결과만 출력
  - FORCE_PUBLISH        : true/1이면 같은 날짜 글 중복 검사를 무시하고 강제 발행

동작 순서:
  1. 구글 트렌드 RSS(한국)에서 오늘의 실시간 인기 검색어를 가져온다.
     - 태국어/키릴/아랍어 등 비한국어 검색어와 해외 축구 스팸은 걸러낸다.
     - 키워드마다 <ht:news_item> 목록에서 키워드와 실제로 겹치는 기사를 골라 붙인다.
  2. 검색어 목록으로 글 제목/본문(HTML)을 생성한다.
  3. (발행 시) 같은 날짜(KST) 글이 이미 블로그에 있으면 중복 발행하지 않고 건너뛴다.
  4. Blogger API로 새 글을 발행한다.
"""

import os
import re
import sys
import html
import time
import argparse
import datetime
import requests
import xml.etree.ElementTree as ET

TRENDS_RSS_URLS = [
    # 구글 트렌드 새 RSS 엔드포인트 (200 OK 확인). 이 주소를 먼저 사용한다.
    "https://trends.google.com/trending/rss?geo=KR",
    # 아래 예전 주소들은 현재 404를 반환한다. 혹시 되살아날 경우를 위한 폴백으로만 남겨 둔다.
    "https://trends.google.com/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
    "https://trends.google.co.kr/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
    "https://www.google.com/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
]
MAX_ITEMS = 10

KST = datetime.timezone(datetime.timedelta(hours=9))

HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
# 한글 없이 영문/숫자/일반 기호로만 이루어진 검색어(예: KBO, F1, BTS) 판별용
LATIN_LIKE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&+\-/:()\[\]]*")
TOKEN_SPLIT_RE = re.compile(r"[\s\-–—·,\.\?!\|/\"'“”‘’]+")

DATE_TITLE_FORMAT = "%Y년 %m월 %d일"


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
        })
        if len(kept) >= limit:
            break

    return kept, rejected


def fetch_trends(limit=MAX_ITEMS):
    """구글 트렌드 RSS를 받아 (항목 리스트, 제외 목록)을 반환. 실패하면 ([], [])."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"}
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
        return parse_trends_xml(resp.content, limit=limit)
    except ET.ParseError as e:
        print(f"트렌드 RSS 응답을 해석하지 못했습니다: {e}")
        return [], []


# ---------------------------------------------------------------------------
# 글 작성
# ---------------------------------------------------------------------------
def build_post(items):
    # GitHub Actions 러너는 UTC라서 그대로 두면 아침 7시 발행 글이 전날 날짜로 찍힌다.
    # 발행 기준 날짜는 항상 한국 시간(KST)으로 계산한다.
    today = datetime.datetime.now(KST).date().strftime(DATE_TITLE_FORMAT)
    title = f"{today} 실시간 인기 검색어 TOP {len(items)}"

    rows = []
    for idx, item in enumerate(items, start=1):
        row = f"<h3>{idx}. {html.escape(item['keyword'])}</h3>"
        news_title = item.get("news_title") or ""
        news_url = item.get("news_url") or ""
        news_source = item.get("news_source") or ""
        if news_title:
            label = f"{news_source}: {news_title}" if news_source else news_title
            if news_url:
                row += f'<p>관련 뉴스: <a href="{html.escape(news_url, quote=True)}" target="_blank" rel="noopener">{html.escape(label)}</a></p>'
            else:
                row += f"<p>관련 뉴스: {html.escape(label)}</p>"
        rows.append(row)

    body = (
        f"<p>{today} 기준, 구글 트렌드에 오른 실시간 인기 검색어를 정리했습니다.</p>"
        + "".join(rows)
        + "<p><em>이 글은 매일 아침 자동으로 생성됩니다.</em></p>"
    )
    return title, body


def print_summary(items, rejected):
    print(f"키워드 {len(items)}개 / 제외 {len(rejected)}개")
    for idx, item in enumerate(items, start=1):
        news = "관련 기사 없음"
        if item.get("news_title"):
            news = f"{item.get('news_source')}: {item.get('news_title')}"
        print(f"{idx:2d}. {item['keyword']}  |  {news}")
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
def env_flag(name):
    value = os.environ.get(name)
    return bool(value) and value.strip().lower() in ("1", "true", "yes", "y", "on")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print post content and skip publishing")
    parser.add_argument("--force", action="store_true", help="같은 날짜 글이 이미 있어도 강제로 발행")
    args = parser.parse_args()

    blog_id = os.environ.get("BLOGGER_BLOG_ID")
    dry_run = args.dry_run or env_flag("DRY_RUN")
    force = args.force or env_flag("FORCE_PUBLISH")

    items, rejected = fetch_trends()
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
