import os
import io
import json
import uuid
import math
import tempfile
from datetime import date, datetime, timedelta

import requests
import pandas as pd
import streamlit as st
import altair as alt
from PIL import Image

from supabase import create_client


# =========================================================
# 0) 기본 UI
# =========================================================
st.set_page_config(page_title="천안공장 HACCP 개선관리", layout="wide")

st.markdown("""
<style>
.small-muted {color:#666; font-size:12px;}
.badge {display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; background:#f2f2f2;}
</style>
""", unsafe_allow_html=True)

st.title("천안공장 HACCP 개선관리")


# =========================================================
# 1) Secrets 체크
# =========================================================
REQUIRED_SECRETS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET"]
missing = [k for k in REQUIRED_SECRETS if k not in st.secrets or not str(st.secrets.get(k, "")).strip()]
if missing:
    st.error(f"🚨 Secrets 누락: {', '.join(missing)}")
    st.info("Streamlit → App Settings → Secrets 에 TOML 형식으로 등록해 주세요.")
    st.stop()

SUPABASE_URL = st.secrets["SUPABASE_URL"].strip()
SUPABASE_ANON_KEY = st.secrets["SUPABASE_ANON_KEY"].strip()
SUPABASE_SERVICE_KEY = st.secrets["SUPABASE_SERVICE_KEY"].strip()
BUCKET = st.secrets["SUPABASE_BUCKET"].strip()


# =========================================================
# 2) Supabase 연결 (가장 안정적으로: service_role 사용)
# =========================================================
@st.cache_resource
def get_supabase():
    # service role 키로 서버 사이드에서만 사용(스트림릿 시크릿)
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

sb = get_supabase()


# =========================================================
# 3) 유틸: 날짜/기간
# =========================================================
def start_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())

def end_of_week(d: date) -> date:
    return start_of_week(d) + timedelta(days=6)

def start_of_month(d: date) -> date:
    return d.replace(day=1)

def end_of_month(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


# =========================================================
# 4) 유틸: 이미지 리사이즈/압축 + 업로드
# =========================================================
def compress_image(file_bytes: bytes, max_w=1280, quality=80) -> tuple[bytes, str]:
    """
    return: (compressed_bytes, ext)
    - 업로드는 jpg로 통일 (용량/호환 안정)
    """
    img = Image.open(io.BytesIO(file_bytes))
    img = img.convert("RGB")
    w, h = img.size
    if w > max_w:
        new_h = int(h * (max_w / w))
        img = img.resize((max_w, new_h))

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), "jpg"

def make_public_url(bucket: str, path: str) -> str:
    # Supabase public bucket 기준
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"

def upload_photo(task_id: str, uploaded_file) -> dict:
    raw = uploaded_file.read()
    compressed, ext = compress_image(raw, max_w=1400, quality=82)

    # 경로: task_id/날짜_uuid.jpg
    key = f"{task_id}/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.{ext}"

    # 업로드
    sb.storage.from_(BUCKET).upload(
        path=key,
        file=compressed,
        file_options={"content-type": "image/jpeg", "upsert": "false"},
    )

    url = make_public_url(BUCKET, key)

    # DB 기록
    row = {
        "task_id": task_id,
        "storage_path": key,
        "public_url": url
    }
    sb.table("haccp_task_photos").insert(row).execute()
    return row

def delete_photo(photo_id: str, storage_path: str):
    # storage 삭제
    try:
        sb.storage.from_(BUCKET).remove([storage_path])
    except Exception:
        # storage 삭제 실패해도 DB는 지울 수 있게
        pass
    # DB 삭제
    sb.table("haccp_task_photos").delete().eq("id", photo_id).execute()


# =========================================================
# 5) DB 함수 (tasks)
# =========================================================
def fetch_tasks(date_from: date | None = None, date_to: date | None = None) -> list[dict]:
    q = sb.table("v_haccp_tasks").select("*").order("issue_date", desc=True).order("created_at", desc=True)
    if date_from:
        q = q.gte("issue_date", str(date_from))
    if date_to:
        q = q.lte("issue_date", str(date_to))
    res = q.execute()
    return res.data or []

def fetch_task(task_id: str) -> dict | None:
    res = sb.table("v_haccp_tasks").select("*").eq("id", task_id).limit(1).execute()
    data = res.data or []
    return data[0] if data else None

def insert_task(issue_date: date, location: str, issue_text: str, reporter: str) -> str:
    row = {
        "issue_date": str(issue_date),
        "location": location.strip(),
        "issue_text": issue_text.strip(),
        "reporter": reporter.strip(),
        "status": "진행중"
    }
    res = sb.table("haccp_tasks").insert(row).execute()
    return res.data[0]["id"]

def update_task(task_id: str, patch: dict):
    sb.table("haccp_tasks").update(patch).eq("id", task_id).execute()

def mark_done_if_action_exists(task_id: str):
    t = fetch_task(task_id)
    if not t:
        return
    if (t.get("action_text") or "").strip():
        update_task(task_id, {"status": "완료"})
    else:
        # action 지우면 상태를 자동으로 되돌리진 않음(혼란 방지)
        pass


# =========================================================
# 6) 엑셀(사진 포함) 내보내기
# =========================================================
def download_image_to_temp(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception:
        return None

def export_excel(tasks: list[dict], filename_prefix="HACCP_보고서") -> bytes:
    # 데이터프레임
    rows = []
    for t in tasks:
        rows.append({
            "ID": t.get("legacy_id") or t["id"],
            "일시": t.get("issue_date"),
            "공정/장소": t.get("location"),
            "개선 필요사항": t.get("issue_text"),
            "발견자": t.get("reporter"),
            "진행상태": t.get("status"),
            "담당자": t.get("assignee"),
            "개선계획(일정)": t.get("plan_due"),
            "개선계획(내용)": t.get("plan_text"),
            "개선내용": t.get("action_text"),
            "개선완료일": t.get("action_done_date"),
        })
    df = pd.DataFrame(rows)

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        # 1) 데이터 시트
        sheet_data = "데이터"
        df.to_excel(writer, sheet_name=sheet_data, index=False, startrow=1)
        wb = writer.book
        ws = writer.sheets[sheet_data]

        # 헤더 스타일
        header_fmt = wb.add_format({"bold": True, "bg_color": "#EFEFEF", "border": 1})
        for col, name in enumerate(df.columns):
            ws.write(0, col, name, header_fmt)

        # 열 폭
        ws.set_column(0, 0, 36)  # ID
        ws.set_column(1, 1, 12)  # 일시
        ws.set_column(2, 2, 16)  # 장소
        ws.set_column(3, 3, 40)  # 필요사항
        ws.set_column(4, 4, 14)  # 발견자
        ws.set_column(5, 5, 10)  # 상태
        ws.set_column(6, 6, 14)  # 담당자
        ws.set_column(7, 7, 14)  # 계획일정
        ws.set_column(8, 8, 28)  # 계획내용
        ws.set_column(9, 9, 28)  # 개선내용
        ws.set_column(10, 10, 14) # 완료일

        # 사진 칼럼 3개 추가
        img_cols = ["사진1", "사진2", "사진3"]
        base_col = len(df.columns)
        for i, c in enumerate(img_cols):
            ws.write(0, base_col + i, c, header_fmt)
            ws.set_column(base_col + i, base_col + i, 22)

        # 행 높이(사진 들어갈 공간)
        for r in range(1, len(df) + 1):
            ws.set_row(r, 120)

        # 사진 삽입(최대 3장)
        for idx, t in enumerate(tasks):
            photos = t.get("photos") or []
            try:
                if isinstance(photos, str):
                    photos = json.loads(photos)
            except Exception:
                photos = []
            photos = photos[:3]

            for j, p in enumerate(photos):
                url = p.get("public_url")
                if not url:
                    continue
                img_path = download_image_to_temp(url)
                if not img_path:
                    continue
                # 행/열 위치
                row = idx + 1
                col = base_col + j
                ws.insert_image(row, col, img_path, {"x_scale": 0.35, "y_scale": 0.35})
                # 임시파일 삭제는 xlsxwriter가 참조할 수 있으므로 저장 후 정리하는 게 정석이지만,
                # 스트림릿 환경에서 문제를 줄이기 위해 여기선 남겨둠(서버 임시영역).

        # 2) 요약 시트
        sheet_sum = "요약"
        ws2 = wb.add_worksheet(sheet_sum)

        total = len(tasks)
        done = sum(1 for t in tasks if t.get("status") == "완료")
        rate = (done / total * 100) if total else 0.0

        ws2.write(0, 0, "HACCP 개선 보고서", wb.add_format({"bold": True, "font_size": 16}))
        ws2.write(2, 0, "총 발굴건수"); ws2.write(2, 1, total)
        ws2.write(3, 0, "개선완료 건수"); ws2.write(3, 1, done)
        ws2.write(4, 0, "완료율(%)"); ws2.write(4, 1, round(rate, 1))

        # 장소별 집계 표
        loc = {}
        for t in tasks:
            k = (t.get("location") or "미분류").strip()
            loc.setdefault(k, {"발굴": 0, "완료": 0})
            loc[k]["발굴"] += 1
            if t.get("status") == "완료":
                loc[k]["완료"] += 1

        ws2.write(6, 0, "공정/장소"); ws2.write(6, 1, "발굴"); ws2.write(6, 2, "완료")
        r0 = 7
        for i, (k, v) in enumerate(sorted(loc.items(), key=lambda x: x[0])):
            ws2.write(r0 + i, 0, k)
            ws2.write(r0 + i, 1, v["발굴"])
            ws2.write(r0 + i, 2, v["완료"])

        ws2.set_column(0, 0, 22)
        ws2.set_column(1, 2, 10)

    return out.getvalue()


# =========================================================
# 7) 화면: 탭 구성
# =========================================================
tabs = st.tabs([
    "대시보드/보고서",
    "개선과제등록",
    "개선계획수립",
    "개선완료 입력",
    "조회/관리",
])

# ---------------------------------------------------------
# (A) 대시보드/보고서
# ---------------------------------------------------------
with tabs[0]:
    st.subheader("대시보드/보고서")

    c1, c2, c3 = st.columns([1.2, 1.2, 2])

    with c1:
        period_type = st.selectbox("기간 단위", ["주간", "월간", "직접선택"], index=0)

    today = date.today()
    if period_type == "주간":
        base = st.date_input("기준일", value=today)
        d_from = start_of_week(base)
        d_to = end_of_week(base)
    elif period_type == "월간":
        base = st.date_input("기준월(아무 날짜)", value=today)
        d_from = start_of_month(base)
        d_to = end_of_month(base)
    else:
        with c2:
            d_from = st.date_input("시작일", value=today - timedelta(days=30))
        with c3:
            d_to = st.date_input("종료일", value=today)

    tasks = fetch_tasks(d_from, d_to)

    total = len(tasks)
    done = sum(1 for t in tasks if t.get("status") == "완료")
    rate = (done / total * 100) if total else 0.0

    m1, m2, m3 = st.columns(3)
    m1.metric("총 발굴건수", total)
    m2.metric("개선완료 건수", done)
    m3.metric("완료율", f"{rate:.1f}%")

    # 장소별
    df_loc = pd.DataFrame([{
        "공정/장소": (t.get("location") or "미분류").strip(),
        "상태": t.get("status")
    } for t in tasks])

    if total == 0:
        st.info("선택한 기간에 데이터가 없습니다.")
    else:
        loc_pivot = (
            df_loc.assign(발굴=1, 완료=(df_loc["상태"] == "완료").astype(int))
            .groupby("공정/장소", as_index=False)[["발굴", "완료"]].sum()
        )

        st.markdown("#### 공정/장소별 발굴 vs 완료")
        chart1 = alt.Chart(loc_pivot).transform_fold(
            ["발굴", "완료"], as_=["구분", "건수"]
        ).mark_bar().encode(
            x=alt.X("공정/장소:N", sort="-y"),
            y="건수:Q",
            xOffset="구분:N",
            tooltip=["공정/장소", "구분", "건수"]
        ).properties(height=360)

        st.altair_chart(chart1, use_container_width=True)

        # 날짜별 추이
        df_day = pd.DataFrame([{
            "일자": t.get("issue_date"),
            "발굴": 1,
            "완료": 1 if t.get("status") == "완료" else 0
        } for t in tasks])
        df_day["일자"] = pd.to_datetime(df_day["일자"])
        df_day = df_day.groupby("일자", as_index=False)[["발굴", "완료"]].sum().sort_values("일자")

        st.markdown("#### 일자별 추이")
        chart2 = alt.Chart(df_day).transform_fold(
            ["발굴", "완료"], as_=["구분", "건수"]
        ).mark_line(point=True).encode(
            x="일자:T",
            y="건수:Q",
            color="구분:N",
            tooltip=["일자:T", "구분:N", "건수:Q"]
        ).properties(height=320)

        st.altair_chart(chart2, use_container_width=True)

        st.divider()
        st.markdown("#### 엑셀 보고서 다운로드 (사진 포함)")
        if st.button("📥 엑셀로 다운로드", type="primary"):
            xbytes = export_excel(tasks)
            fn = f"HACCP_보고서_{d_from}_{d_to}.xlsx"
            st.download_button("⬇️ 다운로드", data=xbytes, file_name=fn, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------
# (B) 개선과제등록
# ---------------------------------------------------------
with tabs[1]:
    st.subheader("개선과제등록 (발굴/등록)")

    with st.form("form_register", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            issue_date = st.date_input("일시", value=date.today())
        with col2:
            location = st.text_input("공정/장소", placeholder="예: 전처리실")
        with col3:
            reporter = st.text_input("발견자", placeholder="예: 품질보증팀")

        issue_text = st.text_area("개선 필요사항", placeholder="무엇이 문제인지 구체적으로 작성", height=120)

        st.caption("사진은 여러 장 업로드 가능 (자동 리사이즈/압축 후 저장)")
        photos = st.file_uploader("사진 업로드", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)

        submitted = st.form_submit_button("✅ 등록하기", type="primary")

    if submitted:
        if not (location.strip() and reporter.strip() and issue_text.strip()):
            st.error("공정/장소, 발견자, 개선 필요사항은 필수입니다.")
        else:
            try:
                task_id = insert_task(issue_date, location, issue_text, reporter)

                # 사진 업로드
                if photos:
                    for f in photos:
                        upload_photo(task_id, f)

                st.success("등록 완료!")
                st.info("다음 탭에서 ‘개선계획수립 → 개선완료 입력’ 순서로 진행하세요.")
            except Exception as e:
                st.error("등록 실패")
                st.exception(e)


# ---------------------------------------------------------
# (C) 개선계획수립
# ---------------------------------------------------------
with tabs[2]:
    st.subheader("개선계획수립 (담당자/일정 지정)")

    tasks = fetch_tasks(None, None)
    if not tasks:
        st.info("등록된 개선과제가 없습니다.")
    else:
        options = [f"{t.get('issue_date')} | {t.get('location')} | {t.get('issue_text')[:30]}... ({t.get('status')})" for t in tasks]
        sel = st.selectbox("대상 선택", options, index=0)
        idx = options.index(sel)
        t = tasks[idx]

        st.write(f"**선택된 과제:** {t.get('issue_text')}")
        st.write(f"- 발견자: {t.get('reporter')}  /  상태: {t.get('status')}")

        with st.form("form_plan"):
            assignee = st.text_input("담당자", value=t.get("assignee") or "", placeholder="예: 생산팀/공무팀/홍길동")
            plan_due = st.date_input("개선계획(일정)", value=pd.to_datetime(t.get("plan_due")).date() if t.get("plan_due") else date.today())
            plan_text = st.text_area("개선계획(내용)", value=t.get("plan_text") or "", height=120)
            ok = st.form_submit_button("💾 저장", type="primary")

        if ok:
            try:
                update_task(t["id"], {
                    "assignee": assignee.strip() if assignee else None,
                    "plan_due": str(plan_due) if plan_due else None,
                    "plan_text": plan_text.strip() if plan_text else None,
                })
                st.success("저장 완료")
            except Exception as e:
                st.error("저장 실패")
                st.exception(e)


# ---------------------------------------------------------
# (D) 개선완료 입력
# ---------------------------------------------------------
with tabs[3]:
    st.subheader("개선완료 입력")

    tasks = fetch_tasks(None, None)
    if not tasks:
        st.info("등록된 개선과제가 없습니다.")
    else:
        options = [f"{t.get('issue_date')} | {t.get('location')} | {t.get('issue_text')[:30]}... ({t.get('status')})" for t in tasks]
        sel = st.selectbox("대상 선택", options, index=0, key="done_select")
        idx = options.index(sel)
        t = tasks[idx]

        st.write(f"**선택된 과제:** {t.get('issue_text')}")
        st.write(f"- 담당자: {t.get('assignee') or '-'}  /  계획일정: {t.get('plan_due') or '-'}")

        with st.form("form_done"):
            action_text = st.text_area("개선내용", value=t.get("action_text") or "", height=140)
            action_done_date = st.date_input(
                "개선완료일",
                value=pd.to_datetime(t.get("action_done_date")).date() if t.get("action_done_date") else date.today()
            )
            ok = st.form_submit_button("✅ 완료 저장", type="primary")

        if ok:
            try:
                update_task(t["id"], {
                    "action_text": action_text.strip() if action_text else None,
                    "action_done_date": str(action_done_date) if action_done_date else None,
                    "status": "완료" if (action_text or "").strip() else t.get("status", "진행중")
                })
                st.success("저장 완료")
            except Exception as e:
                st.error("저장 실패")
                st.exception(e)


# ---------------------------------------------------------
# (E) 조회/관리 (사진 삭제/추가, 상태 변경 등)
# ---------------------------------------------------------
with tabs[4]:
    st.subheader("조회/관리")

    f1, f2, f3, f4 = st.columns([1.1, 1.1, 1, 1.2])
    with f1:
        d_from = st.date_input("시작일", value=date.today() - timedelta(days=30), key="m_from")
    with f2:
        d_to = st.date_input("종료일", value=date.today(), key="m_to")
    with f3:
        status_filter = st.selectbox("상태", ["전체", "진행중", "완료"], index=0)
    with f4:
        keyword = st.text_input("검색(장소/내용/발견자)", value="")

    tasks = fetch_tasks(d_from, d_to)

    # 필터
    def match(t: dict) -> bool:
        if status_filter != "전체" and t.get("status") != status_filter:
            return False
        if keyword.strip():
            k = keyword.strip().lower()
            blob = " ".join([
                str(t.get("location") or ""),
                str(t.get("issue_text") or ""),
                str(t.get("reporter") or ""),
                str(t.get("assignee") or ""),
                str(t.get("action_text") or "")
            ]).lower()
            return k in blob
        return True

    tasks = [t for t in tasks if match(t)]

    st.caption(f"검색 결과: {len(tasks)}건")

    if not tasks:
        st.info("조건에 맞는 데이터가 없습니다.")
    else:
        # 목록 테이블
        df = pd.DataFrame([{
            "일시": t.get("issue_date"),
            "공정/장소": t.get("location"),
            "발견자": t.get("reporter"),
            "상태": t.get("status"),
            "담당자": t.get("assignee"),
            "계획일정": t.get("plan_due"),
            "완료일": t.get("action_done_date"),
            "요약": (t.get("issue_text") or "")[:40]
        } for t in tasks])
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("### 건별 상세")

        options = [f"{t.get('issue_date')} | {t.get('location')} | {(t.get('issue_text') or '')[:30]}... ({t.get('status')})" for t in tasks]
        sel = st.selectbox("상세로 볼 항목", options, index=0, key="detail_select")
        t = tasks[options.index(sel)]

        st.markdown(f"**개선 필요사항:** {t.get('issue_text')}")
        st.write(f"- 발견자: {t.get('reporter')}")
        st.write(f"- 담당자: {t.get('assignee') or '-'} / 계획일정: {t.get('plan_due') or '-'}")
        st.write(f"- 개선내용: {t.get('action_text') or '-'} / 완료일: {t.get('action_done_date') or '-'}")
        st.write(f"- 상태: **{t.get('status')}**")

        # 상태 강제 변경(원하면)
        cst1, cst2 = st.columns([1, 3])
        with cst1:
            new_status = st.selectbox("상태 변경", ["진행중", "완료"], index=0 if t.get("status") != "완료" else 1)
            if st.button("상태 저장"):
                try:
                    update_task(t["id"], {"status": new_status})
                    st.success("상태 저장 완료")
                except Exception as e:
                    st.error("상태 저장 실패")
                    st.exception(e)

        # 사진 표시/삭제
        photos = t.get("photos") or []
        try:
            if isinstance(photos, str):
                photos = json.loads(photos)
        except Exception:
            photos = []

        st.markdown("#### 사진")
        if not photos:
            st.info("등록된 사진이 없습니다.")
        else:
            cols = st.columns(3)
            for i, p in enumerate(photos):
                with cols[i % 3]:
                    st.image(p.get("public_url"), use_container_width=True)
                    if st.button("🗑 삭제", key=f"del_{p.get('photo_id')}"):
                        try:
                            delete_photo(p.get("photo_id"), p.get("storage_path"))
                            st.success("삭제 완료 (새로고침하면 반영)")
                            st.rerun()
                        except Exception as e:
                            st.error("삭제 실패")
                            st.exception(e)

        st.markdown("#### 사진 추가 업로드 (교체는: 삭제 후 다시 업로드)")
        add_files = st.file_uploader("추가할 사진", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="add_files")
        if st.button("📤 사진 추가 업로드"):
            if not add_files:
                st.warning("추가할 사진을 선택해 주세요.")
            else:
                try:
                    for f in add_files:
                        upload_photo(t["id"], f)
                    st.success("업로드 완료")
                    st.rerun()
                except Exception as e:
                    st.error("업로드 실패")
                    st.exception(e)
