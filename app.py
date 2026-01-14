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
# 0) 기본 UI 설정
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
# 2) Supabase 연결
# =========================================================
@st.cache_resource
def get_supabase():
    # service role 키 사용
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

sb = get_supabase()


# =========================================================
# 3) 유틸: 날짜/기간 계산
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
    # Supabase public bucket URL 생성
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


# =========================================================
# 6) 엑셀(사진 포함) 내보내기 기능
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

def export_excel(tasks: list[dict]) -> bytes:
    # 데이터프레임 생성
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
        header_fmt = wb.add_format({"bold": True, "bg_color": "#EFEFEF", "border": 1, "align": "center", "valign": "vcenter"})
        
        for col, name in enumerate(df.columns):
            ws.write(0, col, name, header_fmt)

        # 열 폭 설정
        ws.set_column(0, 0, 30)  # ID
        ws.set_column(1, 1, 12)  # 일시
        ws.set_column(2, 2, 15)  # 장소
        ws.set_column(3, 3, 40)  # 필요사항
        ws.set_column(4, 10, 15) # 나머지

        # 사진 칼럼 추가
        img_cols = ["사진1", "사진2", "사진3"]
        base_col = len(df.columns)
        for i, c in enumerate(img_cols):
            ws.write(0, base_col + i, c, header_fmt)
            ws.set_column(base_col + i, base_col + i, 20)

        # 행 높이 설정 (사진 공간)
        for r in range(1, len(df) + 1):
            ws.set_row(r, 100)

        # 사진 삽입
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
                if not url: continue
                
                img_path = download_image_to_temp(url)
                if not img_path: continue
                
                row = idx + 1
                col = base_col + j
                try:
                    ws.insert_image(row, col, img_path, {"x_scale": 0.2, "y_scale": 0.2, "object_position": 1})
                except:
                    pass

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

    return out.getvalue()


# =========================================================
# 7) 메인 화면: 탭 구성
# =========================================================
tabs = st.tabs([
    "대시보드/보고서",
    "개선과제등록",
    "개선계획수립",
    "개선완료 입력",
    "조회/관리",
])

# ---------------------------------------------------------
# (A) 대시보드/보고서 (수정됨: 그래프 레이블 가로 방향)
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

    if total == 0:
        st.info("선택한 기간에 데이터가 없습니다.")
    else:
        # 데이터프레임 생성
        df_loc = pd.DataFrame([{
            "공정/장소": (t.get("location") or "미분류").strip(),
            "상태": t.get("status")
        } for t in tasks])

        # 1. 장소별 차트 (레이블 가로 방향 적용)
        st.markdown("#### 공정/장소별 발굴 vs 완료")
        if not df_loc.empty:
            loc_pivot = (
                df_loc.assign(발굴=1, 완료=(df_loc["상태"] == "완료").astype(int))
                .groupby("공정/장소", as_index=False)[["발굴", "완료"]].sum()
            )
            loc_long = loc_pivot.melt("공정/장소", var_name="구분", value_name="건수")

            chart1 = alt.Chart(loc_long).mark_bar().encode(
                # 👇 여기 labelAngle=0 추가됨 (가로로 보이게)
                x=alt.X("공정/장소:N", sort="-y", title="장소", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("건수:Q", title="건수"),
                color=alt.Color("구분:N", scale=alt.Scale(domain=['발굴', '완료'], range=['#FF9F36', '#2ECC71'])),
                xOffset="구분:N",
                tooltip=["공정/장소", "구분", "건수"]
            ).properties(height=360)
            st.altair_chart(chart1, use_container_width=True)

        # 2. 날짜별 차트
        st.markdown("#### 일자별 추이")
        df_day = pd.DataFrame([{
            "일자": t.get("issue_date"),
            "발굴": 1,
            "완료": 1 if t.get("status") == "완료" else 0
        } for t in tasks])
        
        if not df_day.empty:
            df_day["일자"] = pd.to_datetime(df_day["일자"])
            day_pivot = df_day.groupby("일자", as_index=False)[["발굴", "완료"]].sum().sort_values("일자")
            
            day_long = day_pivot.melt("일자", var_name="구분", value_name="건수")

            chart2 = alt.Chart(day_long).mark_line(point=True).encode(
                x=alt.X("일자:T", title="날짜", axis=alt.Axis(format="%m-%d")),
                y=alt.Y("건수:Q", title="건수"),
                color=alt.Color("구분:N", scale=alt.Scale(domain=['발굴', '완료'], range=['#FF9F36', '#2ECC71'])),
                tooltip=[alt.Tooltip("일자:T", format="%Y-%m-%d"), "구분", "건수"]
            ).properties(height=320)
            st.altair_chart(chart2, use_container_width=True)

    st.divider()
    st.markdown("#### 엑셀 보고서 다운로드 (사진 포함)")
    if st.button("📥 엑셀로 다운로드 (사진 포함)", type="primary"):
        with st.spinner("사진을 엑셀에 심는 중입니다..."):
            xbytes = export_excel(tasks)
            fn = f"HACCP_보고서_{d_from}_{d_to}.xlsx"
            st.download_button("⬇️ 파일 저장하기", data=xbytes, file_name=fn, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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
        issue_text = st.text_area("개선 필요사항", placeholder="구체적 작성", height=120)
        st.caption("사진은 여러 장 업로드 가능")
        photos = st.file_uploader("사진 업로드", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)
        submitted = st.form_submit_button("✅ 등록하기", type="primary")

    if submitted:
        if not (location.strip() and reporter.strip() and issue_text.strip()):
            st.error("필수 항목 누락")
        else:
            try:
                task_id = insert_task(issue_date, location, issue_text, reporter)
                if photos:
                    for f in photos:
                        upload_photo(task_id, f)
                st.success("등록 완료! 조회 탭에서 확인하세요.")
            except Exception as e:
                st.error("등록 실패")
                st.exception(e)


# ---------------------------------------------------------
# (C) 개선계획수립
# ---------------------------------------------------------
with tabs[2]:
    st.subheader("개선계획수립")
    tasks = fetch_tasks(None, None)
    if not tasks:
        st.info("과제 없음")
    else:
        options = [f"{t.get('issue_date')} | {t.get('location')} | {t.get('issue_text')[:30]}... ({t.get('status')})" for t in tasks]
        sel = st.selectbox("대상 선택", options, index=0)
        t = tasks[options.index(sel)]
        
        st.info(f"선택: {t.get('issue_text')}")
        with st.form("form_plan"):
            assignee = st.text_input("담당자", value=t.get("assignee") or "")
            plan_due = st.date_input("계획일정", value=pd.to_datetime(t.get("plan_due")).date() if t.get("plan_due") else date.today())
            plan_text = st.text_area("계획내용", value=t.get("plan_text") or "")
            if st.form_submit_button("💾 저장"):
                update_task(t["id"], {
                    "assignee": assignee,
                    "plan_due": str(plan_due),
                    "plan_text": plan_text
                })
                st.success("저장 완료")


# ---------------------------------------------------------
# (D) 개선완료 입력
# ---------------------------------------------------------
with tabs[3]:
    st.subheader("개선완료 입력")
    tasks = fetch_tasks(None, None)
    if not tasks:
        st.info("과제 없음")
    else:
        options = [f"{t.get('issue_date')} | {t.get('location')} | {t.get('issue_text')[:30]}... ({t.get('status')})" for t in tasks]
        sel = st.selectbox("대상 선택", options, index=0, key="done_sel")
        t = tasks[options.index(sel)]

        st.info(f"선택: {t.get('issue_text')}")
        with st.form("form_done"):
            action_text = st.text_area("조치내용", value=t.get("action_text") or "")
            action_done_date = st.date_input("완료일", value=pd.to_datetime(t.get("action_done_date")).date() if t.get("action_done_date") else date.today())
            if st.form_submit_button("✅ 완료 저장"):
                update_task(t["id"], {
                    "action_text": action_text,
                    "action_done_date": str(action_done_date),
                    "status": "완료"
                })
                st.success("저장 완료")


# ---------------------------------------------------------
# (E) 조회/관리
# ---------------------------------------------------------
with tabs[4]:
    st.subheader("조회/관리")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    with c1: d_from = st.date_input("시작", value=date.today()-timedelta(days=30), key="s_from")
    with c2: d_to = st.date_input("종료", value=date.today(), key="s_to")
    with c3: st_flt = st.selectbox("상태", ["전체", "진행중", "완료"])
    with c4: kw = st.text_input("검색어")

    tasks = fetch_tasks(d_from, d_to)
    
    # 필터링
    filtered = []
    for t in tasks:
        if st_flt != "전체" and t.get("status") != st_flt: continue
        if kw:
            full_str = f"{t.get('location')} {t.get('issue_text')} {t.get('reporter')}".lower()
            if kw.lower() not in full_str: continue
        filtered.append(t)

    if not filtered:
        st.warning("데이터가 없습니다.")
    else:
        df = pd.DataFrame([{
            "일시": t.get("issue_date"),
            "장소": t.get("location"),
            "내용": t.get("issue_text"),
            "상태": t.get("status")
        } for t in filtered])
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("#### 상세 관리 (사진 삭제/추가)")
        opts = [f"{t.get('issue_date')} | {t.get('location')} | {t.get('issue_text')}" for t in filtered]
        s = st.selectbox("과제 선택", opts)
        target = filtered[opts.index(s)]
        
        # 사진 관리
        photos = target.get("photos") or []
        if isinstance(photos, str): photos = json.loads(photos)
        
        if photos:
            cols = st.columns(3)
            for i, p in enumerate(photos):
                with cols[i%3]:
                    st.image(p.get("public_url"), use_container_width=True)
                    if st.button("🗑 삭제", key=f"d_{p.get('photo_id')}"):
                        delete_photo(p.get("photo_id"), p.get("storage_path"))
                        st.rerun()
        
        add_f = st.file_uploader("사진 추가", accept_multiple_files=True, key="add_p")
        if st.button("업로드 추가"):
            for f in add_f:
                upload_photo(target["id"], f)
            st.success("완료")
            st.rerun()
