#!/usr/bin/env python3
"""tkLeen deck 조립 도구 (deck-template.html + deck-data JSON -> 발표용 HTML 한 파일) · 엔진 v1.6.1 기준

사용법
  python deck_build.py build  deck-template.html data.json out.html [--pdf 원본.pdf]
  python deck_build.py extract 기존덱.html data.json        # 이미 만든 덱에서 JSON만 꺼내기
  python deck_build.py check  data.json                     # 형식 점검만

--pdf 를 주면 layout "page" 이면서 "pdfPage"(1부터)가 있는 쪽을 그림(WebP, 긴 변 1600px)으로 만들어 넣는다.
image 블록의 src가 PC의 그림 파일 경로이면 파일을 읽어 assets에 넣는다.
"""
import base64, io, json, os, re, subprocess, sys, tempfile

LAYOUTS = {"cover", "divider", "content", "page"}
BLOCKS = {"text", "list", "cards", "table", "flow", "stats", "callout", "image", "row", "html",
          "section", "bars", "switch", "math",   # section: v1.1.0 · bars, switch: v1.2.0 · math: v1.3.0
          "stack", "grid", "sim"}                # v1.6.0
MOTION_LEVELS = {"none", "basic", "rich"}
STYLES = {"tkleen", "klein"}                     # v1.6.0 theme.style
# v1.4.0 개별 모션
FX_ENTER = {"rise", "fade", "left", "right", "drop", "pop", "wipe"}
FX_EMPH = {"mark", "pulse", "result", "count"}
ENTERS = {"fade", "zoom", "slide", "rise", "none"}
SPEEDS = {"slow", "normal", "fast"}
SLIDE_MOTION_KEYS = {"stagger", "ring", "zoom", "grow", "enter", "speed"}
REF_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def ok_speed(v):
    """v1.6.1: slow · normal · fast 또는 배율 숫자 0.5–3."""
    return v is None or (isinstance(v, str) and v in SPEEDS) or (isinstance(v, (int, float)) and not isinstance(v, bool) and 0.5 <= v <= 3)


def check_fx(o, where, btype, err, warn):
    fx = o.get("fx")
    if fx is not None:
        toks = fx if isinstance(fx, list) else str(fx).split()
        bad = [t for t in toks if t not in FX_ENTER | FX_EMPH]
        if bad:
            err.append(f"{where}: fx 값 {bad}을 알 수 없습니다")
        if sum(t in FX_ENTER for t in toks) > 1:
            warn.append(f"{where}: 등장 효과(fx)는 하나만 씁니다")
        if "result" in toks and btype != "math":
            warn.append(f"{where}: result는 math 블록·수식 항목에만 효과가 있습니다")
        if "count" in toks and btype not in ("stats", "table", "bars", "math", "stack"):
            warn.append(f"{where}: count는 stats·table·bars·math·stack에만 효과가 있습니다")
    od = o.get("order")
    if od is not None and od != "last" and not (isinstance(od, int) and od > 0):
        err.append(f"{where}: order는 1 이상의 정수 또는 \"last\"입니다")
    rf = o.get("ref")
    if rf is not None and not (isinstance(rf, str) and REF_RE.match(rf)):
        err.append(f"{where}: ref는 영문·숫자·_·- 40자 이내의 이름입니다")
    nr = o.get("noteRows")
    if nr is not None:
        if not (isinstance(nr, list) and 1 <= len(nr) <= 8 and all(isinstance(r, list) and 1 <= len(r) <= 2 for r in nr)):
            err.append(f"{where}: noteRows는 [[\"항목\", \"값\"], …] 형태로 1–8행입니다")
    if o.get("goView") is not None and not (isinstance(o.get("goView"), int) and o["goView"] >= 1):
        err.append(f"{where}: goView는 1 이상의 정수(보기 번호)입니다")
    if (o.get("goRef") or o.get("goView")) and not o.get("go"):
        warn.append(f"{where}: goRef·goView는 go와 함께 써야 동작합니다")
DATA_RE = re.compile(r'(<script type="application/json" id="deck-data">)(.*?)(</script>)', re.S)


def page_image(pdf, no, long_side=1600):
    """PDF 한 쪽을 WebP data URI로 만든다. PyMuPDF가 없으면 pdftoppm을 쓴다."""
    from PIL import Image
    try:
        try:
            import pymupdf as fitz  # PyMuPDF
        except ImportError:
            import fitz
        doc = fitz.open(pdf)
        pg = doc[no - 1]
        z = long_side / max(pg.rect.width, pg.rect.height)
        pix = pg.get_pixmap(matrix=fitz.Matrix(z, z))
        im = Image.open(io.BytesIO(pix.tobytes("png")))
    except ImportError:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["pdftoppm", "-f", str(no), "-l", str(no), "-png", "-scale-to", str(long_side), pdf, os.path.join(td, "p")], check=True)
            name = [f for f in os.listdir(td) if f.endswith(".png")][0]
            im = Image.open(os.path.join(td, name)).copy()
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "WEBP", quality=82)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode(), im.width / im.height


def file_image(path, long_side=1600):
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((long_side, long_side))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "WEBP", quality=82)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()


def walk_blocks(blocks):
    for b in blocks or []:
        yield b
        if b.get("type") == "row":
            for col in b.get("cols", []):
                yield from walk_blocks(col)
        if b.get("type") == "switch":
            for v in b.get("views", []):
                yield from walk_blocks(v.get("blocks"))


MATH_BAD = re.compile(r"<\s*(script|style|iframe|object|embed|foreignObject|annotation-xml)|\son\w+\s*=|javascript:", re.I)


def tex_to_mathml(tex):
    """math 블록의 tex를 MathML로 바꾼다 (pip install latex2mathml)."""
    try:
        from latex2mathml.converter import convert
    except ImportError:
        sys.exit("tex를 MathML로 바꾸려면 pip install latex2mathml 이 필요합니다. 또는 mathml을 직접 적으세요.")
    return convert(tex, display="block")


def text_fields(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k not in ("assets", "mathml"):
                yield from text_fields(v)
    elif isinstance(o, list):
        for v in o:
            yield from text_fields(v)
    elif isinstance(o, str):
        yield o


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def sim_eval(m, vals):
    f = m.get("formula") or {}
    if f.get("type") == "linear":
        return (f.get("intercept") or 0) + sum(v * c for v, c in zip(vals, f["coefficients"]))
    p = f["coefficient"]
    for v, k in zip(vals, f["powers"]):
        p *= v ** k
    return p


def check_sim(b, where, err):
    """v1.6.0 sim(레버 계산기) 점검. 엔진의 simCheck와 같은 기준."""
    V, M = b.get("variables"), b.get("metrics")
    if not (isinstance(V, list) and 1 <= len(V) <= 4):
        err.append(f"{where}: sim의 variables(레버)는 1–4개입니다"); return
    if not (isinstance(M, list) and 1 <= len(M) <= 3):
        err.append(f"{where}: sim의 metrics(지표)는 1–3개입니다"); return
    keys = set()
    for i, v in enumerate(V, 1):
        k = v.get("key")
        if k is not None and (not re.match(r"^[a-z][a-z0-9_]*$", str(k)) or k in keys):
            err.append(f"{where}: 레버 {i}의 key가 잘못되었거나 겹칩니다")
        keys.add(k)
        if not all(_isnum(v.get(x)) for x in ("min", "max", "step", "default", "baseline")):
            err.append(f"{where}: 레버 {i}의 min·max·step·default·baseline은 수여야 합니다"); return
        if not (v["max"] > v["min"] and v["step"] > 0 and v["min"] <= v["default"] <= v["max"] and v["min"] <= v["baseline"] <= v["max"]):
            err.append(f"{where}: 레버 {i}의 범위(min < max, step > 0, default·baseline이 범위 안)가 맞지 않습니다"); return
        for x in ("default", "baseline"):
            q = (v[x] - v["min"]) / v["step"]
            if abs(q - round(q)) > 1e-8:
                err.append(f"{where}: 레버 {i}의 {x} 값은 min + n×step 자리에 있어야 합니다")
    for j, m in enumerate(M, 1):
        f = m.get("formula") or {}
        d = m.get("decimals")
        if d is not None and not (isinstance(d, int) and 0 <= d <= 4):
            err.append(f"{where}: 지표 {j}의 decimals는 0–4입니다")
        t = f.get("type")
        if t in ("product", "product3"):
            pw = f.get("powers")
            if not (_isnum(f.get("coefficient")) and isinstance(pw, list) and len(pw) == len(V)
                    and all(p in (-1, 0, 1, 2, 3) and not isinstance(p, bool) for p in pw)):
                err.append(f"{where}: 지표 {j}의 product 식은 coefficient(수)와 레버 수만큼의 powers(-1·0·1·2·3)가 필요합니다"); continue
            if any(p < 0 and V[k]["min"] <= 0 for k, p in enumerate(pw)):
                err.append(f"{where}: 지표 {j}: 나누는 레버(powers -1)의 min은 0보다 커야 합니다"); continue
        elif t == "linear":
            cs = f.get("coefficients")
            if not (isinstance(cs, list) and len(cs) == len(V) and all(_isnum(c) for c in cs)
                    and (f.get("intercept") is None or _isnum(f.get("intercept")))):
                err.append(f"{where}: 지표 {j}의 linear 식은 레버 수만큼의 coefficients와 수 intercept가 필요합니다"); continue
        else:
            err.append(f"{where}: 지표 {j}의 formula.type은 product 또는 linear입니다"); continue
        for mask in range(1 << len(V)):
            vals = [v["max"] if (mask >> k) & 1 else v["min"] for k, v in enumerate(V)]
            try:
                x = sim_eval(m, vals)
            except (ZeroDivisionError, OverflowError):
                x = None
            if x is None or not _isnum(x) or x < 0:
                err.append(f"{where}: 지표 {j}가 레버 범위 끝에서 음수이거나 계산되지 않습니다"); break
        if m.get("format") not in (None, "hoursMinutes"):
            err.append(f"{where}: 지표 {j}의 format은 hoursMinutes만 씁니다")


def refs_of(s):
    """쪽 안의 ref 이름과 switch 블록(ref → 보기 수)을 모은다."""
    refs, sw = set(), []
    for b in walk_blocks(s.get("blocks")):
        if b.get("ref"):
            refs.add(b["ref"])
        for it in b.get("items", []) if isinstance(b.get("items"), list) else []:
            if isinstance(it, dict) and it.get("ref"):
                refs.add(it["ref"])
        if b.get("type") == "switch":
            sw.append((b.get("ref"), len(b.get("views") or [])))
    return refs, sw


def check(d):
    err, warn = [], []
    if d.get("format") != "tkleen-deck":
        err.append('format은 "tkleen-deck"이어야 합니다')
    slides = d.get("slides") or []
    if not slides:
        err.append("slides가 비어 있습니다")
    ids = [s.get("id") for s in slides]
    if len(ids) != len(set(ids)) or None in ids:
        err.append("slide id가 비었거나 겹칩니다")
    idset, assets = set(ids), d.get("assets", {})
    byid = {s.get("id"): s for s in slides}
    srcs, asms = d.get("sources", {}), d.get("assumptions", {})
    th = d.get("theme") or {}
    if not isinstance(th, dict):
        err.append("theme은 객체여야 합니다")
        th = {}
    if th.get("style") not in (None,) + tuple(STYLES):
        err.append("theme.style은 tkleen · klein 가운데 하나여야 합니다")
    if th.get("accent") is not None and not re.match(r"^#[0-9A-Fa-f]{6}$", str(th.get("accent"))):
        err.append("theme.accent는 #RRGGBB 형식이어야 합니다")
    mo = d.get("motion") or {}
    if mo.get("level", "basic") not in MOTION_LEVELS:
        err.append("motion.level은 none · basic · rich 가운데 하나여야 합니다")
    if mo.get("stagger") not in (None, "off", "blocks", "items"):
        err.append("motion.stagger는 off · blocks · items 가운데 하나여야 합니다")
    for k in ("ring", "zoom", "grow"):
        if k in mo and not isinstance(mo[k], bool):
            err.append(f"motion.{k}는 true 또는 false여야 합니다")
    if mo.get("enter") not in (None,) + tuple(ENTERS):
        err.append("motion.enter는 fade · zoom · slide · rise · none 가운데 하나여야 합니다")
    if not ok_speed(mo.get("speed")):
        err.append("motion.speed는 slow · normal · fast 또는 0.5–3 숫자여야 합니다")
    for i, s in enumerate(slides, 1):
        lay = s.get("layout")
        if lay not in LAYOUTS:
            err.append(f"{i}쪽: layout '{lay}'을 알 수 없습니다")
        if lay == "page" and not (s.get("image") in assets or s.get("pdfPage")):
            err.append(f"{i}쪽: page 배치에 image(assets 키) 또는 pdfPage가 없습니다")
        for k in s.get("src", []):
            if k not in srcs:
                warn.append(f"{i}쪽: sources에 '{k}'가 없습니다")
        for k in s.get("asm", []):
            if k not in asms:
                warn.append(f"{i}쪽: assumptions에 '{k}'가 없습니다")
        sm = s.get("motion")
        if sm is not None:
            if not isinstance(sm, dict):
                err.append(f"{i}쪽: motion은 객체여야 합니다")
            else:
                for k in sm:
                    if k not in SLIDE_MOTION_KEYS:
                        err.append(f"{i}쪽: 쪽 motion에 '{k}'는 쓸 수 없습니다 (단계 level은 덱 전체에서만)")
                if sm.get("enter") not in (None,) + tuple(ENTERS):
                    err.append(f"{i}쪽: motion.enter 값이 잘못되었습니다")
                if not ok_speed(sm.get("speed")):
                    err.append(f"{i}쪽: motion.speed 값이 잘못되었습니다")
                if sm.get("stagger") not in (None, "off", "blocks", "items"):
                    err.append(f"{i}쪽: motion.stagger 값이 잘못되었습니다")
        # v1.6.0 쪽 하단 안내문
        fn = s.get("footnote")
        if fn is not None:
            if not isinstance(fn, str):
                err.append(f"{i}쪽: footnote는 문자열입니다")
            elif lay != "content":
                warn.append(f"{i}쪽: footnote는 content 배치에서만 보입니다")
            elif len(fn) > 160:
                warn.append(f"{i}쪽: footnote가 깁니다 ({len(fn)}자). 두 줄(약 160자)을 넘으면 잘립니다")
        # v1.5.0 자유 배치·배율
        if s.get("free") and lay != "content":
            warn.append(f"{i}쪽: free(자유 배치)는 content 배치에서만 쓰입니다")
        if s.get("free") is True and lay == "content":
            for b in s.get("blocks") or []:
                if b.get("box") is None:
                    warn.append(f"{i}쪽 {b.get('type')} 블록: 자유 배치 쪽인데 box가 없어 자동 정렬로 위쪽에 놓입니다")
        myrefs = []
        for b in walk_blocks(s.get("blocks")):
            bt = b.get("type")
            check_fx(b, f"{i}쪽 {bt} 블록", bt, err, warn)
            if b.get("ref"):
                myrefs.append(b["ref"])
            for it in b.get("items", []) if isinstance(b.get("items"), list) else []:
                if isinstance(it, dict):
                    check_fx(it, f"{i}쪽 {bt} 항목", bt, err, warn)
                    if it.get("ref"):
                        myrefs.append(it["ref"])
            if b.get("reveal") is not None and not isinstance(b.get("reveal"), (bool, str)):
                err.append(f"{i}쪽: reveal은 true 또는 단추 글자(문자열)입니다")
            bx = b.get("box")
            if bx is not None and not any(b is t for t in s.get("blocks") or []):
                warn.append(f"{i}쪽 {bt} 블록: row·switch 안쪽 블록의 box는 엔진이 쓰지 않습니다 (최상위 블록에만)")
            if bx is not None:
                v = [bx.get(k) for k in "xywh"] if isinstance(bx, dict) else []
                if not (len(v) == 4 and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in v)
                        and v[0] >= 0 and v[1] >= 0 and v[2] >= 2 and v[3] >= 2
                        and v[0] + v[2] <= 100.5 and v[1] + v[3] <= 100.5):
                    err.append(f"{i}쪽 {bt} 블록: box는 {{x, y, w, h}} 수(%)이고 0≤x·y, w·h≥2이고 x+w와 y+h가 100을 넘지 않아야 합니다")
            sc = b.get("scale")
            if sc is not None and not (isinstance(sc, (int, float)) and not isinstance(sc, bool) and 0.6 <= sc <= 1.6):
                err.append(f"{i}쪽 {bt} 블록: scale은 0.6–1.6 사이의 수입니다")
            if b.get("type") not in BLOCKS:
                err.append(f"{i}쪽: 블록 종류 '{b.get('type')}'을 알 수 없습니다")
            golist = [it for it in (b.get("items", []) if isinstance(b.get("items"), list) else []) if isinstance(it, dict) and it.get("go")]
            for it in golist:
                if it["go"] not in idset:
                    warn.append(f"{i}쪽: 이동 대상 '{it['go']}'이 없습니다")
                    continue
                trefs, tsw = refs_of(byid[it["go"]])
                gr, gv = it.get("goRef"), it.get("goView")
                if gr and gr not in trefs:
                    warn.append(f"{i}쪽: goRef '{gr}'가 이동 대상 쪽 '{it['go']}'에 없습니다")
                if gv:
                    cand = [n for r, n in tsw if r == gr] or [n for r, n in tsw]
                    if not cand:
                        warn.append(f"{i}쪽: goView를 주었으나 이동 대상 쪽 '{it['go']}'에 switch가 없습니다")
                    elif isinstance(gv, int) and gv > cand[0]:
                        warn.append(f"{i}쪽: goView {gv}가 대상 switch의 보기 수({cand[0]})보다 큽니다")
            if b.get("type") == "image" and b.get("src") not in assets and not os.path.exists(str(b.get("src"))):
                warn.append(f"{i}쪽: 그림 '{b.get('src')}'을 찾을 수 없습니다")
            if b.get("type") == "row" and b.get("panel") is not None:
                pn = b["panel"] if isinstance(b["panel"], list) else [b["panel"]]
                if not all(isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= len(b.get("cols") or []) for p in pn):
                    err.append(f"{i}쪽: row.panel은 1부터 열 수까지의 정수(또는 그 배열)입니다")
            if b.get("type") == "bars":
                its = b.get("items") or []
                if not 2 <= len(its) <= 8:
                    warn.append(f"{i}쪽: bars 항목은 2–8개가 알맞습니다 (지금 {len(its)}개)")
                for it in its:
                    try:
                        float(str(it.get("value")).replace(",", ""))
                    except ValueError:
                        err.append(f"{i}쪽: bars 항목 '{it.get('label')}'의 value가 숫자가 아닙니다")
            if b.get("type") in ("stack", "grid"):
                its = b.get("items") or []
                lo, hi = (2, 6) if b["type"] == "stack" else (2, 5)
                if not lo <= len(its) <= hi:
                    (err if len(its) < lo else warn).append(f"{i}쪽: {b['type']} 항목은 {lo}–{hi}개입니다 (지금 {len(its)}개{', 넘는 항목은 그리지 않음' if len(its) > hi else ''})")
                vals = [_num(it.get("value")) if isinstance(it, dict) else None for it in its]
                if any(v is None or v < 0 for v in vals):
                    err.append(f"{i}쪽: {b['type']} 항목의 value는 0 이상의 수입니다")
                elif not sum(vals) > 0:
                    err.append(f"{i}쪽: {b['type']} 항목 value의 합계가 0입니다")
                if b["type"] == "stack" and b.get("share") is not None and not (isinstance(b["share"], int) and 1 <= b["share"] <= len(its)):
                    err.append(f"{i}쪽: stack.share는 1부터 항목 수까지의 정수입니다")
                if b["type"] == "stack" and b.get("parts") is False and not b.get("share"):
                    warn.append(f"{i}쪽: stack에서 parts: false이면 share가 있어야 그림이 보입니다")
            if b.get("type") == "sim":
                check_sim(b, f"{i}쪽 sim 블록", err)
                if b.get("note") or b.get("noteRows"):
                    warn.append(f"{i}쪽: sim 블록에 note를 붙이면 레버를 누를 때마다 설명 창이 열립니다. 옆에 text 블록으로 두세요")
            if b.get("type") == "math":
                its = b.get("items") or []
                if not 1 <= len(its) <= 6:
                    warn.append(f"{i}쪽: math 블록의 수식은 1–6줄이 알맞습니다 (지금 {len(its)}줄)")
                for it in its:
                    mm = it.get("mathml") or ""
                    if not mm and not it.get("tex"):
                        err.append(f"{i}쪽: math 항목에 mathml 또는 tex가 없습니다")
                    if mm and "<math" not in mm:
                        err.append(f"{i}쪽: mathml은 <math>…</math> 형태여야 합니다")
                    if MATH_BAD.search(mm):
                        err.append(f"{i}쪽: mathml에 허용하지 않는 태그나 속성이 있습니다")
            if b.get("type") == "switch":
                vs = b.get("views") or []
                if not 2 <= len(vs) <= 3:
                    warn.append(f"{i}쪽: switch 보기는 2–3개가 알맞습니다 (지금 {len(vs)}개)")
                if s.get("steps"):
                    warn.append(f"{i}쪽: switch가 있는 쪽에는 steps를 켜지 않습니다")
        dup = {r for r in myrefs if myrefs.count(r) > 1}
        if dup:
            warn.append(f"{i}쪽: ref {sorted(dup)}가 한 쪽 안에서 겹칩니다 (첫 번째만 강조됨)")
        for t in text_fields({k: v for k, v in s.items() if k != "blocks"} | {"blocks": s.get("blocks", [])}):
            if "<math" in t and t.count("<math") != t.count("</math>"):
                err.append(f"{i}쪽: 글 안의 <math>와 </math> 개수가 맞지 않습니다")
            if "<math" in t and MATH_BAD.search(t):
                err.append(f"{i}쪽: 글 안의 수식에 허용하지 않는 태그나 속성이 있습니다")
        if "<math" in str(s.get("title", "")):
            warn.append(f"{i}쪽: 제목에는 MathML을 넣지 않습니다 (쪽 목록·서랍에 태그가 글자로 보임)")
        if lay == "content" and len(s.get("blocks", [])) == 0:
            warn.append(f"{i}쪽: 본문 블록이 없습니다")
    return err, warn


def build(template, data_path, out, pdf=None):
    d = json.load(open(data_path, encoding="utf-8"))
    d.setdefault("assets", {})
    for s in d.get("slides", []):
        if s.get("layout") == "page" and s.get("pdfPage") and s.get("image") not in d["assets"]:
            if not pdf:
                sys.exit(f"{s.get('id')}: pdfPage가 있으나 --pdf가 없습니다")
            key = s.get("image") or f"p{int(s['pdfPage']):03d}"
            d["assets"][key], s["ratio"] = page_image(pdf, int(s["pdfPage"]))
            s["image"] = key
        for b in walk_blocks(s.get("blocks")):
            if b.get("type") == "math":
                for it in b.get("items") or []:
                    if it.get("tex") and not it.get("mathml"):
                        it["mathml"] = tex_to_mathml(it["tex"])
            if b.get("type") == "image" and b.get("src") not in d["assets"] and os.path.isfile(str(b.get("src"))):
                key = "img-" + re.sub(r"\W+", "-", os.path.splitext(os.path.basename(b["src"]))[0])
                d["assets"][key] = file_image(b["src"])
                b["src"] = key
    err, warn = check(d)
    for w in warn:
        print("주의:", w)
    if err:
        sys.exit("오류:\n  " + "\n  ".join(err))
    html = open(template, encoding="utf-8").read()
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    html, n = DATA_RE.subn(lambda m: m.group(1) + "\n" + payload + "\n" + m.group(3), html, count=1)
    if n != 1:
        sys.exit("템플릿에서 deck-data 영역을 찾지 못했습니다")
    title = (d.get("meta") or {}).get("title") or "슬라이드"
    html = re.sub(r"<title>.*?</title>", "<title>" + title.replace("<", "&lt;") + "</title>", html, count=1)
    open(out, "w", encoding="utf-8").write(html)
    style = (d.get("theme") or {}).get("style") or "tkleen"
    print(f"완료: {out} · {len(d['slides'])}쪽 · 스타일 {style} · {os.path.getsize(out) // 1024}KB")


def extract(deck, out):
    m = DATA_RE.search(open(deck, encoding="utf-8").read())
    if not m:
        sys.exit("이 파일에는 deck-data가 없습니다. 이 형식으로 만든 덱이 아닙니다.")
    d = json.loads(m.group(2).replace("<\\/", "</"))
    json.dump(d, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"추출: {out} · {len(d.get('slides', []))}쪽")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "build":
        pdf = a[a.index("--pdf") + 1] if "--pdf" in a else None
        build(a[1], a[2], a[3], pdf)
    elif a[0] == "extract":
        extract(a[1], a[2])
    elif a[0] == "check":
        e, w = check(json.load(open(a[1], encoding="utf-8")))
        print("\n".join(["오류: " + x for x in e] + ["주의: " + x for x in w]) or "이상 없음")
    else:
        sys.exit(__doc__)
