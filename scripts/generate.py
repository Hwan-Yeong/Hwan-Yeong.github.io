#!/usr/bin/env python3
"""자동 발행 파이프라인 - 글 생성기 (5장 2·3절)

keywords.csv에서 '아직 발행하지 않은' 키워드를 priority가 낮은 순으로 하나 골라
Claude(claude-opus-5)로 SEO 블로그 글 초안을 생성해 content/posts/<slug>.md 로 저장한다.

- 중복 방지: data/published.txt 에 이미 있거나, 같은 slug 파일이 이미 있으면 건너뛴다.
- 큐 소진: 쓸 키워드가 없으면 generated=false, reason=queue_exhausted 를 출력한다.
- 결과는 GitHub Actions의 $GITHUB_OUTPUT 으로 넘겨 PR 생성 단계가 사용한다.

이 스크립트는 발행 자체를 하지 않는다. draft:false 글을 만들어 '초안 PR'에 올릴 뿐이며,
실제 발행은 사람이 PR을 검수하고 Merge 할 때 배포 워크플로가 담당한다(사람 검수 게이트).
"""

import csv
import os
import re
import pathlib
from datetime import datetime, timezone, timedelta

import anthropic

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEYWORDS = ROOT / "keywords.csv"
PUBLISHED = ROOT / "data" / "published.txt"
POSTS = ROOT / "content" / "posts"

MODEL = "claude-opus-5"
KST = timezone(timedelta(hours=9))

PROMPT = """너는 한국 주식·금융을 직접 공부하고 투자해 본 정보형 블로거다. 아래 키워드로 한국어 SEO 블로그 글 한 편을 마크다운으로 써라.

키워드: {keyword}
검색 의도: {intent}
제목 후보: {suggested_title}
이 글에서 꼭 다룰 것: {notes}
카테고리(줄기): {seed}
오늘 날짜: {today}

[구조 규칙]
1. 맨 위에 Hugo front matter를 붙여라. --- 두 줄 사이에 다음을 넣는다:
   title(키워드를 앞쪽에 자연스럽게 넣고 클릭을 부르되 낚시 금지),
   date: {today},
   draft: false,
   categories: ["{seed}"],
   tags: (본문에서 다룬 롱테일 3~5개),
   description(검색 결과 미리보기용 한 줄, 이 글을 읽으면 얻는 이득을 압축).
2. 본문 첫 줄은 글 제목(# ...).
3. 서론은 두괄식 2~3문단. 첫 문장에서 검색자의 질문에 곧장 답하라. "~에 대해 알아보겠습니다" 같은 예고형 서론 금지.
4. 서론 바로 다음에 '핵심 요약 박스'를 인용블록(>)으로 3줄 이내, 결론만 넣어라.
5. 본문은 ## 소제목 4~6개로 나눠라. 각 소제목 아래에 구체적 정보·단계·예시·수치를 담아라. 일반론·뜬구름 금지. 소제목은 스캔하다 멈출 만큼 구체적으로.
6. 적어도 한 곳에는 비교나 절차 정리를 위한 마크다운 표를 넣어라.
7. 글 끝에 '## 자주 묻는 질문' 섹션으로, 본문 내용을 근거로 한 Q&A 3~5개. 각 질문은 사람들이 진짜 궁금해할 롱테일 형태로.
8. 맨 끝에 인용블록(>)으로 '본 글은 투자 권유가 아닌 정보 제공을 위한 것이며, 본 정보는 {today} 기준입니다. 제도·수치는 변경될 수 있으니 발행 전 공식 자료로 확인하시기 바랍니다.' 한 줄을 넣어라.

[검색의도·정직성·정책 규칙]
- 검색 의도가 '실행형'이면 '따라 하면 끝나는 절차'를, '비교형'이면 '무엇을 기준으로 고를지'를, '정보형'이면 '개념을 정확히'를 글의 중심에 둬라.
- 다른 글과 겹치는 일반론(예: "배당주란 무엇인가")을 길게 반복하지 마라. 이 글은 '{keyword}'에 정면으로 답하는 데만 집중하라.
- 세율·한도·날짜 규칙 등 정확한 수치는 일반적으로 알려진 값을 쓰되 단정하지 말고, "제도가 바뀔 수 있으니 확인 필요" 뉘앙스를 유지하라. 확실하지 않으면 지어내지 마라.
- 특정 종목의 매수·매도를 권유하거나 "무조건 오른다" 류의 단정적 투자 권유 표현을 절대 쓰지 마라. 정보 제공에 한정하라.
- 사람의 1차 경험이 필요한 자리에는 지어내지 말고 '[경험 추가 필요]'라고만 표시해 둬라. 가상의 인물이나 거짓 사례 금지.

[출력 형식]
- 오직 완성된 마크다운 본문만 출력하라. 코드펜스(```), 머리말/맺음말, 설명 문장을 붙이지 마라. 첫 글자는 반드시 '---'(front matter 시작)이어야 한다.
"""


def gh_output(**kw):
    """GitHub Actions로 결과 값을 넘긴다(없으면 표준출력)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in kw.items():
            print(f"{k}={v}")
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kw.items():
            f.write(f"{k}={v}\n")


def load_published():
    if PUBLISHED.exists():
        return {ln.strip() for ln in PUBLISHED.read_text(encoding="utf-8").splitlines() if ln.strip()}
    return set()


def pick_row():
    """priority 낮은 순으로, 아직 발행 안 한 첫 키워드 행을 고른다."""
    published = load_published()
    with open(KEYWORDS, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: int(r["priority"]))
    for r in rows:
        slug = r["slug"].strip()
        if r["keyword"].strip() in published:
            continue
        if (POSTS / f"{slug}.md").exists():
            continue
        return r
    return None


def generate_markdown(row, today):
    """Claude로 글을 생성해 마크다운 문자열을 돌려준다."""
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수(=금고에서 온 시크릿)를 자동 사용
    prompt = PROMPT.format(
        keyword=row["keyword"].strip(),
        intent=row["intent"].strip(),
        suggested_title=row["suggested_title"].strip(),
        notes=row["notes"].strip(),
        seed=row["seed"].strip(),
        today=today,
    )
    # 긴 출력이므로 스트리밍으로 받아 타임아웃을 피한다(스킬 권장).
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()
    # thinking 블록은 무시하고 text 블록만 이어붙인다.
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    return text


def clean_markdown(md):
    """혹시 감싸 나온 코드펜스를 제거하고 draft를 반드시 false로 강제한다."""
    md = md.strip()
    md = re.sub(r"^```[a-zA-Z]*\n", "", md)
    md = re.sub(r"\n```$", "", md).strip()
    # 안전장치: 모델이 draft: true로 내놓아도 검수 후 Merge로 발행하는 구조이므로 false로 통일
    md = re.sub(r"(?m)^draft:\s*true\s*$", "draft: false", md)
    return md


def main():
    row = pick_row()
    if not row:
        gh_output(generated="false", reason="queue_exhausted")
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    md = clean_markdown(generate_markdown(row, today))

    if not md.startswith("---"):
        # front matter가 없으면 발행 사고를 막기 위해 실패로 처리(워크플로가 실패 이슈를 연다).
        raise RuntimeError("생성된 글에 Hugo front matter(---)가 없습니다. 프롬프트/응답을 확인하세요.")

    slug = row["slug"].strip()
    POSTS.mkdir(parents=True, exist_ok=True)
    (POSTS / f"{slug}.md").write_text(md + "\n", encoding="utf-8")

    # 발행 장부에 키워드를 기록(중복 방지). 이 변경도 PR에 함께 담긴다.
    PUBLISHED.parent.mkdir(parents=True, exist_ok=True)
    with open(PUBLISHED, "a", encoding="utf-8") as f:
        f.write(row["keyword"].strip() + "\n")

    m = re.search(r'(?m)^title:\s*"?(.*?)"?\s*$', md)
    title = (m.group(1) if m else row["suggested_title"]).replace("\n", " ").strip()

    gh_output(generated="true", slug=slug, keyword=row["keyword"].strip(), title=title)


if __name__ == "__main__":
    main()
