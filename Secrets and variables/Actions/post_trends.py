"""
매일 아침 구글 실시간 트렌드(대한민국)를 가져와 Blogger에 자동으로 글을 발행하는 스크립트.

필요한 환경변수 (GitHub Actions Secrets로 주입):
  - BLOGGER_BLOG_ID      : 글을 올릴 블로그의 ID (숫자)
  - GOOGLE_CLIENT_ID     : OAuth 클라이언트 ID
  - GOOGLE_CLIENT_SECRET : OAuth 클라이언트 시크릿
  - GOOGLE_REFRESH_TOKEN : get_refresh_token.py로 최초 1회 발급받은 리프레시 토큰

동작 순서:
  1. 구글 트렌드 RSS(한국)에서 오늘의 실시간 인기 검색어를 가져온다.
  2. 검색어 목록으로 글 제목/본문(HTML)을 생성한다.
  3. Blogger API로 새 글을 발행한다.
"""

import os
import sys
import argparse
import datetime
import requests
import xml.etree.ElementTree as ET

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TRENDS_RSS_URLS = [
    # 구글 트렌드 새 RSS 엔드포인트 (200 OK 확인). 이 주소를 먼저 사용한다.
    "https://trends.google.com/trending/rss?geo=KR",
    # 아래 예전 주소들은 현재 404를 반환한다. 혹시 되살아날 경우를 위한 폴백으로만 남겨 둔다.
    "https://trends.google.com/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
    "https://trends.google.co.kr/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
    "https://www.google.com/trends/trendingsearches/daily/rss?geo=KR&hl=ko",
]
MAX_ITEMS = 10


def fetch_trends():
    """구글 트렌드 RSS를 파싱해서 [(키워드, 관련기사제목, 관련기사링크), ...] 리스트 반환"""
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
        return []

    root = ET.fromstring(resp.content)

    def local_name(tag):
        return tag.split('}')[-1] if '}' in tag else tag

    items = []
    # 네임스페이스 유무와 상관없이 item 요소를 모두 찾는다.
    item_elems = [el for el in root.iter() if local_name(el.tag) == 'item']
    for item in item_elems[:MAX_ITEMS]:
        title = ''
        news_title = ''
        news_url = ''
        for child in item:
            name = local_name(child.tag)
            if name == 'title' and child.text:
                title = child.text.strip()
            elif name == 'news_item':
                # news_item 내부에서 제목/링크를 찾음
                for nchild in child:
                    nname = local_name(nchild.tag)
                    if nname == 'news_item_title' and nchild.text:
                        news_title = nchild.text.strip()
                    if nname == 'news_item_url' and nchild.text:
                        news_url = nchild.text.strip()
        if not title:
            continue
        items.append((title, news_title, news_url))
    return items


def build_post(items):
    today = datetime.date.today().strftime("%Y년 %m월 %d일")
    title = f"{today} 실시간 인기 검색어 TOP {len(items)}"

    rows = []
    for idx, (keyword, news_title, news_url) in enumerate(items, start=1):
        row = f"<h3>{idx}. {keyword}</h3>"
        if news_title:
            if news_url:
                row += f'<p>관련 뉴스: <a href="{news_url}" target="_blank" rel="noopener">{news_title}</a></p>'
            else:
                row += f"<p>관련 뉴스: {news_title}</p>"
        rows.append(row)

    body = (
        f"<p>{today} 기준, 구글 트렌드에 오른 실시간 인기 검색어를 정리했습니다.</p>"
        + "".join(rows)
        + "<p><em>이 글은 매일 아침 자동으로 생성됩니다.</em></p>"
    )
    return title, body


def get_blogger_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/blogger"],
    )
    return build("blogger", "v3", credentials=creds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print post content and skip publishing")
    args = parser.parse_args()

    blog_id = os.environ.get("BLOGGER_BLOG_ID")

    items = fetch_trends()
    if not items:
        print("트렌드를 가져오지 못했습니다. 종료합니다.", file=sys.stderr)
        sys.exit(1)

    title, body = build_post(items)

    # dry-run: 출력만 하고 발행하지 않음
    dry_run_env = os.environ.get("DRY_RUN")
    dry_run = args.dry_run or (dry_run_env and dry_run_env.lower() in ("1", "true", "yes"))

    if dry_run:
        print("--- DRY RUN: 게시를 수행하지 않습니다 ---")
        print(f"제목: {title}")
        print("본문(일부):\n", body[:1000])
        return

    if not blog_id:
        print("환경변수 BLOGGER_BLOG_ID가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    service = get_blogger_service()
    post_body = {"title": title, "content": body}
    result = service.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()

    print(f"발행 완료: {result.get('url')}")


if __name__ == "__main__":
    main()
