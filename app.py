import os
import io
import json
import uuid
import math
import base64
import tempfile
import time
from datetime import date, datetime, timedelta
import pytz 

import requests
import pandas as pd
import streamlit as st
import altair as alt
from PIL import Image
from supabase import create_client

# =========================================================
# 0) 기본 UI 설정
# =========================================================
st.set_page_config(page_title="천안공장 위생 개선관리", layout="wide", initial_sidebar_state="collapsed")

def get_image_base64(file_path):
    with open(file_path, "rb") as f: data = f.read()
    return base64.b64encode(data).decode()

st.markdown("""
<style>
    .block-container { padding-top: 3rem; padding-bottom: 3rem; font-family: 'Pretendard', sans-serif; }
    .header-container { display: flex; align-items: center; padding-bottom: 2rem; margin-bottom: 1rem; border-bottom: 2px solid #f1f3f5; }
    .header-image-container { flex: 0 0 auto; margin-right: 2.5rem; }
    .header-image-container img { width: 140px; height: auto; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
    .fallback-icon { font-size: 5rem; line-height: 1; background: #f8f9fa; padding: 10px; border-radius: 12px; }
    .header-text-container { flex: 1; }
    h1.main-title { font-size: 3.2rem !important; font-weight: 800 !important; margin: 0 !important; color: #212529; letter-spacing: -1px; }
    .sub-caption { font-size: 1.2rem; color: #868e96; margin-top: 0.5rem; font-weight: 500; }
    
    div[data-testid="stTabs"] { gap: 0px; }
    div[data-testid="stTabs"] button[data-testid="stTab"] { background-color: #f8f9fa; color: #495057; border: 1px solid #dee2e6; border-bottom: none; border-radius: 10px 10px 0 0; padding: 1rem 2rem; font-weight: 700; margin-right: 4px; }
    div[data-testid="stTabs"] button[data-testid="stTab"][aria-selected="true"] { background-color: #ffffff; color: #e03131; border-top: 3px solid #e03131; border-bottom: 2px solid #ffffff; margin-bottom: -2px; z-index: 10; }
    
    .metric-card { 
        background-color: #f8f9fa; 
        border: 1px solid #e9ecef; 
        border-radius: 8px; 
        padding: 15px; 
        text-align: center; 
        box-shadow: 0 2px 4px rgba(0,0,0,0.05); 
        margin-bottom: 10px;
    }
    .metric-title { font-size: 0.9rem; color: #868e96; font-weight: 600; margin-bottom: 5px; }
    .metric-value { font-size: 1.6rem; font-weight: 700; color: #212529; }
    .metric-sub { font-size: 0.8rem; color: #adb5bd; margin-top: 5px; }
</style>
""", unsafe_allow_html=True)

logo_html = ""
if os.path.exists("logo.png"):
    img_b64 = get_image_base64("logo.png")
    logo_html = f'<img src="data:image/png;base64,{img_b64}" alt="로고">'
else:
    logo_html = "<div class='fallback-icon'>🍶</div>"

st.markdown(f"""
<div class="header-container">
    <div class="header-image-container">{logo_html}</div>
    <div class="header-text-container">
        <h1 class="main-title">천안공장 위생 개선관리</h1>
        <p class="sub-caption">스마트 해썹(HACCP) 대응을 위한 현장 개선 및 온습도 데이터 관리 시스템</p>
    </div>
</div>
""", unsafe_allow_html=True)

try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"].strip()
    SUPABASE_SERVICE_KEY = st.secrets["SUPABASE_SERVICE_KEY"].strip()
    BUCKET = st.secrets["SUPABASE_BUCKET"].strip()
except:
    st.error("🚨 Secrets 설정이 누락되었습니다.")
    st.stop()

@st.cache_resource
def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

sb = get_supabase()

# 기본 설정값
DEFAULT_SENSOR_CONFIG = {
    "1호기": "쌀창고", "2호기": "전처리실", "3호기": "전처리실", "4호기": "전처리실",
    "5호기": "양조실", "6호기": "양조실", "7호기": "양조실",
    "8호기": "제품포장실", "9호기": "제품포장실", "10호기": "부자재창고"
}
# ★ 기본 순서 (DB 없을 때 사용)
DEFAULT_ROOM_ORDER = ["전처리실", "양조실", "제품포장실", "쌀창고", "부자재창고"]
DEFAULT_ALARM_CONFIG = {
    "쌀창고": (5.0, 25.0), "전처리실": (10.0, 30.0), "양조실": (20.0, 28.0),
    "제품포장실": (10.0, 30.0), "부자재창고": (0.0, 40.0), "default": (0.0, 35.0)
}
ROOM_ICONS = {"쌀창고": "🌾", "전처리실": "🥣", "양조실": "🍶", "제품포장실": "📦", "부자재창고": "🔧"}

# =========================================================
# 2) 핵심 로직
# =========================================================
def fetch_sensor_mapping_from_db():
    try:
        res = sb.table("sensor_mapping").select("*").execute()
        if res.data:
            return {item['sensor_id']: item['room_name'] for item in res.data}
    except: pass
    return DEFAULT_SENSOR_CONFIG

def fetch_alarm_config_from_db():
    try:
        res = sb.table("room_settings").select("*").execute()
        if res.data:
            # { '전처리실': {'min':10, 'max':30, 'cat':'작업장', 'order':1} }
            config = {}
            for item in res.data:
                config[item['room_name']] = {
                    "min": item['min_temp'], 
                    "max": item['max_temp'],
                    "cat": item.get('category', '기타'),
                    "order": item.get('sort_order', 999) # 순서 없으면 999
                }
            return config
    except: pass
    return {}

@st.cache_data(ttl=5, show_spinner=False)
def fetch_tasks_all() -> list[dict]:
    try:
        res = sb.table("haccp_tasks").select("*").order("issue_date", desc=True).execute()
        tasks = res.data or []
        if not tasks: return []

        t_ids = [t["id"] for t in tasks]
        res_p = sb.table("haccp_task_photos").select("*").in_("task_id", t_ids).execute()
        photos = res_p.data or []
        
        p_map_b, p_map_a = {}, {}
        for p in photos:
            tid = p["task_id"]
            if "id" in p and "photo_id" not in p: p["photo_id"] = p["id"]
            target_map = photo_map_after if '/AFTER_' in p.get('storage_path', '') else photo_map_before
            if tid not in target_map: target_map[tid] = []
            target_map[tid].append(p)
            
        for t in tasks:
            t["photos_before"] = photo_map_before.get(t["id"], [])
            t["photos_after"] = photo_map_after.get(t["id"], [])
            t["photos"] = t["photos_before"] + t["photos_after"]
        return tasks
    except: return []

@st.cache_data(ttl=60, show_spinner=False)
def fetch_sensor_logs(days=7, mapping=None) -> pd.DataFrame:
    try:
        start_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
        res = sb.table("sensor_logs").select("*").gte("created_at", start_date).order("created_at", desc=True).limit(5000).execute()
        data = res.data or []
        if not data: return pd.DataFrame()
        
        df = pd.DataFrame(data)
        df['created_at'] = pd.to_datetime(df['created_at']).dt.tz_convert('Asia/Seoul')
        df['sensor_id'] = df['place'] 
        current_map = mapping if mapping else DEFAULT_SENSOR_CONFIG
        df['room_name'] = df['place'].map(current_map).fillna("미분류")
        return df
    except: return pd.DataFrame()

def clear_cache():
    fetch_tasks_all.clear()
    fetch_sensor_logs.clear()

def insert_task(issue_date, location, issue_text, reporter, grade):
    row = {"issue_date": str(issue_date), "location": location.strip(), "issue_text": issue_text.strip(), "reporter": reporter.strip(), "grade": grade, "status": "진행중"}
    res = sb.table("haccp_tasks").insert(row).execute()
    clear_cache()
    return res.data[0]["id"]

def update_task(task_id, patch):
    sb.table("haccp_tasks").update(patch).eq("id", task_id).execute()
    clear_cache()

def delete_task_entirely(task_id: str, photos: list):
    if photos:
        paths = [p.get("storage_path") for p in photos if p.get("storage_path")]
        if paths:
            try: sb.storage.from_(BUCKET).remove(paths)
            except: pass 
    sb.table("haccp_tasks").delete().eq("id", task_id).execute()
    clear_cache()

def compress_image(file_bytes: bytes, max_w=1024) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(file_bytes))
    if img.mode in ("RGBA", "P"): img = img.convert("RGB")
    w, h = img.size
    if w > max_w:
        new_h = int(h * (max_w / w))
        img = img.resize((max_w, new_h), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70, optimize=True)
    return out.getvalue(), "jpg"

def make_public_url(bucket: str, path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"

def upload_photo(task_id: str, uploaded_file, photo_type="BEFORE"):
    raw = uploaded_file.read()
    compressed, ext = compress_image(raw)
    filename = f"{photo_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.{ext}"
    key = f"{task_id}/{filename}"
    sb.storage.from_(BUCKET).upload(path=key, file=compressed, file_options={"content-type": "image/jpeg", "upsert": "false"})
    url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{key}"
    sb.table("haccp_task_photos").insert({"task_id": task_id, "storage_path": key, "public_url": url}).execute()
    clear_cache()

def delete_photo(photo_id: str, storage_path: str):
    try: sb.storage.from_(BUCKET).remove([storage_path])
    except: pass
    sb.table("haccp_task_photos").delete().eq("id", photo_id).execute()
    clear_cache()

def display_photos_grid(photos, title=None):
    if title: st.markdown(f"**{title}**")
    if not photos:
        st.caption("사진 없음")
        return
    cols = st.columns(4)
    for i, p in enumerate(photos):
        with cols[i % 4]: st.image(p.get("public_url"), use_container_width=True)

def export_excel(tasks: list[dict]) -> bytes:
    rows = []
    for t in tasks:
        rows.append({"일시": t.get("issue_date"), "장소": t.get("location"), "등급": t.get("grade"), "내용": t.get("issue_text"), "상태": t.get("status"), "조치내용": t.get("action_text")})
    df = pd.DataFrame(rows)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False)
    return out.getvalue()

GRADE_OPTIONS = ["C등급", "B등급", "A등급", "공장장", "본부장", "대표이사"]

# =========================================================
# 7) 메인 화면: 탭 구성
# =========================================================
tabs = st.tabs(["📊 대시보드", "📝 문제등록", "📅 계획수립", "🛠️ 조치입력", "🔍 조회/관리", "🌡️ 실별온도관리"])

with tabs[0]: # 대시보드 (원본)
    raw_tasks = fetch_tasks_all()
    if not raw_tasks: st.info("데이터가 없습니다.")
    else:
        df_all = pd.DataFrame(raw_tasks)
        df_all['issue_date'] = pd.to_datetime(df_all['issue_date'])
        c1, c2 = st.columns([1, 4])
        with c1: period_mode = st.selectbox("기간 기준", ["월간", "주간", "연간", "전체"], index=3)
        filtered_df = df_all 
        
        total_cnt = len(filtered_df)
        done_cnt = len(filtered_df[filtered_df['status'] == '완료'])
        rate = (done_cnt / total_cnt * 100) if total_cnt > 0 else 0.0

        m1, m2, m3, m4 = st.columns([1, 1, 1, 2])
        m1.metric("총 발생", f"{total_cnt}건"); m2.metric("조치 완료", f"{done_cnt}건"); m3.metric("완료율", f"{rate:.1f}%")
        with m4:
            if st.button("📥 엑셀 다운로드"):
                st.download_button("파일 받기", data=export_excel(filtered_df.to_dict('records')), file_name="HACCP_Data.xlsx")
        st.divider()
        if not filtered_df.empty:
            c_ch, c_tb = st.columns(2)
            with c_ch:
                loc_stats = filtered_df['location'].value_counts().reset_index()
                loc_stats.columns = ['장소', '건수']
                st.markdown("##### 📊 장소별 현황")
                st.altair_chart(alt.Chart(loc_stats).mark_bar().encode(x='장소', y='건수', color='장소'), use_container_width=True)
            with c_tb:
                st.markdown("##### 📋 상세 목록")
                st.dataframe(filtered_df[['issue_date', 'location', 'grade', 'status']], use_container_width=True)

with tabs[1]: # 문제등록
    st.subheader("📝 문제 등록")
    with st.form("form_register", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        issue_date = c1.date_input("일시", value=date.today())
        location = c2.text_input("장소")
        reporter = c3.text_input("발견자")
        grade = c4.selectbox("등급", GRADE_OPTIONS)
        issue_text = st.text_area("내용")
        photos = st.file_uploader("사진", accept_multiple_files=True)
        if st.form_submit_button("등록", type="primary"):
            if location and reporter:
                tid = insert_task(issue_date, location, issue_text, reporter, grade)
                if photos: 
                    for f in photos: upload_photo(tid, f)
                st.success("등록 완료")
            else: st.error("필수 입력 누락")

with tabs[2]: # 계획수립
    st.subheader("📅 계획 수립")
    tasks = [t for t in fetch_tasks_all() if t['status'] != '완료']
    if tasks:
        opts = [f"{t['issue_date']} | {t['location']} - {t['issue_text'][:10]}..." for t in tasks]
        sel = st.selectbox("과제 선택", opts)
        t = tasks[opts.index(sel)]
        st.info(f"내용: {t['issue_text']}")
        with st.form("form_plan"):
            plan_text = st.text_area("계획내용", value=t.get('plan_text') or "")
            if st.form_submit_button("저장"):
                update_task(t['id'], {"plan_text": plan_text})
                st.success("저장됨"); st.rerun()
    else: st.info("대상 없음")

with tabs[3]: # 조치입력
    st.subheader("🛠️ 조치 결과")
    tasks = [t for t in fetch_tasks_all() if t['status'] != '완료']
    if tasks:
        opts = [f"{t['location']} - {t['issue_text'][:10]}..." for t in tasks]
        sel = st.selectbox("조치 대상", opts)
        t = tasks[opts.index(sel)]
        st.info(f"문제: {t['issue_text']}")
        with st.expander("사진 추가"):
            act_p = st.file_uploader("사진", accept_multiple_files=True)
            if act_p and st.button("사진 업로드"):
                for f in act_p: upload_photo(t['id'], f, "AFTER")
                st.rerun()
        with st.form("form_act"):
            act_text = st.text_area("조치내용", value=t.get('action_text') or "")
            if st.form_submit_button("완료 처리"):
                update_task(t['id'], {"action_text": act_text, "status": "완료", "action_done_date": str(date.today())})
                st.success("완료됨"); st.rerun()
    else: st.info("대상 없음")

with tabs[4]: # 조회 (원본)
    st.subheader("🔍 통합 조회")
    tasks = fetch_tasks_all()
    if tasks:
        df = pd.DataFrame(tasks)
        st.dataframe(df[['issue_date', 'location', 'issue_text', 'status']], use_container_width=True)
        st.divider()
        st.markdown("##### 🔧 상세 관리")
        opts = [f"{t['issue_date']} | {t['location']} - {t['issue_text'][:15]}..." for t in tasks]
        sel_t = st.selectbox("항목 선택", opts)
        target = tasks[opts.index(sel_t)]
        c1, c2 = st.columns([3, 1])
        c1.warning(f"선택: {target['location']}")
        if c2.button("🗑️ 삭제", type="primary"):
            delete_task_entirely(target['id'], target.get('photos'))
            st.success("삭제됨"); st.rerun()
        with st.expander("수정"):
            new_g = st.selectbox("등급 변경", GRADE_OPTIONS, index=GRADE_OPTIONS.index(target.get('grade') or "C등급"))
            if st.button("저장"):
                update_task(target['id'], {"grade": new_g})
                st.success("수정됨"); st.rerun()
    else: st.warning("데이터 없음")

# =========================================================
# [마지막 탭] 실별 온도관리 (★ 순서 조절 기능 탑재 ★)
# =========================================================
with tabs[5]:
    # 1. DB 데이터 가져오기
    current_mapping = fetch_sensor_mapping_from_db()
    current_settings = fetch_alarm_config_from_db() # { '방이름': {'min':.., 'max':.., 'cat':.., 'order':..} }
    
    # DB에 없는 기본 방들도 보여주기 위해 합치기
    all_known_rooms = set(list(current_settings.keys()) + ["쌀창고", "전처리실", "양조실", "제품포장실", "부자재창고"])
    
    @st.dialog("⚙️ 환경 설정")
    def open_setting_popup():
        tab_rooms, tab_map = st.tabs(["🏗️ 장소 관리 (순서/온도)", "📍 센서 위치 연결"])
        
        with tab_rooms:
            # 설정값 데이터프레임 생성
            if "df_settings" not in st.session_state:
                rows = []
                for r in all_known_rooms:
                    conf = current_settings.get(r, {"min": 0, "max": 35, "cat": "기타", "order": 999})
                    rows.append({
                        "순서(No)": conf.get('order', 999), # 순서 추가
                        "장소": r,
                        "구역": conf.get('cat', "기타"),
                        "Min(℃)": conf.get('min', 0.0),
                        "Max(℃)": conf.get('max', 35.0)
                    })
                st.session_state.df_settings = pd.DataFrame(rows).sort_values("순서(No)")

            # 장소 추가 폼
            with st.form("add_room_form", clear_on_submit=True):
                c_add1, c_add2, c_add3 = st.columns([2, 1, 1])
                new_name = c_add1.text_input("새 장소 이름", placeholder="예: 제2숙성실")
                new_cat = c_add2.selectbox("구역", ["작업장", "창고", "기타"])
                if c_add3.form_submit_button("➕ 추가"):
                    if new_name and new_name not in st.session_state.df_settings['장소'].values:
                        new_row = {"순서(No)": 999, "장소": new_name, "구역": new_cat, "Min(℃)": 10.0, "Max(℃)": 30.0}
                        st.session_state.df_settings = pd.concat([st.session_state.df_settings, pd.DataFrame([new_row])], ignore_index=True)
                        st.success(f"'{new_name}' 추가됨!")
                    elif new_name: st.warning("이미 존재함")

            # 설정 테이블 (순서 수정 가능)
            st.caption("👇 '순서' 숫자를 바꾸면 화면 배치 순서가 바뀝니다. (1, 2, 3...)")
            edited_settings = st.data_editor(
                st.session_state.df_settings,
                column_config={
                    "순서(No)": st.column_config.NumberColumn("순서", help="작은 숫자가 먼저 표시됨", step=1),
                    "장소": st.column_config.TextColumn("장소", disabled=True),
                    "구역": st.column_config.SelectboxColumn("구역", options=["작업장", "창고", "기타"], required=True),
                    "Min(℃)": st.column_config.NumberColumn("최저", format="%.1f"),
                    "Max(℃)": st.column_config.NumberColumn("최고", format="%.1f"),
                },
                hide_index=True, use_container_width=True, key="settings_editor", num_rows="dynamic"
            )
            st.session_state.df_settings = edited_settings

        with tab_map:
            st.info("센서 위치를 지정하세요.")
            room_opts = sorted(st.session_state.df_settings['장소'].tolist())
            map_df = pd.DataFrame([{"센서": k, "장소": v} for k, v in current_mapping.items()]).sort_values("센서")
            edited_map = st.data_editor(
                map_df,
                column_config={
                    "센서": st.column_config.TextColumn("센서명", disabled=True),
                    "장소": st.column_config.SelectboxColumn("설치 장소", options=room_opts, required=True)
                },
                hide_index=True, use_container_width=True, key="map_editor"
            )

        st.divider()
        if st.button("💾 저장하기", type="primary", use_container_width=True):
            try:
                # 온도/순서/구역 저장
                settings_rows = []
                for _, row in st.session_state.df_settings.iterrows():
                    settings_rows.append({
                        "room_name": row["장소"], "category": row["구역"], 
                        "min_temp": row["Min(℃)"], "max_temp": row["Max(℃)"],
                        "sort_order": row["순서(No)"] # 순서 저장
                    })
                sb.table("room_settings").upsert(settings_rows).execute()
                
                # 센서 위치 저장
                map_rows = [{"sensor_id": r["센서"], "room_name": r["장소"]} for r in edited_map.to_dict('records')]
                sb.table("sensor_mapping").upsert(map_rows).execute()
                
                fetch_sensor_logs.clear()
                st.success("✅ 저장되었습니다!"); time.sleep(1); st.rerun()
            except Exception as e: st.error(f"저장 오류: {e}")

    # 헤더
    col_h, col_b = st.columns([6, 1], vertical_alignment="center")
    with col_h: st.subheader("🌡️ 실별 온도/습도 관리")
    with col_b:
        if st.button("⚙️ 설정"): 
            if "df_settings" in st.session_state: del st.session_state.df_settings
            open_setting_popup()

    # 데이터 로드
    df_logs = fetch_sensor_logs(days=30, mapping=current_mapping)
    latest = pd.DataFrame()
    if not df_logs.empty: latest = df_logs.sort_values('created_at').groupby('sensor_id').tail(1)

    # 화면 표시 (순서 적용)
    # DB에 있는 순서 정보(order)를 기준으로 정렬
    sorted_rooms = []
    # current_settings에 있는 방들을 order 기준으로 정렬
    sorted_settings = sorted(current_settings.items(), key=lambda x: x[1].get('order', 999))
    
    # 그룹핑 (정렬된 순서대로 그룹에 넣음)
    # 예: {'작업장': ['양조실(1번)', '전처리실(2번)'], ...}
    GROUPS = {"작업장": [], "창고": [], "기타": []}
    
    # 1. DB에 설정된 방들 먼저 배치
    for r_name, conf in sorted_settings:
        cat = conf.get('cat', '기타')
        if cat not in GROUPS: GROUPS[cat] = []
        # 실제로 센서가 있거나, 기본 방이면 표시
        if r_name in current_mapping.values() or r_name in DEFAULT_SENSOR_CONFIG.values():
            GROUPS[cat].append(r_name)
            
    # 2. 센서는 있는데 설정에 없는 방 처리 (기타로)
    active_rooms = set(current_mapping.values())
    flat_list = sum(GROUPS.values(), [])
    for r in active_rooms:
        if r not in flat_list: GROUPS["기타"].append(r)

    if df_logs.empty: st.info("📊 데이터 없음")
    else:
        # 그룹 표시 (작업장 -> 창고 -> 기타 순)
        display_order = ["작업장", "창고", "기타"] + [k for k in GROUPS.keys() if k not in ["작업장", "창고", "기타"]]
        
        for g_name in display_order:
            rooms = GROUPS.get(g_name, [])
            if not rooms: continue
            
            st.markdown(f"##### {g_name}")
            cols = st.columns(4)
            for idx, room in enumerate(rooms):
                room_sensors = latest[latest['room_name'] == room]
                with cols[idx % 4]:
                    icon = ROOM_ICONS.get(room, "🏢")
                    # 설정값 가져오기
                    conf = current_settings.get(room, {"min":0, "max":35})
                    min_v, max_v = conf.get('min', 0), conf.get('max', 35)
                    
                    if not room_sensors.empty:
                        avg_t = room_sensors['temperature'].mean()
                        avg_h = room_sensors['humidity'].mean()
                        det_html = ""
                        warn = False
                        for _, row in room_sensors.iterrows():
                            t = row['temperature']
                            if t < min_v or t > max_v: c, w, a, warn = "#e03131", "bold", "🚨", True
                            else: c, w, a = "#555", "normal", ""
                            det_html += f"<div style='display:flex;justify-content:space-between;font-size:0.75rem;color:{c};font-weight:{w};'>{row['sensor_id']}<span>{a}{t}℃</span></div>"
                        
                        hc = "#e03131" if warn else "#212529"
                        st.markdown(f"""<div class="metric-card" style="border-top:3px solid {hc};padding:10px;">
                        <div style="font-weight:800;color:{hc};">{icon} {room}</div>
                        <div style="font-size:1.4rem;color:{hc}">{avg_t:.1f}℃</div>
                        <div style="font-size:0.75rem;color:#888;">기준: {min_v}~{max_v}</div>
                        <div style="font-size:0.9rem;color:#4dabf7;">💧 {avg_h:.1f}%</div>
                        <hr style="margin:5px 0;">{det_html}</div>""", unsafe_allow_html=True)
                    else:
                        st.markdown(f"""<div class="metric-card" style="opacity:0.6;"><div style="font-weight:800;color:#aaa;">{icon} {room}</div><div>-</div><div style="font-size:0.7rem;">데이터 없음</div></div>""", unsafe_allow_html=True)
            st.markdown("")

        st.divider()
        st.markdown("#### 📈 상세 분석")
        col_f1, col_f2 = st.columns([1, 2])
        valid_analysis_rooms = list(active_rooms)
        if valid_analysis_rooms:
            sel_room = col_f1.selectbox("장소 선택", valid_analysis_rooms)
            target_df = df_logs[df_logs['room_name'] == sel_room].copy()
            if not target_df.empty:
                base = alt.Chart(target_df).encode(x='created_at:T')
                lines = base.mark_line(opacity=0.5).encode(y='temperature:Q', color='sensor_id:N')
                avg = base.mark_line(strokeWidth=3, color='#333').encode(y='mean(temperature):Q')
                st.altair_chart((lines + avg).properties(height=300), use_container_width=True)
            else: st.warning("데이터 없음")
