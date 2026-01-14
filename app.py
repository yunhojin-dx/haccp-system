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
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"

def upload_photo(task_id: str, uploaded_file) -> dict:
    raw = uploaded_file.read()
    compressed, ext = compress_image(raw, max_w=1400, quality=82)
    key = f"{task_id}/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.{ext}"

    sb.storage.from_(BUCKET).upload(
        path=key,
        file=compressed,
        file_options={"content-type": "image/jpeg", "upsert": "false"},
    )
    url = make_public_url(BUCKET, key)
    row = {"task_id": task_id, "storage_path": key, "public_url": url}
    sb.table("haccp_task_photos").insert(row).execute()
    return row

def delete_photo(photo_id: str, storage_path: str):
    try:
        sb.storage.from_(BUCKET).remove([storage_path])
    except Exception:
        pass
    sb.table("haccp_task_photos").delete().eq("id", photo_id).execute()

def delete_task_entirely(task_id: str, photos: list):
    if photos:
        paths = [p.get("storage_path") for p in photos if p.get("storage_path")]
        if paths:
            try:
                sb.storage.from_(BUCKET).remove(paths)
            except:
                pass 
    sb.table("haccp_tasks").delete().eq("id", task_id).execute()


# =========================================================
# 5) DB 함수
# =========================================================
def fetch_photos_for_tasks(task_ids: list[str]) -> dict:
    if not task_ids:
        return {}
    try:
        res = sb.table("haccp_task_photos").select("*").in_("task_id", task_ids).execute()
        photos = res.data or []
        photo_map = {}
        for p in photos:
            tid = p["task_id"]
            if "id" in p and "photo_id" not in p:
                p["photo_id"] = p["id"]
            if tid not in photo_map:
                photo_map[tid] = []
            photo_map[tid].append(p)
        return photo_map
    except Exception:
        return {}

def fetch_tasks(date_from: date | None = None, date_to: date | None = None) -> list[dict]:
    q = sb.table("haccp_tasks").select("*").order("issue_date", desc=True).order("created_at", desc=True)
    if date_from:
        q = q.gte("issue_date", str(date_from))
    if date_to:
        q = q.lte("issue_date", str(date_to))
    try:
        res = q.execute()
        tasks = res.data or []
    except Exception as e:
        st.error(f"데이터 조회 실패: {e}")
        return []

    if not tasks:
        return []

    t_ids = [t["id"] for t in tasks]
    photo_map = fetch_photos_for_tasks(t_ids)

    for t in tasks:
        t["photos"] = photo_map.get(t["id"], [])

    return tasks

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
# 6) 엑셀 내보내기 (텍스트 중앙 정렬 + 디자인 개선)
# =========================================================
def download_image_to_temp(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception:
        return None

def export_excel(tasks: list[dict]) -> bytes:
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
        sheet_data = "데이터"
        
        # [중요] header=False로 하여 to_excel이 만드는 제목을 뺍니다.
        df.to_excel(writer, sheet_name=sheet_data, index=False, startrow=1, header=False)
        
        wb = writer.book
        ws = writer.sheets[sheet_data]

        # 1. 헤더 스타일 (진하게, 회색 배경, 중앙 정렬, 테두리)
        header_fmt = wb.add_format({
            "bold": True, 
            "bg_color": "#EFEFEF", 
            "border": 1, 
            "align": "center", 
            "valign": "vcenter"
        })
        
        # 2. 데이터 셀 스타일 (중앙 정렬 핵심!, 줄바꿈, 테두리)
        cell_fmt = wb.add_format({
            "align": "center",    # 가로 가운데
            "valign": "vcenter",  # 세로 가운데 (사진 때문에 행이 높아지므로 필수)
            "text_wrap": True,    # 내용이 길면 줄바꿈
            "border": 1           # 모든 셀에 테두리
        })
        
        # 헤더 수동 작성 (0번 행)
        for col, name in enumerate(df.columns):
            ws.write(0, col, name, header_fmt)
            
        # 열 너비 및 포맷 설정 (여기서 cell_fmt를 적용해야 모든 데이터가 가운데로 옴)
        ws.set_column(0, 0, 30, cell_fmt)  # ID
        ws.set_column(1, 1, 12, cell_fmt)  # 일시
        ws.set_column(2, 2, 15, cell_fmt)  # 장소
        ws.set_column(3, 3, 40, cell_fmt)  # 필요사항
        ws.set_column(4, 10, 15, cell_fmt) # 나머지 컬럼들

        # 사진 컬럼 추가 설정
        img_cols = ["사진1", "사진2", "사진3"]
        base_col = len(df.columns)
        for i, c in enumerate(img_cols):
            ws.write(0, base_col + i, c, header_fmt)
            ws.set_column(base_col + i, base_col + i, 22, cell_fmt) # 사진 컬럼에도 포맷 적용

        # 행 높이 설정 (사진 공간 확보)
        for r in range(1, len(df) + 1):
            ws.set_row(r, 100)

        # 사진 삽입 로직
        for idx, t in enumerate(tasks):
            photos = t.get("photos") or []
            if not isinstance(photos, list): photos = []
            photos = photos[:3]

            for j, p in enumerate(photos):
                url = p.get("public_url")
                if not url: continue
                
                img_path = download_image_to_temp(url)
                if not img_path: continue
                
                try:
                    # 이미지 크기 자동 계산 (셀 안으로 쏙 들어가게)
                    with Image.open(img_path) as img:
                        w, h = img.size
                        
                    # 엑셀 셀 크기 기준 (약 150x133 픽셀)
                    target_w = 150
                    target_h = 130
                    
                    scale_w = target_w / w
                    scale_h = target_h / h
                    scale = min(scale_w, scale_h) * 0.9 # 90% 크기로 약간 여백 주기
                    
                    # 엑셀은 0번 행이 헤더이므로 데이터는 idx+1 행부터 시작
                    ws.insert_image(idx + 1, base_col + j, img_path, {
                        "x_scale": scale, 
                        "y_scale": scale, 
                        "object_position": 1
                    })
                except: pass

        # 요약 시트 작성
        sheet_sum = "요약"
        ws2 = wb.add_worksheet(sheet_sum)
        total = len(tasks)
        done = sum(1 for t in tasks if t.get("status") == "완료")
        rate = (done / total * 100) if total else 0.0
        
        # 요약 시트 스타일
        title_fmt = wb.add_format({"bold": True, "font_size": 16})
        
        ws2.write(0, 0, "HACCP 개선 보고서", title_fmt)
        ws2.write(2, 0, "총 발굴건수"); ws2.write(2, 1, total)
        ws2.write(3, 0, "개선완료 건수"); ws2.write(3, 1, done)
        ws2.write(4, 0, "완료율(%)"); ws2.write(4, 1, round(rate, 1))

    return out.getvalue()

def display_task_photos(t):
    photos = t.get("photos") or []
    if not isinstance(photos, list): photos = []
    
    if photos:
        st.markdown("📸 **현장 사진**")
        cols = st.columns(4) 
        for i, p in enumerate(photos):
            with cols[i % 4]:
                st.image(p.get("public_url"), use_container_width=True)
    else:
        st.caption("등록된 사진이 없습니다.")
    return photos


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
# (A) 대시보드/보고서
# ---------------------------------------------------------
with tabs[0]:
    st.subheader("대시보드/보고서")

    c1, c2, c3 = st.columns([1.2, 1.2, 2])
    today = date.today()

    with c1:
        period_type = st.selectbox("기간 단위", ["월간", "주간", "연간", "직접선택"], index=0)

    if period_type == "월간":
        base = st.date_input("기준월(아무 날짜)", value=today)
        d_from = start_of_month(base)
        d_to = end_of_month(base)
    
    elif period_type == "주간":
        base = st.date_input("기준일", value=today)
        d_from = start_of_week(base)
        d_to = end_of_week(base)
        
    elif period_type == "연간":
        with c2:
            base_year = st.number_input("조회 연도", min_value=2020, max_value=2030, value=today.year, step=1)
        d_from = date(base_year, 1, 1)
        d_to = date(base_year, 12, 31)
        
    else: # 직접선택
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
        df_loc = pd.DataFrame([{
            "공정/장소": (t.get("location") or "미분류").strip(),
            "상태": t.get("status")
        } for t in tasks])

        st.markdown("#### 공정/장소별 발굴 vs 완료")
        if not df_loc.empty:
            loc_pivot = (
                df_loc.assign(발굴=1, 완료=(df_loc["상태"] == "완료").astype(int))
                .groupby("공정/장소", as_index=False)[["발굴", "완료"]].sum()
            )
            loc_long = loc_pivot.melt("공정/장소", var_name="구분", value_name="건수")

            chart1 = alt.Chart(loc_long).mark_bar().encode(
                x=alt.X("공정/장소:N", sort="-y", title="장소", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("건수:Q", title="건수"),
                color=alt.Color("구분:N", scale=alt.Scale(domain=['발굴', '완료'], range=['#FF9F36', '#2ECC71'])),
                xOffset="구분:N",
                tooltip=["공정/장소", "구분", "건수"]
            ).properties(height=360)
            st.altair_chart(chart1, use_container_width=True)

        st.markdown("#### 일자별 추이 (발생일 기준)")
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
    st.markdown("#### 엑셀 보고서 다운로드")
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
                st.error(f"등록 실패: {e}")
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
        
        st.divider()
        st.markdown(f"**📍 장소:** {t.get('location')}  /  **📝 내용:** {t.get('issue_text')}")
        
        display_task_photos(t)
        st.divider()

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

        st.divider()
        st.markdown(f"**📍 장소:** {t.get('location')}  /  **📝 내용:** {t.get('issue_text')}")
        
        display_task_photos(t)
        st.divider()

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
        st.markdown("#### 상세 관리 / 삭제")
        opts = [f"{t.get('issue_date')} | {t.get('location')} | {t.get('issue_text')}" for t in filtered]
        s = st.selectbox("과제 선택", opts)
        target = filtered[opts.index(s)]
        
        c_info, c_del = st.columns([3, 1])
        with c_info:
             st.markdown(f"**내용:** {target.get('issue_text')}")
             st.markdown(f"**담당:** {target.get('assignee') or '-'} | **완료일:** {target.get('action_done_date') or '-'}")
        
        with c_del:
            st.write("") 
            if st.button("🚨 과제 전체 삭제 (복구 불가)", type="primary"):
                delete_task_entirely(target["id"], target.get("photos"))
                st.success("삭제되었습니다. (새로고침 중...)")
                st.rerun()

        current_photos = display_task_photos(target)
        
        if current_photos:
            with st.expander("🗑 개별 사진만 삭제하려면 클릭하세요"):
                cols = st.columns(3)
                for i, p in enumerate(current_photos):
                    with cols[i%3]:
                        st.image(p.get("public_url"), width=100)
                        if st.button("삭제", key=f"d_{p.get('photo_id')}"):
                            delete_photo(p.get("photo_id"), p.get("storage_path"))
                            st.rerun()
        
        st.divider()
        st.write("📸 **사진 추가 등록**")
        add_f = st.file_uploader("", accept_multiple_files=True, key="add_p")
        if st.button("업로드 추가"):
            for f in add_f:
                upload_photo(target["id"], f)
            st.success("완료")
            st.rerun()
