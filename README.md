# tkLeen deck engine

tkLeen 모션 HTML 슬라이드(형식 `tkleen-deck`)의 엔진과 조립 도구입니다. 현재 엔진 v1.6.1.

| 파일 | 내용 |
|:--|:--|
| `deck-template.html` | 고정 엔진과 스타일. deck-data JSON 자리만 채운다 |
| `deck_build.py` | 조립(build)·JSON 꺼내기(extract)·형식 점검(check) |
| `deck-schema.md` | JSON 형식과 모든 규칙의 기준 |
| `deck-example.json` | 7쪽 예시(기본 스타일) |
| `claude/motion-samples.json` | 모션 견본 15쪽 원본 |
| `claude/klein-example.json` | Klein 스타일 예시 4쪽 |

받기:

```bash
git clone --depth 1 https://github.com/taekyoleen-oss/tkleen-deck-engine.git
# 또는 파일 하나씩
curl -fsSLO https://raw.githubusercontent.com/taekyoleen-oss/tkleen-deck-engine/main/deck-template.html
```

조립: `python deck_build.py build deck-template.html data.json 결과.html`

편집 앱: https://motion-slide.vercel.app
