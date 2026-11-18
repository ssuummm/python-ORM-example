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
from src.config import load_settings
from src.loader import load_tables, read_txt
from src.prompt import PROMPT_DIR

KEYWORDS_FILE = app.KEYWORDS_FILE
PROMPT_FILES = {"system": PROMPT_DIR / "system_prompt.md", "tag": PROMPT_DIR / "tag_prompt.md"}
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

    backup(KEYWORDS_FILE)
    df.to_csv(KEYWORDS_FILE, sep="\t", index=False, encoding="utf-8-sig")
    gr.Info(f"keywords.txt 저장 완료 ({len(df)}행, 이전 파일은 data/backup/에 보관)")
    return df, keyword_counts(df)


def reload_keywords():
    df = load_keywords_df()
    return df, keyword_counts(df)


# =====================================================================
# ② 프롬프트 편집
# =====================================================================
def read_prompt(key: str) -> str:
    return PROMPT_FILES[key].read_text(encoding="utf-8")


def save_prompt(key: str, text: str) -> str:
    if STATE.running:
        raise gr.Error("실행 중에는 저장할 수 없습니다.")
    backup(PROMPT_FILES[key])
    PROMPT_FILES[key].write_text(text, encoding="utf-8")
    gr.Info(f"{PROMPT_FILES[key].name} 저장 완료 (이전 버전은 prompts/backup/에 보관)")
    return f"저장됨 · {datetime.now():%H:%M:%S} · {len(text.splitlines())}줄"


# =====================================================================
# ③ 실행 · 결과
# =====================================================================
def readiness() -> str:
    """실행 전 점검 표."""
    lines = []
    if app.TABLES_FILE.exists():
        lines.append(f"| datalake_crawling_result.txt | ✅ 테이블 {len(load_tables(app.TABLES_FILE))}개 |")
    else:
        lines.append("| datalake_crawling_result.txt | ❌ 파일 없음 |")
    if KEYWORDS_FILE.exists():
        df = load_keywords_df()
        lines.append(f"| keywords.txt | ✅ 적용 {int((df['history'] != '삭제').sum())}개 |")
    else:
        lines.append("| keywords.txt | ❌ 파일 없음 |")
    s = load_settings()
    effort = getattr(s, "reasoning_effort", "-")
    lines.append(f"| LLM | {s.provider} · {s.model or '(모델 미설정)'} · effort {effort} |")
    return "| 항목 | 상태 |\n|---|---|\n" + "\n".join(lines)


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
    stats = (f"**민감** {c['sensitive']}  ·  **비민감** {c['non-sensitive']}  ·  "
             f"**검토 필요** {c['needs_review']}  ·  **error** {c['error']}")
    return bar, stats, "\n".join(STATE.logs)


def load_result(path) -> pd.DataFrame:
    return pd.read_excel(path, dtype=str).fillna("") if path else pd.DataFrame(columns=PREVIEW_COLUMNS)


def filter_result(full: pd.DataFrame, decision: str, review_only: bool) -> pd.DataFrame:
    df = full
    if decision != "전체":
        df = df[df["decision"] == decision]
    if review_only:
        df = df[df["needs_review"] == "True"]
    return df[PREVIEW_COLUMNS] if len(df.columns) else df


def run_pipeline(workers: int):
    try:
        STATE.result_path = app.run(workers=int(workers), on_result=STATE.add, stop_event=STATE.stop_event)
    except Exception:
        logging.exception("실행 실패")
    finally:
        STATE.running = False


def start_run(workers):
    """실행 버튼: 백그라운드에서 파이프라인을 돌리고, 1초마다 화면을 갱신한다."""
    if STATE.running:
        raise gr.Error("이미 실행 중입니다.")
    if not (app.TABLES_FILE.exists() and KEYWORDS_FILE.exists()):
        raise gr.Error("data/ 폴더의 입력 파일을 확인해 주세요.")
    STATE.reset(total=len(load_tables(app.TABLES_FILE)))
    worker = threading.Thread(target=run_pipeline, args=(workers,), daemon=True)
    worker.start()
    while worker.is_alive():
        yield *progress_view(), gr.update(), gr.update(), gr.update()
        time.sleep(1)

    full = load_result(STATE.result_path)
    result_file = str(STATE.result_path) if STATE.result_path else None   # gr.File은 문자열 경로를 받는다
    yield *progress_view(), full, filter_result(full, "전체", False), result_file


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

        # ── ① 키워드 관리 ──
        with gr.Tab("① 키워드 관리"):
            kw_count = gr.Markdown(keyword_counts(load_keywords_df()))
            kw_table = gr.Dataframe(value=load_keywords_df(), interactive=True, wrap=True,
                                    row_count=(1, "dynamic"), label="keywords.txt (셀을 눌러 편집 · 맨 아래에서 행 추가)")
            with gr.Row():
                kw_reload = gr.Button("↺ 변경 취소 (파일 다시 읽기)")
                kw_save = gr.Button("💾 저장", variant="primary")
            gr.Markdown("저장 전 확인: 빈 sys_cd·keyword, 같은 sys_cd 안의 중복 키워드는 저장을 막고, "
                        "equivalent_terms의 '/'·',' 구분자는 경고합니다. 저장 시 이전 파일은 data/backup/에 보관됩니다.")
            kw_save.click(save_keywords, kw_table, [kw_table, kw_count])
            kw_reload.click(reload_keywords, None, [kw_table, kw_count])

        # ── ② 프롬프트 편집 ──
        with gr.Tab("② 프롬프트 편집"):
            with gr.Row():
                for key, label in [("system", "판정 프롬프트 · system_prompt.md"),
                                   ("tag", "고위험 태그 프롬프트 · tag_prompt.md")]:
                    with gr.Column():
                        box = gr.Textbox(value=read_prompt(key), label=label, lines=28, max_lines=28)
                        status = gr.Markdown("")
                        with gr.Row():
                            revert = gr.Button("↺ 원본으로 되돌리기")
                            save = gr.Button("💾 저장", variant="primary")
                        save.click(lambda text, k=key: save_prompt(k, text), box, status)
                        revert.click(lambda k=key: read_prompt(k), None, box)
            gr.Markdown("user_prompt.md는 코드가 테이블마다 값을 채우는 틀이라 편집 대상에서 제외했습니다. "
                        "저장 시 이전 버전은 prompts/backup/에 보관됩니다.")

        # ── ③ 실행 · 결과 ──
        with gr.Tab("③ 실행 · 결과") as run_tab:
            full_result = gr.State(pd.DataFrame())
            with gr.Row():
                with gr.Column(scale=1):
                    ready = gr.Markdown(readiness())
                    workers = gr.Slider(1, 16, value=app.WORKERS, step=1, label="동시 처리 테이블 수 (WORKERS)")
                    run_btn = gr.Button("▶ 실행", variant="primary", size="lg")
                    stop_btn = gr.Button("■ 중지")
                    result_file = gr.File(label="결과 파일 (xlsx)", interactive=False)
                with gr.Column(scale=3):
                    bar = gr.HTML(progress_view()[0])
                    stats = gr.Markdown(progress_view()[1])
                    log_box = gr.Textbox(label="실행 로그", lines=10, max_lines=10, autoscroll=True)
                    with gr.Row():
                        decision_filter = gr.Dropdown(["전체", "sensitive", "non-sensitive", "error"],
                                                      value="전체", label="decision")
                        review_only = gr.Checkbox(label="검토 필요만 보기")
                    preview = gr.Dataframe(label="결과 미리보기 (행을 누르면 상세)", interactive=False, wrap=True)
                    detail = gr.Markdown()

            run_tab.select(readiness, None, ready)
            run_btn.click(start_run, workers, [bar, stats, log_box, full_result, preview, result_file])
            stop_btn.click(stop_run)
            for ctl in (decision_filter, review_only):
                ctl.change(filter_result, [full_result, decision_filter, review_only], preview)
            preview.select(show_detail, [full_result, preview], detail)
    return demo


def launch():
    handler = UILogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger().addHandler(handler)
    build().launch(inbrowser=True)          # 실행하면 브라우저가 자동으로 열린다
