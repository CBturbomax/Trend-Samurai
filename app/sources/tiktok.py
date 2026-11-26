"""틱톡 해시태그 통계(게시물 수·누적 조회수). 키 불필요, 비공식.

틱톡은 일반 요청에는 빈 응답을 주므로, 실제 브라우저(Chromium)로 해시태그 페이지를 열고
페이지가 스스로 받아오는 통계 응답을 읽는다.
- 받을 수 있는 건 '지금까지의 누적값'뿐. 과거 추이는 없으므로 매일 기록해서 추세를 만든다.
- 브라우저가 없거나 틱톡이 막으면 TikTokError.
"""
import os
import re

SOURCE_NAME = "TikTok 해시태그 페이지(비공식)"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")


class TikTokError(Exception):
    pass


def tag_for(term):
    """검색어 → 해시태그 (띄어쓰기·기호 제거). 예: 'rhode skin' → 'rhodeskin'"""
    return re.sub(r"[^0-9a-z가-힣]", "", term.lower())


def hashtag_stats(tags):
    """→ {tag: {'videos': 게시물 수, 'views': 누적 조회수} | None(없는 해시태그)}"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise TikTokError("브라우저(playwright)가 설치되지 않음")
    out = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.environ.get("KT_CHROMIUM") or None,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
            ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font")
                      else r.continue_())
            for tag in tags:
                page = ctx.new_page()
                try:
                    with page.expect_response(lambda r: "/api/challenge/detail" in r.url, timeout=45000) as ri:
                        page.goto(f"https://www.tiktok.com/tag/{tag}", wait_until="domcontentloaded", timeout=45000)
                    data = ri.value.json()
                except Exception as e:
                    raise TikTokError(f"#{tag} 통계를 못 받음(차단됐을 수 있음): {type(e).__name__}")
                finally:
                    page.close()
                info = data.get("challengeInfo") or {}
                st = info.get("statsV2") or info.get("stats")
                out[tag] = {"videos": int(st.get("videoCount", 0)), "views": int(st.get("viewCount", 0))} if st else None
            browser.close()
    except TikTokError:
        raise
    except Exception as e:
        raise TikTokError(f"브라우저 실행 실패: {type(e).__name__}: {str(e)[:100]}")
    return out


def creative_center(tag, period=1095, country="US"):
    """틱톡 크리에이티브 센터(공개 페이지)의 해시태그 인기 추이. 페이지가 스스로 받는 응답을 그대로 읽음."""
    from playwright.sync_api import sync_playwright
    url = f"https://ads.tiktok.com/business/creativecenter/hashtag/{tag}/pc/en?countryCode={country}&period={period}"
    got = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("KT_CHROMIUM") or None,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.on("response", lambda r: got.append(r) if "creative_radar_api" in r.url else None)
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
        except Exception:
            pass
        out = []
        for r in got:
            try:
                out.append({"url": r.url[:300], "status": r.status, "body": r.text()[:4000]})
            except Exception as e:
                out.append({"url": r.url[:300], "err": type(e).__name__})
        title = page.title()
        browser.close()
    return {"page": url, "title": title, "responses": out}
