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
# 0) 기본 UI 설정 (와이드 모드)
# =========================================================
st.set_page_config(page_title="천안공장 HACCP 개선관리", layout="wide")

# CSS로 여백 줄이기 (한 화면에 많이 보여주기 위함)
st.markdown("""
<style>
    .block-container {padding-top: 1rem; padding-bottom: 1rem;}
    .small-muted {color:#666; font-size:12px;}
</style>
""", unsafe_allow_html=True)

st.title("🏭 천안공장 HACCP 개선관리")


# =========================================================
# 1) Secrets 체크
# =========================================================
REQUIRED_SECRETS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET"]
missing = [k for k in REQUIRED_SECRETS if k not in st.secrets or not str(st.secrets.get(k, "")).strip()]
if missing:
    st.error(f"🚨 Secrets 누락: {', '.join(missing)}")
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
# 3) 유틸 함수들
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
    sb.storage.from_(BUCKET).upload(path=key, file=compressed, file_options={"content-type": "image/jpeg", "upsert": "false"})
    url = make_public_url(BUCKET, key)
    row = {"task_id": task_id, "storage_path": key, "public_url": url}
    sb.table("haccp_task_photos").insert(row).execute()
    return row

def delete_photo(photo_id: str, storage_path: str):
    try: sb.storage.from_(BUCKET).remove([storage_path])
    except: pass
    sb.table("haccp_task_photos").delete().eq("id", photo_id).execute()

def delete_task_entirely(task_id: str, photos: list):
    if photos:
        paths = [p.get("storage_path") for p in photos if p.get("storage_path")]
        if paths:
            try: sb.storage.from_(BUCKET).remove(paths)
            except: pass 
    sb.table("haccp_tasks").delete().eq("id", task_id).execute()

# [중요] 사진 정보 포함하여 과제 목록 가져오기 (DB 조회 최적화)
def fetch_tasks_all() -> list[dict]:
    # 1. 모든 과제 가져오기 (필터링은 Pandas에서 처리하여 다중 선택 지원)
    try:
        res = sb.table("haccp_tasks").select("*").order("issue_date", desc=True).execute()
        tasks = res.data or []
    except Exception as e:
        st.error(f"데이터 조회 실패: {e}")
        return []

    if not tasks: return []

    # 2. 모든 사진 가져오기
    t_ids = [t["id"] for t in tasks]
    if not t_ids: return tasks

    try:
        res_p = sb.table("haccp_task_photos").select("*").in_("task_id", t_ids).execute()
        photos = res_p.data or []
        
        # 사진 매핑
        photo_map = {}
        for p in photos:
            tid = p["task_id"]
            if "id" in p and "photo_id" not in p: p["photo_id"] = p["id"]
            if tid not in photo_map: photo_map[tid] = []
            photo_map[tid].append(p)
            
        for t in tasks:
            t["photos"] = photo_map.get(t["id"], [])
            
    except: pass # 사진 가져오기 실패해도 목록은 보여줌

    return tasks

def insert_task(issue_date, location, issue_text, reporter):
    row = {"issue_date": str(issue_date), "location": location.strip(), "issue_text": issue_text.strip(), "reporter": reporter.strip(), "status": "진행중"}
    res = sb.table("haccp_tasks").insert(row).execute()
    return res.data[0]["id"]

def update_task(task_id, patch):
    sb.table("haccp_tasks").update(patch).eq("id", task_id).execute()

# 엑셀 다운로드 (디자인 적용)
def download_image_to_temp(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        with open(path, "wb") as f: f.write(r.content)
        return path
    except: return None

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
        df.to_excel(writer, sheet_name=sheet_data, index=False, startrow=1, header=False)
        wb = writer.book
        ws = writer.sheets[sheet_data]
        
        header_fmt = wb.add_format({"bold": True, "bg_color": "#EFEFEF", "border": 1, "align": "center", "valign": "vcenter"})
        cell_fmt = wb.add_format({"align": "center", "valign": "vcenter", "text_wrap": True, "border": 1})
        
        for col, name in enumerate(df.columns): ws.write(0, col, name, header_fmt)
        
        ws.set_column(0, 0, 30, cell_fmt); ws.set_column(1, 1, 12, cell_fmt); ws.set_column(2, 2, 15, cell_fmt); ws.set_column(3, 3, 40, cell_fmt); ws.set_column(4, 10, 15, cell_fmt)
        img_cols = ["사진1", "사진2", "사진3"]
        base_col = len(df.columns)
        for i, c in enumerate(img_cols):
            ws.write(0, base_col + i, c, header_fmt)
            ws.set_column(base_col + i, base_col + i, 22, cell_fmt)

        for r in range(1, len(df) + 1): ws.set_row(r, 100)

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
                    with Image.open(img_path) as img: w, h = img.size
                    target_w, target_h = 150, 130
                    scale = min(target_w / w, target_h / h) * 0.9
                    ws.insert_image(idx + 1, base_col + j, img_path, {"x_scale": scale, "y_scale": scale, "object_position": 1})
                except: pass
        
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

def display_task_photos(t):
    photos = t.get("photos") or []
    if not isinstance(photos, list): photos = []
    if photos:
        st.markdown("📸 **현장 사진**")
        cols = st.columns(4) 
        for i, p in enumerate(photos):
            with cols[i % 4]: st.image(p.get("public_url"), use_container_width=True)
    else: st.caption("등록된 사진이 없습니다.")
    return photos


# =========================================================
# 7) 메인 화면: 탭 구성
# =========================================================
tabs = st.tabs(["📊 대시보드", "📝 문제등록", "📅 계획수립", "🛠️ 조치입력", "🔍 조회/관리"])

# ---------------------------------------------------------
# (A) 대시보드/보고서 (개편됨: 다중선택 + 표)
# ---------------------------------------------------------
with tabs[0]:
    # 1. 데이터 가져오기 (전체 로드 후 필터링)
    raw_tasks = fetch_tasks_all()
    
    if not raw_tasks:
        st.info("등록된 데이터가 없습니다.")
    else:
        # 데이터프레임 변환 (필터링 편의성)
        df_all = pd.DataFrame(raw_tasks)
        df_all['issue_date'] = pd.to_datetime(df_all['issue_date'])
        df_all['Year'] = df_all['issue_date'].dt.year
        df_all['YYYY-MM'] = df_all['issue_date'].dt.strftime('%Y-%m')

        # 2. 필터 UI (한 줄에 배치)
        c1, c2 = st.columns([1, 4])
        with c1:
            period_mode = st.selectbox("기간 기준", ["월간", "연간", "기간지정"], index=0)
        
        filtered_df = df_all.copy()
        
        with c2:
            if period_mode == "월간":
                # 월 다중 선택 (기본값: 이번달)
                all_months = sorted(df_all['YYYY-MM'].unique(), reverse=True)
                this_month = datetime.now().strftime('%Y-%m')
                default_m = [this_month] if this_month in all_months else (all_months[:1] if all_months else [])
                
                selected_months = st.multiselect("조회할 월을 선택하세요 (복수 선택 가능)", all_months, default=default_m)
                if selected_months:
                    filtered_df = df_all[df_all['YYYY-MM'].isin(selected_months)]
                else:
                    filtered_df = df_all.iloc[0:0] # 선택 안하면 빈값

            elif period_mode == "연간":
                # 년 다중 선택 (기본값: 올해)
                all_years = sorted(df_all['Year'].unique(), reverse=True)
                this_year = datetime.now().year
                default_y = [this_year] if this_year in all_years else (all_years[:1] if all_years else [])
                
                selected_years = st.multiselect("조회할 연도를 선택하세요 (복수 선택 가능)", all_years, default=default_y)
                if selected_years:
                    filtered_df = df_all[df_all['Year'].isin(selected_years)]
                else:
                    filtered_df = df_all.iloc[0:0]

            else: # 기간지정 (주간 등)
                d_col1, d_col2 = st.columns(2)
                today = date.today()
                start_d = d_col1.date_input("시작일", value=today - timedelta(weeks=1))
                end_d = d_col2.date_input("종료일", value=today)
                filtered_df = df_all[
                    (df_all['issue_date'].dt.date >= start_d) & 
                    (df_all['issue_date'].dt.date <= end_d)
                ]

        # 3. 핵심 지표 (Metrics)
        st.divider()
        total_cnt = len(filtered_df)
        done_cnt = len(filtered_df[filtered_df['status'] == '완료'])
        rate = (done_cnt / total_cnt * 100) if total_cnt > 0 else 0.0

        m1, m2, m3, m4 = st.columns([1, 1, 1, 2])
        m1.metric("총 발생", f"{total_cnt}건")
        m2.metric("조치 완료", f"{done_cnt}건")
        m3.metric("완료율", f"{rate:.1f}%")
        with m4:
            if st.button("📥 엑셀 다운로드 (사진 포함)", type="primary", use_container_width=True):
                # 필터링된 데이터만 엑셀로 변환
                # DataFrame -> dict list 변환 필요
                tasks_to_export = filtered_df.to_dict('records')
                # photos 등 누락된 정보 다시 채워주기 (df변환시 object라 유지되지만 안전하게)
                with st.spinner("엑셀 생성 중..."):
                    xbytes = export_excel(tasks_to_export)
                    fname = f"HACCP_개선보고서_{datetime.now().strftime('%Y%m%d')}.xlsx"
                    st.download_button("⬇️ 파일 받기", data=xbytes, file_name=fname, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        # 4. 차트와 표 (반반 배치)
        st.divider()
        
        if total_cnt == 0:
            st.warning("선택된 기간에 데이터가 없습니다.")
        else:
            col_chart, col_table = st.columns([1, 1])
            
            # (1) 데이터 집계 (장소별)
            filtered_df['공정/장소'] = filtered_df['location'].fillna("미분류").str.strip()
            
            loc_stats = filtered_df.groupby('공정/장소').agg(
                발생건수=('id', 'count'),
                완료건수=('status', lambda x: (x == '완료').sum())
            ).reset_index()
            loc_stats['개선율'] = (loc_stats['완료건수'] / loc_stats['발생건수'] * 100).round(1)
            loc_stats = loc_stats.sort_values('발생건수', ascending=False)

            with col_chart:
                st.markdown("##### 📊 장소별 발생 현황 (그래프)")
                # 차트용 데이터 변환
                chart_data = loc_stats.melt('공정/장소', value_vars=['발생건수', '완료건수'], var_name='구분', value_name='건수')
                
                chart = alt.Chart(chart_data).mark_bar().encode(
                    x=alt.X('공정/장소:N', sort='-y', axis=alt.Axis(labelAngle=0), title=None),
                    y=alt.Y('건수:Q', title=None),
                    color=alt.Color('구분:N', scale=alt.Scale(domain=['발생건수', '완료건수'], range=['#FF9F36', '#2ECC71'])),
                    xOffset='구분:N',
                    tooltip=['공정/장소', '구분', '건수']
                ).properties(height=350)
                st.altair_chart(chart, use_container_width=True)

            with col_table:
                st.markdown("##### 📋 장소별 상세 집계 (표)")
                # 보기 좋게 컬럼명 정리 및 표시
                display_table = loc_stats.rename(columns={'공정/장소': '장소'})
                
                st.dataframe(
                    display_table,
                    column_config={
                        "장소": st.column_config.TextColumn("장소", width="medium"),
                        "발생건수": st.column_config.NumberColumn("발생", format="%d건"),
                        "완료건수": st.column_config.NumberColumn("완료", format="%d건"),
                        "개선율": st.column_config.ProgressColumn("진행률", format="%.1f%%", min_value=0, max_value=100),
                    },
                    use_container_width=True,
                    hide_index=True,
                    height=350
                )


# ---------------------------------------------------------
# (B) 개선과제등록
# ---------------------------------------------------------
with tabs[1]:
    st.subheader("📝 문제 등록")
    with st.form("form_register", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        issue_date = c1.date_input("일시", value=date.today())
        location = c2.text_input("장소", placeholder="예: 포장실")
        reporter = c3.text_input("발견자", placeholder="예: 홍길동")
        issue_text = st.text_area("내용", placeholder="문제점을 구체적으로 입력하세요", height=100)
        photos = st.file_uploader("사진 (여러 장 가능)", type=["jpg", "png", "webp"], accept_multiple_files=True)
        if st.form_submit_button("등록", type="primary"):
            if not (location and reporter and issue_text):
                st.error("장소, 발견자, 내용은 필수입니다.")
            else:
                try:
                    tid = insert_task(issue_date, location, issue_text, reporter)
                    if photos:
                        for f in photos: upload_photo(tid, f)
                    st.success("저장되었습니다!")
                except Exception as e: st.error(f"오류: {e}")

# ---------------------------------------------------------
# (C) 개선계획수립
# ---------------------------------------------------------
with tabs[2]:
    st.subheader("📅 계획 수립")
    tasks = fetch_tasks_all()
    tasks = [t for t in tasks if t['status'] != '완료'] # 미완료만 보기
    
    if not tasks:
        st.info("계획을 수립할 미완료 과제가 없습니다.")
    else:
        opts = [f"[{t['issue_date']}] {t['location']} - {t['issue_text'][:20]}..." for t in tasks]
        sel = st.selectbox("과제 선택", opts)
        t = tasks[opts.index(sel)]
        
        st.info(f"내용: {t['issue_text']}")
        display_task_photos(t)
        
        with st.form("form_plan"):
            c1, c2 = st.columns(2)
            assignee = c1.text_input("담당자", value=t.get('assignee') or "")
            plan_due = c2.date_input("계획일정", value=pd.to_datetime(t.get('plan_due')).date() if t.get('plan_due') else date.today())
            plan_text = st.text_area("계획내용", value=t.get('plan_text') or "")
            if st.form_submit_button("저장"):
                update_task(t['id'], {"assignee": assignee, "plan_due": str(plan_due), "plan_text": plan_text})
                st.success("저장 완료")
                st.rerun()

# ---------------------------------------------------------
# (D) 개선완료 입력
# ---------------------------------------------------------
with tabs[3]:
    st.subheader("🛠️ 조치 결과 입력")
    tasks = fetch_tasks_all()
    # 완료 안된 것만
    tasks = [t for t in tasks if t['status'] != '완료']
    
    if not tasks:
        st.info("조치할 과제가 없습니다.")
    else:
        opts = [f"[{t['issue_date']}] {t['location']} - {t['issue_text'][:20]}..." for t in tasks]
        sel = st.selectbox("과제 선택", opts, key="act_sel")
        t = tasks[opts.index(sel)]
        
        st.info(f"내용: {t['issue_text']}")
        display_task_photos(t)
        
        with st.form("form_act"):
            action_text = st.text_area("조치내용", value=t.get('action_text') or "")
            action_done_date = st.date_input("완료일", value=pd.to_datetime(t.get('action_done_date')).date() if t.get('action_done_date') else date.today())
            if st.form_submit_button("조치 완료 저장", type="primary"):
                update_task(t['id'], {
                    "action_text": action_text,
                    "action_done_date": str(action_done_date),
                    "status": "완료"
                })
                st.balloons()
                st.success("조치 완료되었습니다!")
                st.rerun()

# ---------------------------------------------------------
# (E) 조회/관리
# ---------------------------------------------------------
with tabs[4]:
    st.subheader("🔍 통합 조회 및 관리")
    
    # 간편 필터
    c1, c2, c3 = st.columns([1, 1, 2])
    status_filter = c1.selectbox("상태", ["전체", "진행중", "완료"])
    loc_filter = c2.text_input("장소 검색")
    txt_filter = c3.text_input("내용 검색")
    
    tasks = fetch_tasks_all()
    filtered = []
    for t in tasks:
        if status_filter != "전체" and t['status'] != status_filter: continue
        if loc_filter and loc_filter not in (t['location'] or ""): continue
        if txt_filter and txt_filter not in (t['issue_text'] or ""): continue
        filtered.append(t)
        
    if not filtered:
        st.warning("조건에 맞는 데이터가 없습니다.")
    else:
        # 목록 표시
        df_list = pd.DataFrame(filtered)[['issue_date', 'location', 'issue_text', 'reporter', 'status', 'assignee', 'action_done_date']]
        df_list.columns = ['일시', '장소', '내용', '발견자', '상태', '담당자', '완료일']
        st.dataframe(df_list, use_container_width=True, hide_index=True, height=200)
        
        st.divider()
        st.markdown("#### 🔧 상세 관리 (수정/삭제)")
        opts = [f"[{t['issue_date']}] {t['location']} - {t['issue_text']}" for t in filtered]
        sel = st.selectbox("관리할 과제 선택", opts)
        target = filtered[opts.index(sel)]
        
        c_left, c_right = st.columns([3, 1])
        with c_left:
            st.markdown(f"**내용:** {target['issue_text']}")
            display_task_photos(target)
        
        with c_right:
            st.write("")
            st.write("")
            if st.button("🗑️ 과제 전체 삭제", type="primary"):
                delete_task_entirely(target['id'], target.get('photos'))
                st.success("삭제됨")
                st.rerun()
                
        # 사진 개별 삭제
        if target.get('photos'):
            with st.expander("사진 개별 관리"):
                cols = st.columns(4)
                for i, p in enumerate(target['photos']):
                    with cols[i%4]:
                        st.image(p['public_url'])
                        if st.button("삭제", key=f"del_{p['photo_id']}"):
                            delete_photo(p['photo_id'], p['storage_path'])
                            st.rerun()
                            
        # 사진 추가
        new_photos = st.file_uploader("사진 추가 등록", accept_multiple_files=True, key="add_new")
        if new_photos and st.button("추가 업로드"):
            for f in new_photos: upload_photo(target['id'], f)
            st.success("업로드 완료")
            st.rerun()
