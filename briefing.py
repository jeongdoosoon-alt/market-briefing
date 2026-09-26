"""매일 미국 시황 브리핑을 만들어 카카오톡으로 보낸다.

- 카카오톡: 핵심 요약 1개 + [전체 브리핑 보기] 버튼
- 웹페이지(docs/index.html): 전체 브리핑 (GitHub Pages로 공개)

사용법:
    python briefing.py            # 브리핑 생성 + 카카오톡 전송
    python briefing.py --dry-run  # 전송 없이 카톡 내용 출력 + 웹페이지만 생성
"""
import html
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yfinance as yf
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

import kakao

load_dotenv()

# (표시 이름, 카톡용 짧은 이름, 야후 파이낸스 티커, 표시 형식)
INDICATORS = [
    ("S&P500", "S&P", "^GSPC", "index"),
    ("나스닥", "나스닥", "^IXIC", "index"),
    ("다우", "다우", "^DJI", "index"),
    ("필라델피아 반도체(SOX)", "반도체", "^SOX", "index"),
    ("MSCI 한국 ETF(EWY)", "EWY", "EWY", "usd"),
    ("VIX 공포지수", "VIX", "^VIX", "index"),
    ("미국 10년물 금리", "10년물", "^TNX", "yield"),
    ("원/달러 환율", "원/달러", "KRW=X", "index"),
    ("WTI 유가", "WTI", "CL=F", "usd"),
]
KAKAO_SUMMARY_TICKERS = ["^GSPC", "^IXIC", "^SOX", "EWY"]  # 카톡에 보여줄 지표
WEEKDAYS = "월화수목금토일"
KST = timezone(timedelta(hours=9))
DOCS_DIR = Path(__file__).parent / "docs"

# 무료 뉴스 피드 (Gemini 검색 기능은 무료 한도가 없어서 뉴스는 직접 가져온다)
NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=stock+market+today+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=wall+street+stocks+close+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=federal+reserve+OR+treasury+yields+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://www.cnbc.com/id/15839069/device/rss/rss.html",  # CNBC Markets
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Top News
]
MAX_HEADLINES = 60

# 앞 모델이 혼잡하거나 단종되면 다음 모델로 넘어간다
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]


@dataclass
class Quote:
    name: str
    short: str
    ticker: str
    value: str  # 예: "7,743.41", "5.18%"
    change: str  # 예: "▲0.51%", "+2bp"
    direction: int  # 1 상승, -1 하락, 0 보합/없음


class Sector(BaseModel):
    name: str = Field(description="섹터 이름 (예: 반도체)")
    reason: str = Field(description="이유 한 문장")


class Briefing(BaseModel):
    headline: str = Field(description="오늘 미국 시장 한 문장 요약, 50자 이내")
    issues: list[str] = Field(description="시장을 움직인 원인 2~3개, 각 한 문장")
    checkpoints: list[str] = Field(description="앞으로 주목할 일정/변수 1~2개, 각 한 문장")
    korea_outlook: str = Field(description="오늘 한국 증시 예상 흐름: 강세, 약세, 혼조 중 하나")
    korea_reason: str = Field(description="한국 증시 예상 흐름의 이유, 1~2문장")
    positive_sectors: list[Sector] = Field(description="오늘 주목할 긍정적 한국 섹터 1~2개")
    caution_sectors: list[Sector] = Field(description="오늘 부담이 될 수 있는 한국 섹터 1개")


def fetch_market() -> tuple[date, list[Quote]]:
    """지표별 최근 종가와 전일 대비 변화를 가져온다. 숫자는 AI가 아닌 데이터에서만 가져온다."""
    quotes, session_date = [], None
    for name, short, ticker, kind in INDICATORS:
        closes = yf.Ticker(ticker).history(period="7d")["Close"].dropna()
        if len(closes) < 2:
            quotes.append(Quote(name, short, ticker, "-", "데이터 없음", 0))
            continue
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        if ticker == "^GSPC":
            session_date = closes.index[-1].date()
        if kind == "yield":
            bp = (last - prev) * 100
            direction = (bp > 0) - (bp < 0)
            quotes.append(Quote(name, short, ticker, f"{last:.2f}%", f"{bp:+.0f}bp", direction))
        else:
            pct = (last / prev - 1) * 100
            direction = (pct > 0) - (pct < 0)
            arrow = {1: "▲", -1: "▼", 0: "-"}[direction]
            prefix = "$" if kind == "usd" else ""
            quotes.append(Quote(name, short, ticker, f"{prefix}{last:,.2f}", f"{arrow}{abs(pct):.2f}%", direction))
    return session_date or date.today(), quotes


def fetch_news() -> list[str]:
    """최근 36시간 안의 뉴스 헤드라인을 모은다."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    seen, headlines = set(), []
    for url in NEWS_FEEDS:
        try:
            res = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            items = ET.fromstring(res.content).findall(".//item")
        except Exception as e:
            print(f"[경고] 뉴스 피드 실패: {url} ({e})", file=sys.stderr)
            continue
        for item in items:
            title = (item.findtext("title") or "").strip()
            try:
                published = parsedate_to_datetime(item.findtext("pubDate"))
            except (TypeError, ValueError):
                continue
            key = title.lower()[:60]
            if title and published >= cutoff and key not in seen:
                seen.add(key)
                headlines.append(title)
    return headlines[:MAX_HEADLINES]


def build_prompt(session_date: date, quotes: list[Quote], headlines: list[str]) -> str:
    d = f"{session_date:%Y-%m-%d}({WEEKDAYS[session_date.weekday()]})"
    numbers = "\n".join(f"{q.name} {q.value} ({q.change})" for q in quotes)
    news = "\n".join(f"- {h}" for h in headlines) or "(수집된 뉴스 없음)"
    return f"""당신은 한국 직장인에게 매일 아침 미국 증시 시황을 알려주는 경제 브리핑 담당자입니다.
아래 {d} 미국 증시 마감 수치와 뉴스 헤드라인만 근거로 브리핑을 작성하세요.

[확정된 마감 수치 - 이 숫자만 사용하고 다른 수치를 지어내지 마세요]
{numbers}

[최근 뉴스 헤드라인 (영문)]
{news}

[작성 규칙]
- 헤드라인에 없는 사실은 지어내지 마세요. 증시와 관련 없는 뉴스는 무시하세요.
- 한국어, 쉬운 말로. 경제 초보도 이해할 수 있게. 문장은 짧게.
- 한국 증시 분석에는 반도체지수(SOX)는 삼성전자·SK하이닉스, 한국 ETF(EWY)는 외국인 수급,
  원/달러는 수출주와 외국인 매매, 유가는 정유·항공·화학의 단서로 활용하세요.
- 개별 종목 매수/매도 추천은 하지 마세요. 전망은 "~할 가능성", "~에 주목"처럼 가능성으로 표현하세요.
"""


def generate_briefing(prompt: str) -> Briefing:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    preferred = os.environ.get("GEMINI_MODEL")
    models = ([preferred] if preferred else []) + [m for m in FALLBACK_MODELS if m != preferred]
    last_error = None
    for model in models:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    response_mime_type="application/json",
                    response_schema=Briefing,
                ),
            )
            return Briefing.model_validate_json(response.text)
        except Exception as e:
            print(f"[경고] {model} 실패, 다음 모델 시도: {str(e)[:80]}", file=sys.stderr)
            last_error = e
    raise RuntimeError(f"모든 Gemini 모델 실패: {last_error}")


def session_label(session_date: date) -> str:
    return f"{session_date:%m/%d}({WEEKDAYS[session_date.weekday()]})"


def build_kakao_text(session_date: date, quotes: list[Quote], b: Briefing | None) -> str:
    by_ticker = {q.ticker: q for q in quotes}
    picks = [by_ticker[t] for t in KAKAO_SUMMARY_TICKERS if t in by_ticker]
    pairs = [f"{q.short} {q.change}" for q in picks]
    lines = [f"🇺🇸 {session_label(session_date)} 미국 마감"]
    lines += [" | ".join(pairs[i:i + 2]) for i in range(0, len(pairs), 2)]
    if b:
        sectors = "·".join(s.name for s in b.positive_sectors[:2])
        korea = f"🇰🇷 오늘 한국: {b.korea_outlook} 예상"
        korea += f" (주목: {sectors})" if sectors else ""
        fixed = "\n".join(lines + [korea])
        room = kakao.MAX_TEXT - len(fixed) - len("\n📌 ")
        headline = b.headline if len(b.headline) <= room else b.headline[: room - 1] + "…"
        lines += [f"📌 {headline}", korea]
    else:
        lines.append("(AI 요약 실패 - 숫자만 전송)")
    return "\n".join(lines)[: kakao.MAX_TEXT]


def build_html(session_date: date, quotes: list[Quote], b: Briefing | None) -> str:
    e = html.escape
    color = {1: "up", -1: "down", 0: ""}
    rows = "\n".join(
        f'<tr><td>{e(q.name)}</td><td class="num">{e(q.value)}</td>'
        f'<td class="num {color[q.direction]}">{e(q.change)}</td></tr>'
        for q in quotes
    )
    if b:
        issues = "".join(f"<li>{e(x)}</li>" for x in b.issues)
        checks = "".join(f"<li>{e(x)}</li>" for x in b.checkpoints)
        pos = "".join(f"<li><b>{e(s.name)}</b> — {e(s.reason)}</li>" for s in b.positive_sectors)
        neg = "".join(f"<li><b>{e(s.name)}</b> — {e(s.reason)}</li>" for s in b.caution_sectors)
        outlook_cls = {"강세": "up", "약세": "down"}.get(b.korea_outlook.strip(), "")
        body = f"""
  <p class="headline">📌 {e(b.headline)}</p>
  <section class="card korea">
    <h2>🇰🇷 오늘 한국 증시 전망</h2>
    <p class="outlook"><span class="badge {outlook_cls}">{e(b.korea_outlook)}</span> {e(b.korea_reason)}</p>
    <h3>주목 섹터</h3><ul>{pos}</ul>
    <h3>주의 섹터</h3><ul>{neg}</ul>
  </section>
  <section class="card"><h2>📰 주요 이슈</h2><ul>{issues}</ul></section>
  <section class="card"><h2>👀 체크포인트</h2><ul>{checks}</ul></section>"""
    else:
        body = '<p class="headline">AI 요약을 만들지 못했습니다. 숫자만 확인해주세요.</p>'

    updated = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>미국 시황 브리핑 {session_label(session_date)}</title>
<style>
  :root {{ --bg:#f5f6f8; --card:#fff; --text:#1d1f23; --sub:#6b7280; --line:#e5e7eb; --up:#d92d20; --down:#1f6feb; --accent:#fee500; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111315; --card:#1c1f23; --text:#e8eaed; --sub:#9aa0a6; --line:#2d3136; --up:#ff6b5e; --down:#5b9dff; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.6 -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }}
  main {{ max-width:640px; margin:0 auto; padding:20px 16px 40px; }}
  header h1 {{ font-size:22px; margin:0; }}
  header p {{ color:var(--sub); margin:4px 0 0; font-size:14px; }}
  .headline {{ font-size:18px; font-weight:700; margin:20px 0; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin:14px 0; }}
  .card h2 {{ font-size:17px; margin:0 0 10px; }}
  .card h3 {{ font-size:14px; color:var(--sub); margin:14px 0 4px; }}
  ul {{ margin:0; padding-left:20px; }}
  li {{ margin:4px 0; }}
  table {{ width:100%; border-collapse:collapse; font-size:15px; }}
  td {{ padding:7px 0; border-bottom:1px solid var(--line); }}
  tr:last-child td {{ border-bottom:0; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; padding-left:10px; }}
  .up {{ color:var(--up); }} .down {{ color:var(--down); }}
  .korea {{ border-color:var(--accent); border-width:2px; }}
  .badge {{ display:inline-block; font-weight:700; padding:1px 10px; border-radius:999px; border:1.5px solid currentColor; margin-right:4px; }}
  footer {{ color:var(--sub); font-size:12px; margin-top:24px; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>🇺🇸 미국 시황 브리핑</h1>
    <p>{session_date:%Y-%m-%d}({WEEKDAYS[session_date.weekday()]}) 미국 마감 기준</p>
  </header>
  {body}
  <section class="card"><h2>📊 마감 지표</h2><table>{rows}</table></section>
  <footer>
    데이터: Yahoo Finance · 뉴스: Google News, CNBC · 요약: Gemini<br>
    AI가 자동으로 작성한 참고 자료이며 투자 권유가 아닙니다. · 생성 {updated} KST
  </footer>
</main>
</body>
</html>
"""


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    session_date, quotes = fetch_market()

    try:
        b = generate_briefing(build_prompt(session_date, quotes, fetch_news()))
    except Exception as e:  # AI가 실패해도 숫자 브리핑은 보낸다
        print(f"[경고] AI 요약 실패: {e}", file=sys.stderr)
        b = None

    page = build_html(session_date, quotes, b)
    (DOCS_DIR / "archive").mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(page, encoding="utf-8")
    (DOCS_DIR / "archive" / f"{session_date:%Y-%m-%d}.html").write_text(page, encoding="utf-8")

    text = build_kakao_text(session_date, quotes, b)
    if dry_run:
        print(f"----- 카톡 메시지 ({len(text)}자) -----\n{text}\n")
        print(f"웹페이지: {DOCS_DIR / 'index.html'}")
        return

    access_token, refresh_token = kakao.refresh_access_token(kakao.load_refresh_token())
    if not os.environ.get("KAKAO_REFRESH_TOKEN"):
        kakao.save_refresh_token(refresh_token)
    elif refresh_token != os.environ["KAKAO_REFRESH_TOKEN"]:
        # GitHub Actions에서 새 리프레시 토큰을 Secret에 다시 저장하도록 알린다
        print(f"::add-mask::{refresh_token}")  # 실행 로그에 토큰이 보이지 않게 가림
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as f:
            f.write(f"new_refresh_token={refresh_token}\n")

    link = os.environ.get("BRIEFING_URL") or "https://finance.yahoo.com"
    kakao.send_to_me(access_token, text, link_url=link, button_title="전체 브리핑 보기")
    print("카카오톡 전송 완료")


if __name__ == "__main__":
    main()
