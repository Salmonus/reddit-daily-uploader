import os
import time
import tempfile
import requests
import boto3
import tweepy

GRAPHQL_ENDPOINT = os.environ["GRAPHQL_ENDPOINT"]
API_KEY = os.environ["API_KEY"]
S3_BUCKET = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

X_API_KEY = os.environ["X_API_KEY"]
X_API_SECRET = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_TOKEN_SECRET = os.environ["X_ACCESS_TOKEN_SECRET"]

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "10"))

s3 = boto3.client("s3", region_name=AWS_REGION)


def gql(query, variables=None):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
    }
    resp = requests.post(
        GRAPHQL_ENDPOINT,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=30,
    )
    data = resp.json()
    if "errors" in data:
        print("GraphQL errors:", data["errors"])
    return data


def list_approved_posts():
    # 페이지네이션 단순화: 최근 200개 중 approved만
    query = """
    query ListPosts {
      listPosts(limit: 200) {
        items {
          id
          title
          imagePath
          status
          sourceUrl
        }
      }
    }
    """
    result = gql(query)
    items = result.get("data", {}).get("listPosts", {}).get("items", []) or []
    return [p for p in items if (p.get("status") or "") == "approved" and p.get("imagePath")]


def mark_posted(post_id: str):
    mutation = """
    mutation UpdatePost($input: UpdatePostInput!) {
      updatePost(input: $input) {
        id
        status
      }
    }
    """
    return gql(mutation, {"input": {"id": post_id, "status": "posted"}})


def download_from_s3(image_path: str) -> str:
    """S3 객체를 임시 파일로 저장하고 경로 반환"""
    suffix = os.path.splitext(image_path)[1] or ".jpg"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    s3.download_file(S3_BUCKET, image_path, tmp_path)
    return tmp_path


def post_image_to_x(image_path_local: str, title: str = "") -> bool:
    # v1.1 미디어 업로드 + v2 트윗
    auth = tweepy.OAuth1UserHandler(
        X_API_KEY,
        X_API_SECRET,
        X_ACCESS_TOKEN,
        X_ACCESS_TOKEN_SECRET,
    )
    api_v1 = tweepy.API(auth)

    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
    )

    media = api_v1.media_upload(filename=image_path_local)
    media_id = media.media_id_string

    # 이미지만 올리기 (빈 텍스트 거절 가능성 있어 공백)
    client.create_tweet(text=" ", media_ids=[media_id])
    return True


def main():
    print("approved 게시글 조회 중...")
    posts = list_approved_posts()
    print(f"대상 {len(posts)}개")

    success = 0
    for post in posts[:MAX_POSTS_PER_RUN]:
        post_id = post["id"]
        image_path = post["imagePath"]
        title = post.get("title") or ""
        tmp = None
        try:
            print(f"처리 중: {post_id} / {title[:40]}")
            tmp = download_from_s3(image_path)
            post_image_to_x(tmp, title)
            mark_posted(post_id)
            success += 1
            print(f"  성공 → status=posted")
            time.sleep(2)
        except Exception as e:
            print(f"  실패: {e}")
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    print(f"완료: {success}개 X 업로드")


if __name__ == "__main__":
    main()
