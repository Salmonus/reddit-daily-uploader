import os
import time
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse
from apify_client import ApifyClient
import boto3

# ===== 환경변수 =====
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
SEARCH_KEYWORDS = [k.strip() for k in os.environ.get("SEARCH_KEYWORD", "AI").split(",") if k.strip()]
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50"))
LIMIT_UPLOAD = int(os.environ.get("LIMIT_UPLOAD", "20"))
S3_BUCKET = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
GRAPHQL_ENDPOINT = os.environ["GRAPHQL_ENDPOINT"]
API_KEY = os.environ["API_KEY"]

s3 = boto3.client("s3", region_name=AWS_REGION)


def is_image_url(url: str) -> bool:
    """이미지 URL인지 판단"""
    if not isinstance(url, str):
        return False
    url_lower = url.lower()
    if "pbs.twimg.com" not in url_lower and "twimg.com" not in url_lower:
        return False
    # 비디오 썸네일 제외 (가능하면)
    if "video_thumb" in url_lower or "amplify_video_thumb" in url_lower:
        return False
    # 확장자나 name= 파라미터가 있으면 더 확실
    if any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
        return True
    if "name=" in url_lower or "/media/" in url_lower:
        return True
    return True  # pbs.twimg.com이면 일단 통과


def find_image_urls(obj, found=None) -> list[str]:
    """
    최대한 강력하게 이미지 URL을 찾는 함수
    - 재귀적으로 전체 객체를 탐색
    - 알려진 필드명들을 우선 체크
    """
    if found is None:
        found = []

    if isinstance(obj, dict):
        # 1. 직접적인 이미지 필드들 우선 체크
        for key in [
            "media_url_https", "media_url", "url", "mediaUrl", "image", "photo",
            "src", "href", "expanded_url", "display_url", "coverImage",
            "profile_image_url_https", "profile_banner_url"
        ]:
            val = obj.get(key)
            if is_image_url(val):
                found.append(val)

        # 2. media / photos / images 배열 처리
        for key in ["media", "photos", "images", "extended_entities", "entities"]:
            val = obj.get(key)
            if isinstance(val, list):
                for item in val:
                    find_image_urls(item, found)
            elif isinstance(val, dict):
                find_image_urls(val, found)

        # 3. 나머지 모든 값 재귀 탐색
        for key, val in obj.items():
            # 이미 위에서 처리한 키는 스킵
            if key in ["media_url_https", "media_url", "url", "media", "photos", "images"]:
                continue
            find_image_urls(val, found)

    elif isinstance(obj, list):
        for item in obj:
            find_image_urls(item, found)

    elif isinstance(obj, str):
        if is_image_url(obj):
            found.append(obj)

    # 중복 제거 + 고화질 변환 + 정렬 (실제 사진 우선)
    cleaned = []
    seen = set()
    for u in found:
        u = u.replace("&amp;", "&").strip()
        if not u or u in seen:
            continue
        seen.add(u)

        # 고화질로 변환 시도
        if "name=" not in u and "pbs.twimg.com/media/" in u:
            u = u + ("&name=large" if "?" in u else "?name=large")

        cleaned.append(u)

    # video_thumb이 아닌 것을 앞으로
    cleaned.sort(key=lambda x: ("video_thumb" in x.lower(), x))
    return cleaned


def upload_to_s3(image_bytes: bytes, key: str, content_type: str = "image/jpeg"):
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=image_bytes, ContentType=content_type)
    return key


def create_post(title, content, image_path, source_url=None, tweet_id=None):
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        id
        title
        imagePath
        redditId
        status
        fetchedAt
      }
    }
    """
    variables = {
        "input": {
            "title": (title or "Untitled")[:200],
            "content": content or "",
            "imagePath": image_path,
            "sourceUrl": source_url,
            "redditId": tweet_id,
            "status": "pending",
            "fetchedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    }
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    resp = requests.post(GRAPHQL_ENDPOINT, json={"query": mutation, "variables": variables}, headers=headers, timeout=30)
    result = resp.json()
    print("GraphQL status:", resp.status_code)
    print("GraphQL 응답:", result)
    return result


def already_exists(tweet_id: str) -> bool:
    query = """
    query ListPosts {
      listPosts(limit: 500) {
        items {
          redditId
        }
      }
    }
    """
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    try:
        resp = requests.post(GRAPHQL_ENDPOINT, json={"query": query}, headers=headers, timeout=20)
        result = resp.json()
        items = result.get("data", {}).get("listPosts", {}).get("items", []) or []
        return any(item.get("redditId") == tweet_id for item in items)
    except Exception as e:
        print(f"already_exists 체크 실패: {e}")
        return False


def main():
    print(f"===== X 스크래핑 시작 ({datetime.now(timezone.utc)}) =====")
    print(f"키워드: {SEARCH_KEYWORDS} / 저장 목표: {LIMIT_UPLOAD}개")

    client = ApifyClient(APIFY_TOKEN)

    run_input = {
        "searchTerms": SEARCH_KEYWORDS,
        "sort": "Top",
        "maxItems": MAX_ITEMS,
    }

    print("[Apify] Top 포스트 수집 중...")
    run = client.actor("apidojo/tweet-scraper").call(run_input=run_input)

    dataset_id = (
        getattr(run, "default_dataset_id", None)
        or getattr(run, "defaultDatasetId", None)
    )

    if not dataset_id:
        print("데이터셋 ID 없음")
        print("run 객체:", run)
        return

    items = list(client.dataset(dataset_id).iterate_items())
    print(f"수집된 포스트: {len(items)}개")

    items.sort(key=lambda x: x.get("likeCount", 0) or 0, reverse=True)

    success_count = 0
    no_image_count = 0

    for item in items:
        if success_count >= LIMIT_UPLOAD:
            break

        tweet_id = str(item.get("id") or item.get("tweetId") or "")
        if not tweet_id:
            continue

        if already_exists(tweet_id):
            print(f"[스킵] 이미 존재: {tweet_id}")
            continue

        image_urls = find_image_urls(item)

        if not image_urls:
            no_image_count += 1
            print(f"[스킵] 이미지 없음: {tweet_id}")
            continue

        # 원작자 username 추출
        author = item.get("author") or {}
        username = (
            author.get("userName")
            or author.get("username")
            or author.get("screen_name")
            or author.get("screenName")
            or None
        )

        valid_image_url = image_urls[0]
        text = item.get("text") or item.get("full_text") or ""
        source_url = item.get("url") or item.get("twitterUrl") or f"https://x.com/i/status/{tweet_id}"

        if username:
            title = f"@{username}"
        else:
            title = text[:200] if text else f"X post {tweet_id}"

        try:
            response = requests.get(valid_image_url, timeout=25, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            if response.status_code != 200:
                print(f"이미지 다운로드 실패 ({response.status_code}): {valid_image_url[:80]}")
                continue

            path = urlparse(valid_image_url).path
            ext = os.path.splitext(path)[1].lower()
            if ext not in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                ext = ".jpg"

            s3_key = f"images/x_{tweet_id}{ext}"
            content_type = response.headers.get("Content-Type", "image/jpeg")

            upload_to_s3(response.content, s3_key, content_type)
            print(f"S3 업로드 완료: {s3_key} (title: {title}) | 이미지 후보 {len(image_urls)}개")

            result = create_post(title, text, s3_key, source_url, tweet_id)

            post_id_created = result.get("data", {}).get("createPost", {}).get("id")
            if post_id_created:
                success_count += 1
                print(f"  [{success_count}] DB 저장 성공: {title}")
            else:
                print(f"  DB 저장 실패: {result}")

            time.sleep(0.4)

        except Exception as e:
            print(f"  에러 {tweet_id}: {e}")

    print(f"\n===== 스크래핑 완료! 총 {success_count}개 DB 저장 / 이미지 없음 {no_image_count}개 =====")


if __name__ == "__main__":
    main()
