"""최초 1회만 실행: 카카오 로그인으로 리프레시 토큰을 발급받아 저장한다.

사용법:
    python kakao_auth.py               # 브라우저에서 카카오 로그인 창 열기
    python kakao_auth.py "<주소>"      # 로그인 후 이동한 주소로 토큰 발급 + 테스트 메시지
"""
import os
import sys
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import load_dotenv

load_dotenv()

import kakao  # noqa: E402


def open_login() -> None:
    params = {
        "client_id": os.environ["KAKAO_REST_API_KEY"],
        "redirect_uri": os.environ["KAKAO_REDIRECT_URI"],
        "response_type": "code",
        "scope": "talk_message",
    }
    url = "https://kauth.kakao.com/oauth/authorize?" + urlencode(params)
    webbrowser.open(url)
    print("브라우저에서 카카오 로그인 창을 열었습니다.")


def finish(redirected: str) -> None:
    query = parse_qs(urlparse(redirected).query)
    if "error" in query:
        raise SystemExit(f"카카오 로그인 실패: {query.get('error_description', query['error'])[0]}")
    code = query.get("code", [redirected])[0]

    tokens = kakao.exchange_code(code)
    kakao.save_refresh_token(tokens["refresh_token"])
    kakao.send_to_me(tokens["access_token"], "✅ 시황 브리핑 봇 연결 완료!")
    print("완료! 카카오톡 '나와의 채팅'을 확인해보세요.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        finish(sys.argv[1])
    else:
        open_login()
