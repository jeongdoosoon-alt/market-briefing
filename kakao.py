"""카카오톡 '나에게 보내기' 전송과 토큰 관리."""
import json
import os
from pathlib import Path

import requests

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TOKEN_FILE = Path(__file__).parent / "kakao_tokens.json"
MAX_TEXT = 200  # 카카오 텍스트 템플릿 최대 글자 수


def _client_params() -> dict:
    params = {"client_id": os.environ["KAKAO_REST_API_KEY"]}
    secret = os.environ.get("KAKAO_CLIENT_SECRET")
    if secret:
        params["client_secret"] = secret
    return params


def exchange_code(code: str) -> dict:
    """최초 1회: 인가 코드로 토큰 발급."""
    data = {
        "grant_type": "authorization_code",
        "redirect_uri": os.environ["KAKAO_REDIRECT_URI"],
        "code": code,
        **_client_params(),
    }
    res = requests.post(TOKEN_URL, data=data, timeout=15)
    res.raise_for_status()
    return res.json()


def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """리프레시 토큰으로 새 액세스 토큰 발급.

    리프레시 토큰은 약 2개월 유효하며, 만료가 1개월 이내로 남으면 카카오가
    새 리프레시 토큰을 함께 내려준다. 반환값: (access_token, 최신 refresh_token)
    """
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, **_client_params()}
    res = requests.post(TOKEN_URL, data=data, timeout=15)
    res.raise_for_status()
    body = res.json()
    return body["access_token"], body.get("refresh_token", refresh_token)


def load_refresh_token() -> str:
    """환경변수(GitHub Actions) 우선, 없으면 로컬 파일에서 읽기."""
    token = os.environ.get("KAKAO_REFRESH_TOKEN")
    if token:
        return token
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["refresh_token"]
    raise RuntimeError("카카오 토큰이 없습니다. 먼저 `python kakao_auth.py` 를 실행하세요.")


def save_refresh_token(token: str) -> None:
    TOKEN_FILE.write_text(json.dumps({"refresh_token": token}, indent=2), encoding="utf-8")


def split_messages(text: str, limit: int = MAX_TEXT) -> list[str]:
    """줄 단위로 끊어서 limit 글자 이하 메시지 여러 개로 나눈다."""
    chunks, current = [], ""
    for line in text.strip().splitlines():
        while len(line) > limit:  # 한 줄이 너무 긴 경우 강제로 자르기
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def send_to_me(
    access_token: str,
    text: str,
    link_url: str = "https://finance.yahoo.com",
    button_title: str | None = None,
) -> None:
    template = {
        "object_type": "text",
        "text": text[:MAX_TEXT],
        "link": {"web_url": link_url, "mobile_web_url": link_url},
    }
    if button_title:
        template["button_title"] = button_title
    res = requests.post(
        SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=15,
    )
    if res.status_code != 200:
        raise RuntimeError(f"카카오톡 전송 실패 ({res.status_code}): {res.text}")
