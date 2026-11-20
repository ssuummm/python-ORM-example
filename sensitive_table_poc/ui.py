"""로컬 실행용 Gradio UI: 1 키워드 관리 → 2 프롬프트 편집 → 3 실행 · 결과.

python app.py 를 실행하면 브라우저에 http://127.0.0.1:7860 이 열린다.
파이프라인 자체는 app.run()을 그대로 호출하고, 이 파일은 화면과 파일 저장만 맡는다.
"""
from __future__ import annotations

import difflib
import html
import logging
import shutil
import threading
import time
from collections import Counter, deque
from datetime import datetime

import gradio as gr
import pandas as pd

import app
from src.loader import load_tables, read_txt
from src.prompt import PROMPT_DIR

KEYWORDS_FILE = app.KEYWORDS_FILE
PROMPT_FILES = {"system": PROMPT_DIR / "system_prompt.md", "tag": PROMPT_DIR / "tag_prompt.md"}
PROMPT_TITLES = {"system": "민감 판정 프롬프트", "tag": "고위험 태그 프롬프트"}
MODELS = ["HCP-LLM-Latest", "HCP-Medium-Latest", "HCP-Small-Latest"]   # 첫 번째가 기본값
REASONING_EFFORTS = ["low", "medium", "high", "xhigh"]
DEFAULT_EFFORT = "high"
TAG_COLORS = {"재무성과": "#d8434b", "영업기밀": "#e98b3a", "사업계획": "#2f5bea",
              "기술노하우": "#7a5af0", "전략제품": "#1f9d6b"}

# 화면 전체에서 쓰는 글꼴 하나 (표·로그·편집기 포함). 앞에서부터 설치된 글꼴을 쓴다.
FONTS = ["Noto Sans KR", "Malgun Gothic", "Apple SD Gothic Neo", "sans-serif"]
FONT_CSS = ", ".join(f'"{f}"' if " " in f else f for f in FONTS)

CSS = """
.gradio-container{max-width:none!important;padding-left:32px!important;padding-right:32px!important}   /* 화면 너비에 맞춰 늘어남 */
/* 글꼴 통일: Gradio 기본 글꼴(IBM Plex 등)·고정폭 글꼴 대신 FONTS 하나만 쓴다 */
.gradio-container, .gradio-container *:not(svg):not(path), .cm-editor, .cm-content{font-family:__FONT__!important}
/* 상단 탭 = 진행 단계 */
/* 상단 단계 표시: 화면 너비를 3등분한 STEP 1 → 2 → 3 */
#steps > .tab-wrapper{border-bottom:none!important;height:auto!important;margin:4px 0 18px;padding:0}
#steps > .tab-wrapper > [role=tablist]{display:flex;gap:14px;width:100%;height:auto}
#steps > .tab-wrapper [role=tab]{flex:1;display:flex;flex-direction:column;align-items:flex-start;gap:2px;height:auto!important;
  padding:10px 4px 18px!important;margin:0!important;border:none!important;border-top:3px solid #dfe3ea!important;
  border-radius:0!important;background:transparent!important;color:#8a93a5!important;font-size:15px;font-weight:700;text-align:left}
#steps > .tab-wrapper [role=tab]::before{font-size:11px;font-weight:700;letter-spacing:.06em;color:#a3abb9}
#steps > .tab-wrapper [role=tab]::after{display:none!important}
#steps > .tab-wrapper [role=tab][data-tab-id=keyword]::before{content:"STEP 1"}
#steps > .tab-wrapper [role=tab][data-tab-id=prompt]::before{content:"STEP 2"}
#steps > .tab-wrapper [role=tab][data-tab-id=run]::before{content:"STEP 3"}
#steps > .tab-wrapper [role=tab]:hover{color:#4b5565!important;border-top-color:#c3c9d6!important}
#steps > .tab-wrapper [role=tab][aria-selected=true]{color:#1b2333!important;border-top-color:#4f46e5!important}
#steps > .tab-wrapper [role=tab][aria-selected=true]::before{color:#4f46e5}
/* 요약 카드 */
.cards{display:flex;gap:12px}
.card{flex:1;background:#fff;border:1px solid #e3e7ef;border-radius:10px;padding:12px 14px}
.card .l{color:#5b6577;font-size:12px}.card .v{font-size:24px;font-weight:800}.card .s{color:#8a93a5;font-size:11px}
.card.amb{border-left:4px solid #c98111}
/* 하단 고정 작업 바 */
.actionbar{position:sticky;bottom:0;z-index:20;background:#fff;border:1px solid #e3e7ef;border-radius:10px;
           padding:8px 12px;box-shadow:0 4px 14px rgba(20,30,60,.10);align-items:center}
/* 결과 KPI 카드 버튼 */
.kpi button, button.kpi{white-space:pre-line;text-align:left;justify-content:flex-start;font-size:13px;line-height:1.4;
        min-height:64px;font-size:14px;border-radius:10px;background:#fff;color:#1b2333;border:1px solid #e3e7ef}
button.kpi::first-line{color:#5b6577}
button.kpi-red{border-left:4px solid #d8434b} button.kpi-grn{border-left:4px solid #1f9d6b}
button.kpi-amb{border-left:4px solid #c98111} button.kpi-gry{border-left:4px solid #6b7280}
button.kpi.primary{box-shadow:0 0 0 2px #2f5bea;background:#e9efff;color:#1b2333}
.muted{color:#8a93a5;font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}
/* 공통 정리 */
.apptitle{font-size:24px;font-weight:800;color:#1b2333;padding:8px 0 4px;letter-spacing:-.01em}
/* 프롬프트 목차: 박스 없이 왼쪽 선 하나로 된 목록 */
.outline{background:transparent!important;border:none!important;padding:4px 0!important}
.outline .wrap{flex-direction:column!important;gap:0!important}
.outline label{background:transparent!important;border:none!important;box-shadow:none!important;border-radius:0!important;
  border-left:2px solid #e6e9f0!important;padding:7px 12px!important;margin:0!important;color:#5b6577;font-size:13px;width:100%}
.outline label:hover{color:#1b2333;background:#f1f3f8!important}
.outline label input{display:none!important}
.outline label.selected{border-left:2px solid #4f46e5!important;color:#4338ca!important;font-weight:700;background:#eef0ff!important}
.outline label.selected span{color:#4338ca!important}
/* 세그먼트 버튼 (편집/변경 비교, 추론 정도) */
.seg .wrap{display:inline-flex!important;gap:0!important;border:1px solid #dfe3ea;border-radius:8px;overflow:hidden;background:#fff;flex-wrap:nowrap!important}
.seg label{border:none!important;border-radius:0!important;box-shadow:none!important;background:#fff!important;margin:0!important;padding:3px 10px!important;min-height:0!important;
  border-right:1px solid #dfe3ea!important;color:#5b6577}
.seg label:last-child{border-right:none!important}
.seg label input{display:none!important}
.seg label.selected{background:#eef0ff!important;color:#4338ca!important;font-weight:700}
.seg label.selected span{color:#4338ca!important}
/* 칩 버튼 (시스템, 스키마) */
.chips .wrap{gap:5px!important}
.chips label{border:1px solid #dfe3ea!important;border-radius:999px!important;box-shadow:none!important;background:#fff!important;padding:2px 10px!important;min-height:0!important;color:#5b6577}
.chips label input{display:none!important}
.chips label.selected{background:#4f46e5!important;border-color:#4f46e5!important}
.chips label.selected span{color:#fff!important;font-weight:700}
/* 칩·세그먼트 버튼 글자: 본문보다 한 단계 작게 */
.chips label span, .seg label span{font-size:12.5px!important;line-height:1.5!important}
.chips label, .seg label{gap:0!important;justify-content:center}
.chips label span, .seg label span{margin:0!important;padding:0!important}

button.secondary{border:1px solid #dfe3ea!important;background:#fff!important}
button.secondary:hover{background:#f3f5fa!important}
[role=gridcell], [role=gridcell] *, th, th *{font-size:13px!important}
th{background:#f7f8fb!important;color:#4b5565!important}
/* 키워드 표: '행 추가'를 표 머리줄(복사·확대 아이콘 왼쪽)에 겹쳐 둔다 */
#kw-tablebar{justify-content:flex-end;margin-bottom:-44px;padding-right:72px;position:relative;z-index:5;
  background:transparent!important;border:none!important;pointer-events:none}
#kw-tablebar button{pointer-events:auto}
.section-title{font-size:20px;font-weight:800;color:#1b2333;padding:4px 2px}
#close-detail{align-self:flex-end}
/* 실행 로그 */
#log textarea{font-size:12px!important;line-height:1.6!important;background:#fafbfd!important}
"""


THEME = gr.themes.Soft(primary_hue="indigo", neutral_hue="slate", radius_size="md",
                       font=[gr.themes.Font(f) for f in FONTS]).set(
    body_background_fill="#f6f7fb",
    block_shadow="none", block_border_color="#e6e9f0", block_radius="12px",
    block_label_background_fill="transparent", block_label_text_color="#6b7486",
    block_label_text_weight="600", block_label_text_size="13px", block_label_padding="0 0 4px 0",
    block_title_background_fill="transparent", block_title_text_color="#6b7486",
    block_title_text_weight="600", block_title_padding="0 0 4px 0",
    button_primary_shadow="none", button_primary_shadow_hover="none", button_secondary_shadow="none",
    button_secondary_shadow_hover="none",
    button_secondary_background_fill="#ffffff", button_secondary_background_fill_hover="#f3f5fa",
    button_secondary_border_color="#dfe3ea", input_shadow="none", checkbox_label_shadow="none",
)


def now() -> str:
    return datetime.now().strftime("%m%d_%H%M%S")


def backup(path) -> None:
    """저장 전에 원본을 backup/ 폴더에 복사해 둔다."""
    if path.exists():
        folder = path.parent / "backup"
        folder.mkdir(exist_ok=True)
        shutil.copy2(path, folder / f"{path.stem}_{now()}{path.suffix}")


def cards_html(items) -> str:
    """[(제목, 값, 설명, 강조여부)] → 요약 카드 HTML."""
    return '<div class="cards">' + "".join(
        f'<div class="card{" amb" if hl else ""}"><div class="l">{t}</div><div class="v">{v}</div>'
        f'<div class="s">{s}</div></div>' for t, v, s, hl in items) + "</div>"


# =====================================================================
# 실행 상태 (실행은 백그라운드 스레드, 화면은 1초마다 이 값을 읽어 갱신)
# =====================================================================
class RunState:
    def __init__(self):
        self.running = False
        self.stage = "대기"
        self.total = 0
        self.counts = Counter()
        self.logs = deque(maxlen=300)
        self.started = 0.0
        self.result_path = None
        self.table_info = {}          # table_id → TableMeta (한글명 · 원천 · 잠금여부, 화면 표시용)
        self.stop_event = threading.Event()

    def reset(self, total: int):
        self.__init__()
        self.running, self.total, self.started, self.stage = True, total, time.time(), "판정"

    def add(self, result):                     # app.run()이 테이블 1건마다 호출
        self.counts["done"] += 1
        self.counts[result.decision] += 1
        self.counts["needs_review"] += result.needs_review
        if self.counts["done"] == self.total:
            self.stage = "엑셀 저장"


STATE = RunState()


class UILogHandler(logging.Handler):
    """파이프라인 로그를 화면 로그 창으로 보낸다."""
    def emit(self, record):
        STATE.logs.append(self.format(record))


# =====================================================================
# 1 키워드 관리
#   - kw_full : 편집 중인 전체 표 (No 열로 행을 구분)
#   - kw_orig : 마지막으로 저장된 표 (변경 여부 비교용)
#   - 화면 표 : kw_full을 시스템·검색어로 거른 것. 편집하면 No 기준으로 kw_full에 합친다
# =====================================================================
KW_COLS = ["sys_cd", "keyword", "description", "equivalent_terms", "high_risk_tag", "source", "source_detail", "history"]
ROW_COLORS = {"신규": "#e6f6ef", "수정": "#fdf3e1"}


def read_keywords() -> pd.DataFrame:
    df = read_txt(KEYWORDS_FILE).fillna("").astype(str)
    for c in KW_COLS:
        if c not in df:
            df[c] = ""
    df = df[KW_COLS]
    df.insert(0, "No", [str(i) for i in range(1, len(df) + 1)])
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """앞뒤 공백 제거 + 완전히 빈 행(행 추가 후 비워 둔 행)은 버린다."""
    df = df[KW_COLS].fillna("").astype(str).apply(lambda col: col.str.strip())
    return df[(df != "").any(axis=1)]


def change_status(full: pd.DataFrame, orig: pd.DataFrame) -> pd.Series:
    """행마다 신규 / 수정 / 유지."""
    o = orig.set_index("No")[KW_COLS]
    f = full.set_index("No")[KW_COLS]
    is_new = ~f.index.isin(o.index)
    common = f[~is_new]
    changed = (common != o.loc[common.index]).any(axis=1)
    status = pd.Series("유지", index=f.index)
    status[is_new] = "신규"
    status[changed[changed].index] = "수정"
    return status.values


def find_problems(full: pd.DataFrame) -> pd.DataFrame:
    """저장을 막는 문제를 칸 단위로 찾는다. (No, 열, 사유) 표를 돌려준다."""
    df = full.copy()
    df[KW_COLS] = df[KW_COLS].apply(lambda col: col.str.strip())
    filled = df[(df[KW_COLS] != "").any(axis=1)]              # 완전히 빈 행은 저장 시 버리므로 제외
    rows = []
    for col in ("sys_cd", "keyword"):
        for no in filled.loc[filled[col] == "", "No"]:
            rows.append((no, col, f"{col} 비어 있음"))
    active = filled[(filled["history"] != "삭제") & (filled["keyword"] != "")]
    dup = active[active.duplicated(["sys_cd", "keyword"], keep=False)]
    for no, sys_cd, kw in dup[["No", "sys_cd", "keyword"]].itertuples(index=False):
        rows.append((no, "keyword", f"{sys_cd}에 '{kw}' 중복"))
    return pd.DataFrame(rows, columns=["No", "col", "msg"])


def kw_view(full, orig, sys_sel, query):
    """화면에 보여 줄 표(색 포함). 신규 행은 검색어와 무관하게 항상 보여 준다.
    변경 상태(신규/수정/유지)는 표에 열로 넣지 않고 행 색으로만 보여 준다."""
    df = full.copy()
    status = pd.Series(change_status(full, orig), index=df.index)
    if sys_sel and sys_sel != "전체":
        df = df[(df["sys_cd"] == sys_sel) | (status == "신규")]
    if query:
        hit = df[KW_COLS].apply(lambda col: col.str.contains(query, case=False, regex=False)).any(axis=1)
        df = df[hit | (status[df.index] == "신규")]
    status = dict(zip(df["No"], status[df.index]))          # No → 변경 상태 (행 색칠용)
    df = df.reset_index(drop=True)

    problems = find_problems(full)
    bad = problems.groupby(["No", "col"]).size().index     # 문제 있는 (No, 열) 목록

    def paint(row):
        base = ROW_COLORS.get(status[row["No"]], "")
        if row["history"] == "삭제":
            base = "#f3f4f6;color:#9aa1ad"
        return [("background-color:#fdecec;color:#d8434b;font-weight:700" if (row["No"], c) in bad
                 else f"background-color:{base}" if base else "") for c in row.index]

    return df.style.apply(paint, axis=1)


def kw_summary(full, orig):
    """요약 카드(시스템 / 적용 중 키워드 / 유사어 포함 매칭 단어 / 저장 안 된 변경) + 하단 바 문구 + 문제 목록."""
    active = full[(full["history"] != "삭제") & (full["keyword"].str.strip() != "")]
    terms = active["equivalent_terms"].str.split("|").explode().str.strip()
    words = len(active) + int((terms != "").sum())
    systems = sorted(active["sys_cd"].unique())
    status = Counter(change_status(full, orig))
    removed = len(set(orig["No"]) - set(full["No"]))
    n_change = status["신규"] + status["수정"] + removed
    sys_names = " · ".join("공통(*)" if s == "*" else s for s in systems[:5]) + (" …" if len(systems) > 5 else "")
    cards = cards_html([
        ("시스템 (sys_cd)", len(systems), sys_names, False),
        ("적용 중 키워드", len(active), "history가 '삭제'인 행 제외", False),
        ("유사어 포함 매칭 단어", words, "keyword + equivalent_terms", False),
        ("저장 안 된 변경", n_change, f"신규 {status['신규']} · 수정 {status['수정']}", n_change > 0),
    ])
    bar = (f"<span class='badge' style='background:#fdf3e1;color:#c98111'>변경 {n_change}건</span> "
           f"신규 {status['신규']} · 수정 {status['수정']} — 저장하면 이전 파일은 data/backup/에 보관됩니다"
           if n_change else "<span class='muted'>저장 안 된 변경 없음</span>")
    problems = find_problems(full)
    msg = ""
    if len(problems):
        msg = "⚠ **저장 전에 고쳐야 할 칸 " + str(len(problems)) + "개** (빨간 칸): " + \
              " · ".join(f"No {no} {m}" for no, m in problems[["No", "msg"]].head(6).itertuples(index=False))
    return cards, bar, msg


def sys_choices(full):
    """시스템 선택 버튼: '전체 (128)', '공통(*) (41)', 'GSCM (23)' …"""
    active = full[(full["history"] != "삭제") & (full["keyword"].str.strip() != "")]
    counts = active["sys_cd"].value_counts()
    return [(f"전체 ({len(active)})", "전체")] + [
        (f"{'공통(*)' if s == '*' else s} ({n})", s) for s, n in sorted(counts.items()) if s]


def kw_refresh(full, orig, sys_sel, query):
    """표·요약·시스템 버튼을 한 번에 다시 그린다."""
    cards, bar, msg = kw_summary(full, orig)
    values = [v for _, v in sys_choices(full)]
    sel = sys_sel if sys_sel in values else "전체"
    return (full, kw_view(full, orig, sel, query), cards, bar, msg,
            gr.Radio(choices=sys_choices(full), value=sel))


def kw_on_edit(edited: pd.DataFrame, full, orig, sys_sel, query):
    """화면 표를 고치면 No 기준으로 전체 표에 반영한다."""
    edited = edited.fillna("").astype(str)
    merged = full.set_index("No")
    merged.update(edited.set_index("No")[KW_COLS])
    return kw_refresh(merged.reset_index(), orig, sys_sel, query)


def kw_add_row(full, orig, sys_sel, query):
    new_no = str(int(full["No"].astype(int).max() if len(full) else 0) + 1)
    row = {c: "" for c in KW_COLS} | {"No": new_no, "sys_cd": "" if sys_sel == "전체" else sys_sel}
    full = pd.concat([full, pd.DataFrame([row])], ignore_index=True)
    gr.Info(f"No {new_no} 행을 추가했습니다. 표 아래쪽(초록색 행)에서 입력하세요.")
    return kw_refresh(full, orig, sys_sel, query)


def kw_save(full, orig, sys_sel, query):
    if STATE.running:
        raise gr.Error("실행 중에는 저장할 수 없습니다. 실행이 끝난 뒤 저장해 주세요.")
    problems = find_problems(full)
    if len(problems):
        raise gr.Error(f"빨간 칸 {len(problems)}개를 먼저 고쳐 주세요.")
    df = clean(full)
    bad_sep = df[df["equivalent_terms"].str.contains(r"[/,]")]
    if len(bad_sep):
        gr.Warning("equivalent_terms에 '/' 또는 ','가 있습니다. 여러 표현은 '|'로 구분해야 합니다: "
                   + ", ".join(bad_sep["keyword"].head(5)))
    if df.reset_index(drop=True).equals(clean(read_keywords()).reset_index(drop=True)):
        gr.Info("변경된 내용이 없어 저장하지 않았습니다.")
        return kw_refresh(full, orig, sys_sel, query) + (orig,)
    backup(KEYWORDS_FILE)
    df.to_csv(KEYWORDS_FILE, sep="\t", index=False, encoding="utf-8-sig")
    gr.Info(f"keywords.txt 저장 완료 ({len(df)}행, 이전 파일은 data/backup/에 보관)")
    saved = read_keywords()
    return kw_refresh(saved, saved, sys_sel, query) + (saved,)


def kw_revert(sys_sel, query):
    saved = read_keywords()
    return kw_refresh(saved, saved, sys_sel, query) + (saved,)


def kw_next(full, orig):
    n = sum(s != "유지" for s in change_status(full, orig)) + len(set(orig["No"]) - set(full["No"]))
    if n:
        gr.Warning(f"저장하지 않은 변경 {n}건이 있습니다. 저장해야 실행에 반영됩니다.")
    return gr.Tabs(selected="prompt")


# =====================================================================
# 2 프롬프트 편집
#   - 목차: '# 제목' 줄로 구역을 나눈다. 구역을 고르면 그 구역만 편집창에 띄운다
#   - 변경 비교: 같은 편집 칸에서 저장본(또는 고른 이전 버전) 대비 바뀐 줄을 색으로 보여 준다
# =====================================================================
def read_prompt(key: str) -> str:
    return PROMPT_FILES[key].read_text(encoding="utf-8")


def split_sections(text: str) -> list[tuple[str, int, int]]:
    """[(제목, 시작 줄, 끝 줄)] — '# '로 시작하는 줄마다 새 구역."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("# ")]
    if not starts or starts[0] != 0:
        starts = [0] + starts
    ends = starts[1:] + [len(lines)]
    return [(lines[s][2:].strip() if lines[s].startswith("# ") else "(머리말)", s, e) for s, e in zip(starts, ends)]


def outline_choices(text: str):
    return [("전체 보기", -1)] + [(title, i) for i, (title, _, _) in enumerate(split_sections(text))]


def section_text(text: str, idx: int) -> str:
    if idx is None or idx < 0:
        return text
    secs = split_sections(text)
    if idx >= len(secs):
        return text
    _, s, e = secs[idx]
    return "\n".join(text.split("\n")[s:e])


def replace_section(text: str, idx: int, new_part: str) -> str:
    if idx is None or idx < 0:
        return new_part
    lines = text.split("\n")
    _, s, e = split_sections(text)[idx]
    return "\n".join(lines[:s] + new_part.split("\n") + lines[e:])


def diff_html(old: str, new: str, title: str) -> str:
    """바뀐 줄만(앞뒤 2줄 포함) 초록=추가, 빨강=삭제로 보여 준다."""
    rows = []
    for ln in difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=2):
        if ln.startswith(("---", "+++")):
            continue
        t = html.escape(ln[1:]) or "&nbsp;"
        if ln.startswith("@@"):
            rows.append("<div style='color:#8a93a5;margin-top:8px'>⋯</div>")
        elif ln.startswith("+"):
            rows.append(f"<div style='background:#e3f6ec;color:#1f6b4b'>+ {t}</div>")
        elif ln.startswith("-"):
            rows.append(f"<div style='background:#fdecec;color:#b4323a;text-decoration:line-through'>- {t}</div>")
        else:
            rows.append(f"<div style='color:#5b6577'>&nbsp; {t}</div>")
    body = "".join(rows) or "<div class='muted'>바뀐 줄이 없습니다.</div>"
    return (f"<div class='muted' style='margin-bottom:6px'>{title}</div>"
            f"<div style='font-size:12px;line-height:1.7;border:1px solid #e3e7ef;"
            f"border-radius:8px;padding:10px 12px;max-height:620px;overflow:auto;white-space:pre-wrap'>{body}</div>")


def prompt_info(key: str, text: str) -> str:
    changed = text != read_prompt(key)
    mark = "<span class='badge' style='background:#fdf3e1;color:#c98111'>수정됨 · 저장 안 됨</span>" if changed \
        else "<span class='badge' style='background:#e6f6ef;color:#1f9d6b'>저장본과 같음</span>"
    return f"{mark} <span class='muted'>{PROMPT_FILES[key].name} · {len(text):,}자</span>"


def version_choices(key: str):
    """prompts/backup/ 의 이전 버전 목록 (최신순)."""
    p = PROMPT_FILES[key]
    files = sorted((p.parent / "backup").glob(f"{p.stem}_*{p.suffix}"), reverse=True)
    out = []
    for f in files:
        stamp = f.stem.rsplit("_", 2)[-2:]                       # ['1118', '164701']
        label = f"{stamp[0][:2]}/{stamp[0][2:]} {stamp[1][:2]}:{stamp[1][2:4]}:{stamp[1][4:]}"
        out.append((label, str(f)))
    return out


def prompt_bar(sys_text: str, tag_text: str) -> str:
    changed = [PROMPT_TITLES[k] for k, t in (("system", sys_text), ("tag", tag_text)) if t != read_prompt(k)]
    if not changed:
        return "<span class='muted'>저장 안 된 변경 없음</span>"
    return ("<span class='badge' style='background:#fdf3e1;color:#c98111'>저장 안 된 변경</span> "
            + ", ".join(changed) + " — 저장하면 이전 버전은 prompts/backup/에 보관됩니다")


def save_prompts(sys_text: str, tag_text: str):
    if STATE.running:
        raise gr.Error("실행 중에는 저장할 수 없습니다.")
    changed = []
    for key, text in (("system", sys_text), ("tag", tag_text)):
        if text == read_prompt(key):
            continue                                   # 변경 없음: 저장·백업 안 함
        backup(PROMPT_FILES[key])
        PROMPT_FILES[key].write_text(text, encoding="utf-8")
        changed.append(PROMPT_FILES[key].name)
    if changed:
        gr.Info(f"{', '.join(changed)} 저장 완료 (이전 버전은 prompts/backup/에 보관)")
    else:
        gr.Info("변경된 내용이 없어 저장하지 않았습니다.")
    return (prompt_bar(sys_text, tag_text), prompt_info("system", sys_text), prompt_info("tag", tag_text),
            gr.Dropdown(choices=version_choices("system"), value=None),
            gr.Dropdown(choices=version_choices("tag"), value=None))


def prompt_editor(key: str, text_state: gr.State):
    """프롬프트 1개의 편집 화면(목차 · 편집/비교 · 이전 버전)을 만든다."""
    text = read_prompt(key)
    with gr.Row():
        with gr.Column(scale=1, min_width=180):
            outline = gr.Radio(outline_choices(text), value=-1, label="목차", elem_classes="outline", container=False)
        with gr.Column(scale=4):
            with gr.Row():
                info = gr.HTML(prompt_info(key, text))
                mode = gr.Radio(["편집", "변경 비교"], value="편집", show_label=False, container=False, scale=0, min_width=200,
                                elem_classes="seg")
            editor = gr.Code(text, language="markdown", lines=26, max_lines=26, show_label=False, wrap_lines=True)
            diff = gr.HTML(visible=False)
            compare_to = gr.State("")                    # 비어 있으면 저장본과 비교
        with gr.Column(scale=1, min_width=230):
            version = gr.Dropdown(version_choices(key), value=None, label="이전 버전 (prompts/backup)")
            with gr.Row():
                cmp_btn = gr.Button("비교", size="sm")
                restore_btn = gr.Button("복원", size="sm")
            gr.Markdown("<span class='muted'>복원하면 편집 칸에 불러오기만 합니다. 저장해야 파일에 반영됩니다.</span>")

    def show(t, sel, m, target, part):
        t = replace_section(t, sel, part) if m == "변경 비교" else t   # 방금 입력한 내용까지 반영 후 비교
        base = target and open(target, encoding="utf-8").read()
        title = f"비교 대상: 이전 버전 {[l for l, v in version_choices(key) if v == target][0]}" if base \
            else "비교 대상: 저장된 파일"
        return (t, gr.Code(section_text(t, sel), visible=m == "편집"),
                gr.HTML(diff_html(base or read_prompt(key), t, title), visible=m == "변경 비교"))

    def on_edit(part, t, sel):
        t = replace_section(t, sel, part)
        return t, prompt_info(key, t), gr.Radio(choices=outline_choices(t), value=sel)

    def on_compare(v):
        if not v:
            raise gr.Error("비교할 이전 버전을 고르세요.")
        return v, "변경 비교"

    def on_restore(v):
        if not v:
            raise gr.Error("복원할 이전 버전을 고르세요.")
        t = open(v, encoding="utf-8").read()
        gr.Info("이전 버전을 편집 칸에 불러왔습니다. 저장해야 파일에 반영됩니다.")
        return t, "편집", gr.Radio(choices=outline_choices(t), value=-1), prompt_info(key, t), \
            gr.Code(t, visible=True), gr.HTML(visible=False), ""

    shown = [text_state, editor, diff]
    outline.input(show, [text_state, outline, mode, compare_to, editor], shown)
    mode.input(show, [text_state, outline, mode, compare_to, editor], shown)
    editor.input(on_edit, [editor, text_state, outline], [text_state, info, outline], trigger_mode="always_last")
    cmp_btn.click(on_compare, version, [compare_to, mode]).then(
        show, [text_state, outline, mode, compare_to, editor], shown)
    restore_btn.click(on_restore, version, [text_state, mode, outline, info, editor, diff, compare_to])
    return dict(outline=outline, mode=mode, editor=editor, diff=diff, info=info, version=version, compare_to=compare_to)


# =====================================================================
# 3 실행 · 결과
# =====================================================================
def schema_choices():
    out = []
    for s in app.SCHEMAS:
        p = app.tables_file(s)
        out.append((f"{s}" if p.exists() else f"{s} · 없음", s))
    return out


def schema_info(schema: str) -> str:
    """스키마 칩 위에 보이는 안내 문구."""
    p = app.tables_file(schema)
    if not p.exists():
        return f"{p.name} 파일이 없습니다"
    return f"테이블 수: {len(load_tables(p))}개"


def stage_html() -> str:
    """진행 단계(준비 → 판정 → 엑셀 저장 → 완료) + 진행 막대."""
    names = ["준비", "판정", "엑셀 저장", "완료"]
    cur = names.index(STATE.stage) if STATE.stage in names else -1
    chips = "".join(
        f"<div style='flex:1;text-align:center;padding:6px 2px;border-radius:6px;font-size:12px;font-weight:700;"
        f"background:{'#2f5bea' if i == cur else '#e6f6ef' if i < cur else '#eef0f3'};"
        f"color:{'#fff' if i == cur else '#1f9d6b' if i < cur else '#5b6577'}'>{n}</div>" for i, n in enumerate(names))
    done, total = STATE.counts["done"], max(STATE.total, 1)
    pct = 100 if STATE.stage == "완료" else int(done / total * 100)
    elapsed = time.time() - STATE.started if STATE.started else 0
    remain = elapsed / done * (STATE.total - done) if done else 0
    return (f"<div style='display:flex;gap:6px;margin-bottom:8px'>{chips}</div>"
            f"<div style='height:8px;background:#e8ecf3;border-radius:4px;overflow:hidden'>"
            f"<div style='height:100%;width:{pct}%;background:#2f5bea'></div></div>"
            f"<div class='muted' style='margin-top:6px'>{done} / {STATE.total} 테이블 · 경과 {app.format_elapsed(elapsed)}"
            f"{' · 남은 예상 ' + app.format_elapsed(remain) if STATE.running and done else ''}</div>")


def load_result(path) -> pd.DataFrame:
    return pd.read_excel(path, dtype=str).fillna("") if path else pd.DataFrame()


KPI = [("sensitive", "민감", "kpi-red"), ("non-sensitive", "비민감", "kpi-grn"),
       ("review", "검토 필요", "kpi-amb"), ("error", "에러", "kpi-gry")]


def kpi_buttons(counts: Counter, active: str | None):
    total = max(sum(counts[k] for k in ("sensitive", "non-sensitive", "error")), 1)
    out = []
    for key, name, _ in KPI:
        n = counts["needs_review" if key == "review" else key]
        mark = " ✓" if active == key else ""
        out.append(gr.Button(f"{name}{mark}\n{n}건  ({n * 100 // total}%)",
                             variant="primary" if active == key else "secondary"))
    return out


def result_counts(full: pd.DataFrame) -> Counter:
    if full.empty:
        return Counter()
    c = Counter(full["decision"])
    c["needs_review"] = int((full["needs_review"] == "True").sum())
    return c


def tag_html(full: pd.DataFrame) -> str:
    """고위험 유형 분포 막대 (민감 테이블 기준, 복수 태그)."""
    if full.empty:
        return "<div class='muted'>실행이 끝나면 고위험 유형 분포가 표시됩니다.</div>"
    sens = full[full["decision"] == "sensitive"]
    tags = sens["high_risk_tag"].str.split("|").explode().str.strip()
    counts = tags[tags != ""].value_counts()
    total = max(int(counts.sum()), 1)
    bar = "".join(f"<div title='{t} {counts.get(t, 0)}' style='width:{counts.get(t, 0) / total * 100:.1f}%;background:{c}'></div>"
                  for t, c in TAG_COLORS.items())
    legend = " ".join(f"<span style='margin-right:10px'><span style='display:inline-block;width:8px;height:8px;"
                      f"border-radius:50%;background:{c}'></span> {t} {counts.get(t, 0)}</span>" for t, c in TAG_COLORS.items())
    return (f"<div style='display:flex;justify-content:space-between'><b>고위험 유형 분포</b>"
            f"<span class='muted'>민감 {len(sens)}건 기준 · 복수 태그</span></div>"
            f"<div style='display:flex;height:10px;border-radius:5px;overflow:hidden;background:#eef0f3;margin:8px 0'>{bar}</div>"
            f"<div class='muted'>{legend}</div>")


DECISION_LABEL = {"sensitive": "민감", "non-sensitive": "비민감", "error": "에러"}
DECISION_STYLE = {"민감": "background-color:#fdecec;color:#d8434b;font-weight:700",
                  "비민감": "background-color:#e6f6ef;color:#1f9d6b;font-weight:700",
                  "에러": "background-color:#eef0f3;color:#6b7280;font-weight:700"}


# 결과 표 열 폭: 모두 %로 지정(합 100%)해서 화면이 넓어지면 표도 꽉 차게 늘어난다.
# (px 고정 폭을 섞으면 넓은 화면에서 오른쪽이 비거나 넘친다)
RESULT_COL_WIDTHS = ["17%", "8%", "10%", "10%", "14%", "14%", "17%", "10%"]


def result_table(full: pd.DataFrame, active: str | None):
    """KPI 필터를 적용한 결과 표(색 포함)."""
    cols = ["테이블명", "잠금 여부", "민감 여부", "검토", "민감 컬럼", "민감 행 조건", "고위험 유형", "신뢰도"]
    if full.empty:
        return pd.DataFrame(columns=cols)
    df = full
    if active == "review":
        df = df[df["needs_review"] == "True"]
    elif active:
        df = df[df["decision"] == active]
    view = pd.DataFrame({
        "테이블명": df["table_id"],
        "잠금 여부": df["table_id"].map(lambda t: "🔒" if is_locked(t) else ""),
        "민감 여부": df["decision"].map(DECISION_LABEL).fillna(df["decision"]),
        "검토": df["needs_review"].map({"True": "검토 필요"}).fillna(""),
        "민감 컬럼": df["sensitive_columns"], "민감 행 조건": df["row_conditions"],
        "고위험 유형": df["high_risk_tag"].str.replace("|", " · ", regex=False),
        "신뢰도": pd.to_numeric(df["confidence"], errors="coerce").round(2).astype(str).replace("nan", ""),
    }).reset_index(drop=True)
    return (view.style
            .map(lambda v: DECISION_STYLE.get(v, ""), subset=["민감 여부"])
            .map(lambda v: "background-color:#fdf3e1;color:#c98111;font-weight:700" if v else "", subset=["검토"]))


def is_locked(table_id: str) -> bool:
    meta = STATE.table_info.get(table_id)
    return bool(meta and meta.locked)


def section(title: str, body: str, style: str = "") -> str:
    """상세 패널의 항목 1개: 굵은 제목 + 내용. 항목 사이는 한 줄(약 20px) 띄운다."""
    return f"<div style='margin-bottom:20px'><b>{title}</b><div style='margin-top:4px;{style}'>{body}</div></div>"


def detail_html(full: pd.DataFrame, table_id: str) -> str:
    row = full[full["table_id"] == table_id].iloc[0]
    label = DECISION_LABEL.get(row["decision"], row["decision"])
    conf = float(row["confidence"] or 0)
    tag_reasons = "".join(f"<div style='margin:3px 0'>{html.escape(p.strip())}</div>"
                          for p in row["high_risk_tag_reason"].split(" | ") if p.strip()) or "-"
    conds = "".join(f"<div style='background:#fafbfd;border:1px solid #e3e7ef;border-radius:6px;"
                    f"padding:6px 8px;margin:3px 0'>{html.escape(c.strip())}</div>"
                    for c in row["row_conditions"].split(" | ") if c.strip()) or "-"
    reviews = "".join(f"<div style='margin:3px 0'>{html.escape(r.strip())}</div>"
                      for r in row["review_reasons"].split(" | ") if r.strip()) or "-"
    meta = STATE.table_info.get(table_id)
    kor_name, source_table = (meta.table_kor_name, meta.source_table) if meta else ("", "")
    lock = " <span title='잠금 테이블'>🔒</span>" if is_locked(table_id) else ""
    return (f"<h3 style='margin:0'>{html.escape(table_id)}{lock}</h3>"
            f"<div class='muted'>{html.escape(kor_name) or '-'}</div>"
            f"<div class='muted'>원천: {html.escape(source_table) or '-'}</div>"
            f"<div style='display:grid;grid-template-columns:80px 1fr;gap:6px 10px;margin:12px 0 20px;font-size:13px'>"
            f"<span class='muted'>판정</span><span><span class='badge' style='{DECISION_STYLE.get(label, '')}'>{label}</span></span>"
            f"<span class='muted'>신뢰도</span><span><span style='display:inline-block;width:120px;height:8px;background:#e8ecf3;"
            f"border-radius:4px;vertical-align:middle'><span style='display:block;height:100%;width:{conf * 100:.0f}%;"
            f"background:{'#c98111' if conf < 0.7 else '#1f9d6b'};border-radius:4px'></span></span> {conf:.2f}</span></div>"
            + section("민감 컬럼", html.escape(row["sensitive_columns"]) or "-")
            + section("민감 행 조건", conds)
            + section("매칭 키워드", html.escape(row["matched_keywords"]) or "-")
            + section("고위험 유형", tag_reasons)
            + section("판정 이유", html.escape(row["reason"]) or "-")
            + section("추가 검토 필요 사항", reviews)
            + (section("error", html.escape(row["error"]), "color:#d8434b") if row["error"] else ""))


def file_size_label(path) -> str:
    """파일 크기를 '6.0 KB' 같은 읽기 쉬운 형태로 바꾼다."""
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def download_button(path):
    if not path:
        return gr.DownloadButton("⬇ 다운로드 (결과 없음)", value=None, interactive=False, size="sm", scale=0, min_width=170)
    return gr.DownloadButton(f"⬇ 다운로드 ({file_size_label(path)})", value=str(path), interactive=True, size="sm")


def run_pipeline(schema: str, model: str, effort: str):
    try:
        STATE.result_path = app.run(schema=schema, model=model, reasoning_effort=effort,
                                    on_result=STATE.add, stop_event=STATE.stop_event)
    except Exception:
        logging.exception("실행 실패")
    finally:
        STATE.running = False
        STATE.stage = "완료"


def start_run(schema, model, effort):
    """실행 버튼: 백그라운드에서 파이프라인을 돌리고, 1초마다 화면을 갱신한다."""
    if STATE.running:
        raise gr.Error("이미 실행 중입니다.")
    table_path = app.tables_file(schema)
    if not table_path.exists():
        raise gr.Error(f"data/{table_path.name} 파일이 없습니다. 스키마 선택과 파일 이름을 확인해 주세요.")
    if not KEYWORDS_FILE.exists():
        raise gr.Error("data/keywords.txt 파일이 없습니다.")
    tables = load_tables(table_path)
    STATE.reset(total=len(tables))
    STATE.table_info = {t.table_id: t for t in tables}
    worker = threading.Thread(target=run_pipeline, args=(schema, model, effort), daemon=True)
    worker.start()
    keep = [gr.update()] * 5
    while worker.is_alive():
        yield stage_html(), "\n".join(STATE.logs), *kpi_buttons(STATE.counts, None), *keep
        time.sleep(1)

    full = load_result(STATE.result_path)
    yield (stage_html(), "\n".join(STATE.logs), *kpi_buttons(result_counts(full), None),
           full, None, tag_html(full), result_table(full, None), download_button(STATE.result_path))


def stop_run():
    if STATE.running:
        STATE.stop_event.set()
        gr.Info("중지 요청됨: 처리 중인 테이블이 끝나면 멈추고, 그때까지의 결과를 엑셀로 저장합니다.")


def toggle_filter(key: str):
    def handler(full, active):
        active = None if active == key else key
        return (active, *kpi_buttons(result_counts(full), active), result_table(full, active), gr.Column(visible=False))
    return handler


def on_select(full, shown, evt: gr.SelectData):
    table_id = shown.iloc[evt.index[0]]["테이블명"]
    return gr.Column(visible=True), detail_html(full, table_id)


# =====================================================================
# 화면 구성
# =====================================================================
def build() -> gr.Blocks:
    with gr.Blocks(title="고위험 데이터 분류기", fill_width=True) as demo:
        gr.HTML("<div class='apptitle'>고위험 데이터 분류기</div>")
        with gr.Tabs(elem_id="steps", selected="run") as tabs:   # 앱을 열면 STEP 3(실행 · 결과)부터

            # ── 1 키워드 관리 ──
            with gr.Tab("키워드 관리", id="keyword"):
                first = read_keywords()
                kw_full, kw_orig = gr.State(first), gr.State(first)
                c0, b0, m0 = kw_summary(first, first)
                kw_cards = gr.HTML(c0)
                kw_query = gr.Textbox(placeholder="🔍 키워드 · 설명 · 유사어 검색", show_label=False, container=False)
                kw_sys = gr.Radio(sys_choices(first), value="전체", label="시스템 (sys_cd)", elem_classes="chips")
                kw_msg = gr.Markdown(m0)
                with gr.Row(elem_id="kw-tablebar"):          # 표 머리줄의 복사·확대 아이콘 왼쪽에 겹쳐 놓는다
                    kw_addbtn = gr.Button("＋ 행 추가", size="sm", scale=0, min_width=90)
                kw_table = gr.Dataframe(value=kw_view(first, first, "전체", ""), interactive=True, wrap=True,
                                        static_columns=[0], max_height=520,
                                        column_widths=["5%", "8%", "11%", "24%", "18%", "9%", "9%", "9%", "7%"],   # 합 100% → 화면 너비에 맞춰 늘어남
                                        label="셀을 눌러 편집 · 초록 = 신규, 노랑 = 수정, 회색 = history '삭제', 빨강 = 저장 전에 고칠 칸")
                with gr.Row(elem_classes="actionbar"):
                    kw_bar = gr.HTML(b0)
                    kw_revert_btn = gr.Button("↺ 원본으로 되돌리기", size="sm", scale=0, min_width=150)
                    kw_save_btn = gr.Button("💾 저장", size="sm", scale=0, min_width=80)
                    kw_next_btn = gr.Button("다음: 프롬프트 편집 →", variant="primary", size="sm", scale=0, min_width=160)

                outs = [kw_full, kw_table, kw_cards, kw_bar, kw_msg, kw_sys]
                kw_table.input(kw_on_edit, [kw_table, kw_full, kw_orig, kw_sys, kw_query], outs, trigger_mode="always_last")
                kw_sys.change(kw_refresh, [kw_full, kw_orig, kw_sys, kw_query], outs)
                kw_query.submit(kw_refresh, [kw_full, kw_orig, kw_sys, kw_query], outs)
                kw_query.change(kw_refresh, [kw_full, kw_orig, kw_sys, kw_query], outs)
                kw_addbtn.click(kw_add_row, [kw_full, kw_orig, kw_sys, kw_query], outs)
                kw_save_btn.click(kw_save, [kw_full, kw_orig, kw_sys, kw_query], outs + [kw_orig])
                kw_revert_btn.click(kw_revert, [kw_sys, kw_query], outs + [kw_orig])
                kw_next_btn.click(kw_next, [kw_full, kw_orig], tabs)

            # ── 2 프롬프트 편집 ──
            with gr.Tab("프롬프트 편집", id="prompt"):
                sys_text, tag_text = gr.State(read_prompt("system")), gr.State(read_prompt("tag"))
                with gr.Tabs():
                    with gr.Tab(PROMPT_TITLES["system"]):
                        ed_sys = prompt_editor("system", sys_text)
                    with gr.Tab(PROMPT_TITLES["tag"]):
                        ed_tag = prompt_editor("tag", tag_text)
                with gr.Row(elem_classes="actionbar"):
                    pr_bar = gr.HTML(prompt_bar(read_prompt("system"), read_prompt("tag")))
                    pr_revert = gr.Button("↺ 원본으로 되돌리기", size="sm", scale=0, min_width=150)
                    pr_save = gr.Button("💾 저장", size="sm", scale=0, min_width=80)
                    pr_next = gr.Button("다음: 실행 · 결과 →", variant="primary", size="sm", scale=0, min_width=150)

                def current(s, t, e1, o1, m1, e2, o2, m2):
                    """편집 칸에 방금 입력한 내용까지 반영한 두 프롬프트 전문."""
                    s = replace_section(s, o1, e1) if m1 == "편집" else s
                    t = replace_section(t, o2, e2) if m2 == "편집" else t
                    return s, t
                cur_in = [sys_text, tag_text, ed_sys["editor"], ed_sys["outline"], ed_sys["mode"],
                          ed_tag["editor"], ed_tag["outline"], ed_tag["mode"]]

                for ed in (ed_sys, ed_tag):
                    ed["editor"].input(prompt_bar, [sys_text, tag_text], pr_bar, trigger_mode="always_last")
                pr_save.click(current, cur_in, [sys_text, tag_text]).then(
                    save_prompts, [sys_text, tag_text],
                    [pr_bar, ed_sys["info"], ed_tag["info"], ed_sys["version"], ed_tag["version"]])

                def revert():
                    s, t = read_prompt("system"), read_prompt("tag")
                    return (s, t, gr.Code(s, visible=True), gr.Code(t, visible=True),
                            gr.Radio(choices=outline_choices(s), value=-1), gr.Radio(choices=outline_choices(t), value=-1),
                            "편집", "편집", prompt_info("system", s), prompt_info("tag", t), prompt_bar(s, t),
                            gr.HTML(visible=False), gr.HTML(visible=False), "", "")
                pr_revert.click(revert, None, [sys_text, tag_text, ed_sys["editor"], ed_tag["editor"],
                                               ed_sys["outline"], ed_tag["outline"], ed_sys["mode"], ed_tag["mode"],
                                               ed_sys["info"], ed_tag["info"], pr_bar, ed_sys["diff"], ed_tag["diff"],
                                               ed_sys["compare_to"], ed_tag["compare_to"]])

                def to_run(s, t):
                    if s != read_prompt("system") or t != read_prompt("tag"):
                        gr.Warning("저장하지 않은 프롬프트 변경이 있습니다. 저장해야 실행에 반영됩니다.")
                    return gr.Tabs(selected="run")
                pr_next.click(current, cur_in, [sys_text, tag_text]).then(to_run, [sys_text, tag_text], tabs)

            # ── 3 실행 · 결과 ──
            with gr.Tab("실행 · 결과", id="run") as run_tab:
                full_result, active = gr.State(pd.DataFrame()), gr.State(None)
                with gr.Row():
                    # 좌측: 실행 설정 → 실행 → 진행 단계 → 로그
                    with gr.Column(scale=1, min_width=280):
                        with gr.Group():
                            choices = schema_choices()
                            default = next((v for l, v in choices if "없음" not in l), app.SCHEMAS[0])
                            schema = gr.Radio(choices, value=default, label="스키마", info=schema_info(default),
                                              elem_classes="chips")
                            model = gr.Dropdown(MODELS, value=MODELS[0], label="LLM 모델")
                            effort = gr.Radio(REASONING_EFFORTS, value=DEFAULT_EFFORT, label="추론 정도 (reasoning_effort)", elem_classes="seg")
                        with gr.Row():
                            run_btn = gr.Button("▶ 실행", variant="primary", size="md", scale=3, min_width=120)
                            stop_btn = gr.Button("■ 중지", size="md", scale=1, min_width=70)
                        stage = gr.HTML(stage_html())
                        with gr.Accordion("실행 로그", open=True):
                            log_box = gr.Textbox(show_label=False, lines=14, max_lines=14, autoscroll=True, elem_id="log")

                    # 가운데: 결과 미리보기 → KPI 카드(누르면 필터) → 유형 분포 → 표
                    with gr.Column(scale=3):
                        with gr.Row():
                            gr.HTML("<div class='section-title'>테이블 분류 결과</div>", container=False, padding=False)
                            download_btn = download_button(None)
                        with gr.Row():
                            kpis = [gr.Button(f"{name}\n-", elem_classes=["kpi", cls], min_width=110) for _, name, cls in KPI]
                        gr.HTML("<div class='muted' style='margin-top:-6px'>카드를 누르면 그 건만 표에 표시되고, 한 번 더 누르면 전체로 돌아갑니다.</div>")
                        tags = gr.HTML(tag_html(pd.DataFrame()))
                        preview = gr.Dataframe(value=result_table(pd.DataFrame(), None), interactive=False, wrap=True,
                                               show_search="search", max_height=560,
                                               column_widths=RESULT_COL_WIDTHS,
                                               label="행을 누르면 오른쪽에 상세가 열립니다")

                    # 오른쪽: 상세 (행을 누르면 열림)
                    with gr.Column(scale=1, min_width=300, visible=False) as detail_col:
                        close_btn = gr.Button("✕ 닫기", size="sm", scale=0, min_width=70, elem_id="close-detail")
                        detail = gr.HTML()

                schema.input(lambda s: gr.Radio(info=schema_info(s)), schema, schema)
                run_tab.select(lambda s: gr.Radio(choices=schema_choices(), value=s, info=schema_info(s)), schema, schema)
                run_btn.click(start_run, [schema, model, effort],
                              [stage, log_box, *kpis, full_result, active, tags, preview, download_btn])
                stop_btn.click(stop_run)
                for btn, (key, _, _) in zip(kpis, KPI):
                    btn.click(toggle_filter(key), [full_result, active], [active, *kpis, preview, detail_col])
                preview.select(on_select, [full_result, preview], [detail_col, detail])
                close_btn.click(lambda: gr.Column(visible=False), None, detail_col)
    return demo


def launch():
    handler = UILogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger().addHandler(handler)
    build().launch(inbrowser=True, css=CSS.replace("__FONT__", FONT_CSS), theme=THEME)
