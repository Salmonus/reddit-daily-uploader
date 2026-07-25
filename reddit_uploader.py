import os
import time
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse
from apify_client import ApifyClient
import boto3

# ===== 환경변수 =====
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
# 여러 서브레딧 (쉼표로 구분)
SUBREDDITS = [s.strip() for s in os.environ.get("SUBREDDITS", "wallpapers").split(",") if s.strip()]
LIMIT_PER_SUB = int(os.environ.get("LIMIT_PER_SUB", "30"))   # 서브레딧당 업로드 개수
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "100"))          # 서브레딧당 Apify 검색 개수
S3_BUCKET = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
GRAPHQL_ENDPOINT = os.environ["GRAPHQL_ENDPOINT"]
API_KEY = os.environ["API_KEY"]

s3 = boto3.client("s3", region_name=AWS_REGION)


def find_image_urls(obj):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ["thumbnail", "icon_img", "all_awardings", "snoovatar"]:
                continue
            found.extend(find_image_urls(v))
    elif isinstance(obj, list):
        for i in obj:
            found.extend(find_image_urls(i))
    elif isinstance(obj, str) and obj.startswith("http"):
        lower_url = obj.lower()
        if "i.redd.it" in lower_url or "preview.redd.it" in lower_url:
            if "snoovatar" not in lower_url and "external-preview" not in lower_url:
                found.append(obj)
        elif lower_url.split("?")[0].endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            found.append(obj)
    return found


def upload_to_s3(image_bytes: bytes, key: str, content_type: str = "image/jpeg"):
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=image_bytes, ContentType=content_type)
    return key


def create_post(title, content, image_path, source_url=None, reddit_id=None):
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        id
        title
        imagePath
        redditId
      }
    }
    """
    variables = {
        "input": {
            "title": (title or "Untitled")[:200],
            "content": content or "",
            "imagePath": image_path,
            "sourceUrl": source_url,
            "redditId": reddit_id,
        }
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
    }
    resp = requests.post(
        GRAPHQL_ENDPOINT,
        json={"query": mutation, "variables": variables},
        headers=headers,
        timeout=30,
    )
    return resp.json()


def already_exists(reddit_id: str) -> bool:
    query = """
    query ListPosts {
      listPosts(limit: 200) {
        items { redditId }
      }
    }
    """
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    try:
        resp = requests.post(GRAPHQL_ENDPOINT, json={"query": query}, headers=headers, timeout=20)
        items = resp.json().get("data", {}).get("listPosts", {}).get("items", [])
        return any(item.get("redditId") == reddit_id for item in items)
    except Exception:
        return False


def process_subreddit(client: ApifyClient, subreddit: str):
    print(f"\n===== r/{subreddit} 시작 =====")

    base = f"https://www.reddit.com/r/{subreddit}"
    start_urls = [
        f"{base}/top/?t=day",
        f"{base}/hot/",
        f"{base}/rising/",
    ]

    run_input = {
        "startUrls": start_urls,
        "maxItems": MAX_ITEMS,      # 서브레딧당 최대 100개 검색
        "endPage": 20,
        "includeComments": False,
        "proxy": {
            "useApifyProxy": True,
            "apifyProxyGroups": ["RESIDENTIAL"],
        },
    }

    print(f"[Apify] r/{subreddit} 수집 중... (maxItems={MAX_ITEMS})")
    run = client.actor("epctex/reddit-scraper").call(run_input=run_input)

    try:
        dataset_id = getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None)
    except TypeError:
        dataset_id = run.get("defaultDatasetId")

    if not dataset_id:
        print(f"r/{subreddit} 데이터셋 ID 없음")
        return 0

    dataset = client.dataset(dataset_id).iterate_items()

    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(start_of_day.timestamp())
    end_ts = int(now.timestamp()) + 7200

    seen_ids = set()
    candidate_posts = []

    for item in dataset:
        post_id = item.get("id") or item.get("postId") or item.get("name")
        if not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        created_utc = item.get("created_utc") or item.get("createdAt") or item.get("created")
        if not created_utc:
            continue

        try:
            if isinstance(created_utc, str):
                dt = datetime.fromisoformat(created_utc.replace("Z", "+00:00"))
                post_ts = int(dt.timestamp())
            else:
                post_ts = int(created_utc)
        except Exception:
            continue

        if start_ts <= post_ts <= end_ts:
            candidate_posts.append(item)

    candidate_posts.sort(
        key=lambda x: x.get("score", 0) or x.get("ups", 0) or 0,
        reverse=True,
    )
    candidate_posts = candidate_posts[:LIMIT_PER_SUB]

    print(f"r/{subreddit} → 오늘 상위 {len(candidate_posts)}개 선정")

    success_count = 0

    for item in candidate_posts:
        candidate_urls = find_image_urls(item)
        if not candidate_urls:
            continue

        candidate_urls = list(set(candidate_urls))
        candidate_urls.sort(key=lambda x: "i.redd.it" not in x.lower())
        valid_image_url = candidate_urls[0].replace("&amp;", "&")

        post_id = str(item.get("id") or item.get("postId") or "unknown")
        title = item.get("title") or f"Reddit r/{subreddit}"
        content = item.get("selftext") or ""
        source_url = item.get("url") or (
            f"https://www.reddit.com{item.get('permalink')}" if item.get("permalink") else None
        )

        # 서브레딧 이름을 redditId에 포함시켜 중복 방지 강화
        unique_id = f"{subreddit}_{post_id}"

        if already_exists(unique_id):
            print(f"[스킵] 이미 존재: {unique_id}")
            continue

        try:
            response = requests.get(valid_image_url, timeout=25)
            if response.status_code != 200:
                continue

            path = urlparse(valid_image_url).path
            ext = os.path.splitext(path)[1].lower()
            if ext not in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                ext = ".jpg"

            s3_key = f"images/{unique_id}{ext}"
            content_type = response.headers.get("Content-Type", "image/jpeg")

            upload_to_s3(response.content, s3_key, content_type)
            result = create_post(title, content, s3_key, source_url, unique_id)

            if "errors" not in result:
                success_count += 1
                print(f"  [{success_count}] 성공: {title[:50]}")
            else:
                print(f"  GraphQL 에러: {result.get('errors')}")

            time.sleep(0.4)

        except Exception as e:
            print(f"  에러 {unique_id}: {e}")

    print(f"r/{subreddit} 완료 → {success_count}개 업로드")
    return success_count


def main():
    print(f"대상 서브레딧: {SUBREDDITS}")
    print(f"서브레딧당 검색: {MAX_ITEMS}개 / 업로드: {LIMIT_PER_SUB}개")

    client = ApifyClient(APIFY_TOKEN)
    total_success = 0

    for subreddit in SUBREDDITS:
        try:
            count = process_subreddit(client, subreddit)
            total_success += count
        except Exception as e:
            print(f"r/{subreddit} 처리 중 예외 발생: {e}")
            continue

        # 서브레딧 사이 잠시 대기 (예의 + rate limit)
        time.sleep(2)

    print(f"\n===== 전체 완료! 총 {total_success}개 업로드 =====")


if __name__ == "__main__":
    main()
