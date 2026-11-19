"""로컬 실행용 Gradio UI: ① 키워드 관리  ② 프롬프트 편집  ③ 실행 · 결과.

python app.py 를 실행하면 브라우저에 http://127.0.0.1:7860 이 열린다.
파이프라인 자체는 app.run()을 그대로 호출하고, 이 파일은 화면과 파일 저장만 맡는다.
"""
from __future__ import annotations

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
MODELS = ["HCP-LLM-Latest", "HCP-Medium-Latest", "HCP-Small-Latest"]   # 첫 번째가 기본값
REASONING_EFFORTS = ["low", "high", "xhigh"]
DEFAULT_EFFORT = "high"
PREVIEW_COLUMNS = ["table_id", "sys_cd", "decision", "sensitive_columns", "row_conditions",
                   "high_risk_tag", "needs_review"]


def now() -> str:
    return datetime.now().strftime("%m%d_%H%M%S")


def backup(path) -> None:
    """저장 전에 원본을 backup/ 폴더에 복사해 둔다."""
    if path.exists():
        folder = path.parent / "backup"
        folder.mkdir(exist_ok=True)
        shutil.copy2(path, folder / f"{path.stem}_{now()}{path.suffix}")


# =====================================================================
# 실행 상태 (실행은 백그라운드 스레드, 화면은 1초마다 이 값을 읽어 갱신)
# =====================================================================
class RunState:
    def __init__(self):
        self.running = False
        self.total = 0
        self.counts = Counter()
        self.logs = deque(maxlen=300)
        self.started = 0.0
        self.result_path = None
        self.stop_event = threading.Event()

    def reset(self, total: int):
        self.__init__()
        self.running, self.total, self.started = True, total, time.time()

    def add(self, result):                     # app.run()이 테이블 1건마다 호출
        self.counts["done"] += 1
        self.counts[result.decision] += 1
        self.counts["needs_review"] += result.needs_review


STATE = RunState()


class UILogHandler(logging.Handler):
    """파이프라인 로그를 화면 로그 창으로 보낸다."""
    def emit(self, record):
        STATE.logs.append(self.format(record))


# =====================================================================
# ① 키워드 관리
# =====================================================================
def load_keywords_df() -> pd.DataFrame:
    return read_txt(KEYWORDS_FILE)


def keyword_counts(df: pd.DataFrame) -> str:
    deleted = int((df["history"] == "삭제").sum()) if "history" in df else 0
    return f"**전체 {len(df)}** · 적용 {len(df) - deleted} · 삭제 {deleted}"


def clean_keywords(df: pd.DataFrame) -> pd.DataFrame:
    df = df.fillna("").astype(str)
    df = df.apply(lambda col: col.str.strip())
    return df[(df != "").any(axis=1)]          # 완전히 빈 행(행 추가 후 비워 둔 행)은 버린다


def save_keywords(df: pd.DataFrame):
    if STATE.running:
        raise gr.Error("실행 중에는 저장할 수 없습니다. 실행이 끝난 뒤 저장해 주세요.")
    df = clean_keywords(df)

    # 저장을 막는 오류
    errors = []
    blank = df[(df["sys_cd"] == "") | (df["keyword"] == "")]
    if len(blank):
        errors.append(f"sys_cd 또는 keyword가 빈 행 {len(blank)}개")
    active = df[df["history"] != "삭제"]
    dup = active[active.duplicated(["sys_cd", "keyword"], keep=False)]
    if len(dup):
        errors.append("같은 sys_cd 안의 중복 키워드: " + ", ".join(sorted(set(dup["keyword"]))))
    if errors:
        raise gr.Error(" / ".join(errors))

    # 저장은 하되 알려 줄 경고
    bad_sep = df[df["equivalent_terms"].str.contains(r"[/,]")]
    if len(bad_sep):
        gr.Warning("equivalent_terms에 '/' 또는 ','가 있습니다. 여러 표현은 '|'로 구분해야 합니다: "
                   + ", ".join(bad_sep["keyword"].head(5)))

    saved = clean_keywords(load_keywords_df())
    if df.reset_index(drop=True).equals(saved.reset_index(drop=True)):
        gr.Info("변경된 내용이 없어 저장하지 않았습니다.")
        return df, keyword_counts(df)

    backup(KEYWORDS_FILE)
    df.to_csv(KEYWORDS_FILE, sep="\t", index=False, encoding="utf-8-sig")
    gr.Info(f"keywords.txt 저장 완료 ({len(df)}행, 이전 파일은 data/backup/에 보관)")
    return df, keyword_counts(df)


def add_keyword_row(df: pd.DataFrame) -> pd.DataFrame:
    """표 맨 아래에 빈 행 하나를 추가한다."""
    empty = pd.DataFrame([[""] * len(df.columns)], columns=df.columns)
    return pd.concat([df, empty], ignore_index=True)


def reload_keywords():
    df = load_keywords_df()
    return df, keyword_counts(df)


# =====================================================================
# ② 프롬프트 편집
# =====================================================================
def read_prompt(key: str) -> str:
    return PROMPT_FILES[key].read_text(encoding="utf-8")


def save_prompts(system_text: str, tag_text: str) -> str:
    """두 프롬프트를 한 번에 저장한다. 바뀐 파일만 백업 후 저장한다."""
    if STATE.running:
        raise gr.Error("실행 중에는 저장할 수 없습니다.")
    changed = []
    for key, text in (("system", system_text), ("tag", tag_text)):
        if text == read_prompt(key):
            continue                                   # 변경 없음: 저장·백업 안 함
        backup(PROMPT_FILES[key])
        PROMPT_FILES[key].write_text(text, encoding="utf-8")
        changed.append(PROMPT_FILES[key].name)
    if not changed:
        gr.Info("변경된 내용이 없어 저장하지 않았습니다.")
        return "변경 없음"
    gr.Info(f"{', '.join(changed)} 저장 완료 (이전 버전은 prompts/backup/에 보관)")
    return f"저장됨 · {datetime.now():%H:%M:%S} · {', '.join(changed)}"


def reload_prompts():
    return read_prompt("system"), read_prompt("tag")


# =====================================================================
# ③ 실행 · 결과
# =====================================================================
def progress_view():
    done, total = STATE.counts["done"], max(STATE.total, 1)
    pct = int(done / total * 100)
    elapsed = time.time() - STATE.started if STATE.started else 0
    remain = elapsed / done * (STATE.total - done) if done else 0
    status = "실행 중" if STATE.running else "대기"
    bar = (f'<div style="height:12px;background:#f3f4f6;border-radius:6px;overflow:hidden">'
           f'<div style="height:100%;width:{pct}%;background:#f97316"></div></div>'
           f'<div style="margin-top:6px;color:#6b7280;font-size:13px">{status} · {done} / {STATE.total} 테이블 · '
           f'경과 {app.format_elapsed(elapsed)} · 남은 예상 {app.format_elapsed(remain)}</div>')
    c = STATE.counts
    stats = (f"민감 **{c['sensitive']}**  ·  비민감 **{c['non-sensitive']}**  ·  "
             f"검토 필요 **{c['needs_review']}**  ·  에러 발생 **{c['error']}**")
    return bar, stats, "\n".join(STATE.logs)


def load_result(path) -> pd.DataFrame:
    return pd.read_excel(path, dtype=str).fillna("") if path else pd.DataFrame(columns=PREVIEW_COLUMNS)


REVIEW_FILTERS = ["전체", "검토 필요", "검토 불필요"]


def filter_result(full: pd.DataFrame, decision: str, review: str) -> pd.DataFrame:
    df = full
    if decision != "전체":
        df = df[df["decision"] == decision]
    if review == "검토 필요":
        df = df[df["needs_review"] == "True"]
    elif review == "검토 불필요":
        df = df[df["needs_review"] != "True"]
    return df[PREVIEW_COLUMNS] if len(df.columns) else df


def file_size_label(path) -> str:
    """파일 크기를 '6.0 KB' 같은 읽기 쉬운 형태로 바꾼다."""
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def download_view(path):
    """결과 파일 영역: 파일명 안내 + '다운로드 (용량)' 버튼."""
    if not path:
        return "결과 파일이 아직 없습니다. 실행이 끝나면 여기서 내려받을 수 있습니다.", gr.DownloadButton(visible=False)
    return (f"**{path.name}**",
            gr.DownloadButton(f"⬇ 다운로드 ({file_size_label(path)})", value=str(path), visible=True))


def run_pipeline(schema: str, model: str, effort: str):
    try:
        STATE.result_path = app.run(schema=schema, model=model, reasoning_effort=effort,
                                    on_result=STATE.add, stop_event=STATE.stop_event)
    except Exception:
        logging.exception("실행 실패")
    finally:
        STATE.running = False


def start_run(schema, model, effort):
    """실행 버튼: 백그라운드에서 파이프라인을 돌리고, 1초마다 화면을 갱신한다."""
    if STATE.running:
        raise gr.Error("이미 실행 중입니다.")
    table_path = app.tables_file(schema)
    if not table_path.exists():
        raise gr.Error(f"data/{table_path.name} 파일이 없습니다. 스키마 선택과 파일 이름을 확인해 주세요.")
    if not KEYWORDS_FILE.exists():
        raise gr.Error("data/keywords.txt 파일이 없습니다.")
    STATE.reset(total=len(load_tables(table_path)))
    worker = threading.Thread(target=run_pipeline, args=(schema, model, effort), daemon=True)
    worker.start()
    while worker.is_alive():
        yield *progress_view(), gr.update(), gr.update(), gr.update(), gr.update()
        time.sleep(1)

    full = load_result(STATE.result_path)
    yield *progress_view(), full, filter_result(full, "전체", "전체"), *download_view(STATE.result_path)


def stop_run():
    if STATE.running:
        STATE.stop_event.set()
        gr.Info("중지 요청됨: 처리 중인 테이블이 끝나면 멈추고, 그때까지의 결과를 엑셀로 저장합니다.")


def show_detail(full: pd.DataFrame, shown: pd.DataFrame, evt: gr.SelectData) -> str:
    """결과 표에서 행을 누르면 판정 이유·태그 근거를 보여 준다."""
    table_id = shown.iloc[evt.index[0]]["table_id"]
    row = full[full["table_id"] == table_id].iloc[0]
    return (f"#### {table_id}\n"
            f"- **판정 이유**: {row['reason']}\n"
            f"- **근거 키워드**: {row['matched_keywords'] or '-'}\n"
            f"- **고위험 태그 근거**: {row['high_risk_tag_reason'] or '-'}\n"
            f"- **검토 사유**: {row['review_reasons'] or '-'}\n"
            f"- **error**: {row['error'] or '-'}")


# =====================================================================
# 화면 구성
# =====================================================================
def build() -> gr.Blocks:
    with gr.Blocks(title="민감 테이블 식별 PoC") as demo:
        gr.Markdown("## Data Lake 민감 테이블 식별 PoC")
        with gr.Tabs() as tabs:

            # ── ① 키워드 관리 ──
            with gr.Tab("① 키워드 관리", id="keyword"):
                kw_count = gr.Markdown(keyword_counts(load_keywords_df()))
                kw_table = gr.Dataframe(value=load_keywords_df(), interactive=True, wrap=True,
                                        label="keywords.txt (셀을 눌러 편집)")
                with gr.Row():
                    kw_add = gr.Button("＋ 행 추가")
                    kw_reload = gr.Button("↺ 변경 취소 (파일 다시 읽기)")
                    kw_save = gr.Button("💾 저장", variant="primary")
                    kw_next = gr.Button("다음 ▶")
                gr.Markdown("저장 전 확인: 빈 sys_cd·keyword, 같은 sys_cd 안의 중복 키워드는 저장을 막고, "
                            "equivalent_terms의 '/'·',' 구분자는 경고합니다. "
                            "변경된 내용이 있을 때만 저장하며, 이전 파일은 data/backup/에 보관됩니다.")
                kw_add.click(add_keyword_row, kw_table, kw_table)
                kw_save.click(save_keywords, kw_table, [kw_table, kw_count])
                kw_reload.click(reload_keywords, None, [kw_table, kw_count])
                kw_next.click(lambda: gr.Tabs(selected="prompt"), None, tabs)

            # ── ② 프롬프트 편집 ──
            with gr.Tab("② 프롬프트 편집", id="prompt"):
                with gr.Row():
                    system_box = gr.Textbox(value=read_prompt("system"), lines=28, max_lines=28,
                                            label="판정 프롬프트 · system_prompt.md")
                    tag_box = gr.Textbox(value=read_prompt("tag"), lines=28, max_lines=28,
                                         label="고위험 태그 프롬프트 · tag_prompt.md")
                with gr.Row():
                    pr_reload = gr.Button("↺ 원본으로 되돌리기")
                    pr_save = gr.Button("💾 저장", variant="primary")
                    pr_next = gr.Button("다음 ▶")
                pr_status = gr.Markdown("user_prompt.md는 코드가 테이블마다 값을 채우는 틀이라 편집 대상에서 제외했습니다. "
                                        "변경된 파일만 저장하며, 이전 버전은 prompts/backup/에 보관됩니다.")
                pr_save.click(save_prompts, [system_box, tag_box], pr_status)
                pr_reload.click(reload_prompts, None, [system_box, tag_box])
                pr_next.click(lambda: gr.Tabs(selected="run"), None, tabs)

            # ── ③ 실행 · 결과 ──
            with gr.Tab("③ 실행 · 결과", id="run"):
                full_result = gr.State(pd.DataFrame())
                with gr.Row():
                    # 좌측: 실행 설정 · 실행 버튼 · 진행 막대 · 실행 로그
                    with gr.Column(scale=1):
                        with gr.Group():
                            schema = gr.Dropdown(app.SCHEMAS, value=app.SCHEMAS[0], label="스키마명")
                            model = gr.Dropdown(MODELS, value=MODELS[0], label="LLM 모델")
                            effort = gr.Dropdown(REASONING_EFFORTS, value=DEFAULT_EFFORT, label="추론 정도 (reasoning_effort)")
                        run_btn = gr.Button("▶ 실행", variant="primary", size="lg")
                        stop_btn = gr.Button("■ 중지")
                        bar = gr.HTML(progress_view()[0])
                        log_box = gr.Textbox(label="실행 로그", lines=16, max_lines=16, autoscroll=True)

                    # 우측: 결과 미리보기 → 건수 → 필터 → 표 → 상세 → 결과 파일
                    with gr.Column(scale=3):
                        gr.Markdown("### 결과 미리보기")
                        stats = gr.Markdown(progress_view()[1])
                        with gr.Row():
                            decision_filter = gr.Dropdown(["전체", "sensitive", "non-sensitive", "error"],
                                                          value="전체", label="decision")
                            review_filter = gr.Dropdown(REVIEW_FILTERS, value="전체", label="검토 필요 여부")
                        preview = gr.Dataframe(value=pd.DataFrame(columns=PREVIEW_COLUMNS), interactive=False, wrap=True,
                                               label="행을 누르면 아래에 상세가 표시됩니다")
                        detail = gr.Markdown()
                        with gr.Group():
                            gr.Markdown("#### 결과 파일 (xlsx)")
                            file_name = gr.Markdown(download_view(None)[0])
                            download_btn = gr.DownloadButton(visible=False, variant="primary")

                run_btn.click(start_run, [schema, model, effort],
                              [bar, stats, log_box, full_result, preview, file_name, download_btn])
                stop_btn.click(stop_run)
                for ctl in (decision_filter, review_filter):
                    ctl.change(filter_result, [full_result, decision_filter, review_filter], preview)
                preview.select(show_detail, [full_result, preview], detail)
    return demo


def launch():
    handler = UILogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger().addHandler(handler)
    build().launch(inbrowser=True)          # 실행하면 브라우저가 자동으로 열린다
