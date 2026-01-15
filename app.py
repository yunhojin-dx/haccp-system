import os
import io
import json
import uuid
import math
import base64
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
    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] { border-top: 2px solid #dee2e6; margin-top: -2px; }
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
        <p class="sub-caption">스마트 해썹(HACCP) 대응을 위한 현장 개선 데이터 관리 시스템</p>
    </div>
</div>
""", unsafe_allow_html=True)


# =========================================================
# 1) Secrets & DB 연결
# =========================================================
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


# =========================================================
# 2) 핵심 로직
# =========================================================
@st.cache_data(ttl=5, show_spinner=False)
def fetch_tasks_all() -> list[dict]:
    try:
        res = sb.table("haccp_tasks").select("*").order("issue_date", desc=True).execute()
        tasks = res.data or []
        if not tasks: return []

        t_ids = [t["id"] for t in tasks]
        res_p = sb.table("haccp_task_photos").select("*").in_("task_id", t_ids).execute()
        photos = res_p.data or []
        
        photo_map_before = {}
        photo_map_after = {}
        
        for p in photos:
            tid = p["task_id"]
            if "id" in p and "photo_id" not in p: p["photo_id"] = p["id"]
            
            path = p.get('storage_path', '')
            if '/AFTER_' in path:
                if tid not in photo_map_after: photo_map_after[tid] = []
                photo_map_after[tid].append(p)
            else:
                if tid not in photo_map_before: photo_map_before[tid] = []
                photo_map_before[tid].append(p)
            
        for t in tasks:
            t["photos_before"] = photo_map_before.get(t["id"], [])
            t["photos_after"] = photo_map_after.get(t["id"], [])
            t["photos"] = t["photos_before"] + t["photos_after"]
            
        return tasks
    except Exception as e:
        print(f"DB Error: {e}")
        return []

def clear_cache():
    fetch_tasks_all.clear()

def insert_task(issue_date, location, issue_text, reporter):
    row = {"issue_date": str(issue_date), "location": location.strip(), "issue_text": issue_text.strip(), "reporter": reporter.strip(), "status": "진행중"}
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

def compress_image(file_bytes: bytes, max_w=1024, quality=70) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(file_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    w, h = img.size
    if w > max_w:
        new_h = int(h * (max_w / w))
        img = img.resize((max_w, new_h), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), "jpg"

def make_public_url(bucket: str, path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"

def upload_photo(task_id: str, uploaded_file, photo_type="BEFORE") -> dict:
    raw = uploaded_file.read()
    compressed, ext = compress_image(raw, max_w=1024, quality=70)
    filename = f"{photo_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.{ext}"
    key = f"{task_id}/{filename}"
    sb.storage.from_(BUCKET).upload(path=key, file=compressed, file_options={"content-type": "image/jpeg", "upsert": "false"})
    url = make_public_url(BUCKET, key)
    row = {"task_id": task_id, "storage_path": key, "public_url": url}
    sb.table("haccp_task_photos").insert(row).execute()
    clear_cache()
    return row

def delete_photo(photo_id: str, storage_path: str):
    try: sb.storage.from_(BUCKET).remove([storage_path])
    except: pass
    sb.table("haccp_task_photos").delete().eq("id", photo_id).execute()
    clear_cache()

def download_image_to_temp(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=5)
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
        base_col = len(df.columns)
        photo_headers = ["개선전_사진1", "개선전_사진2", "개선후_사진1", "개선후_사진2"]
        for i, ph in enumerate(photo_headers):
            ws.write(0, base_col + i, ph, header_fmt)
            ws.set_column(base_col + i, base_col + i, 22, cell_fmt)
        for r in range(1, len(df) + 1): ws.set_row(r, 100)
        for idx, t in enumerate(tasks):
            befores = t.get("photos_before", [])[:2]
            afters = t.get("photos_after", [])[:2]
            export_photos = befores + [None]*(2-len(befores)) + afters + [None]*(2-len(afters))
            for j, p in enumerate(export_photos):
                if p and p.get("public_url"):
                    img_path = download_image_to_temp(p.get("public_url"))
                    if img_path:
                        try:
                            with Image.open(img_path) as img: w, h = img.size
                            scale = min(150 / w, 130 / h) * 0.9
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

def display_photos_grid(photos, title=None):
    if title: st.markdown(f"**{title}**")
    if not photos:
        st.caption("사진 없음")
        return
    cols = st.columns(4)
    for i, p in enumerate(photos):
        with cols[i % 4]: st.image(p.get("public_url"), use_container_width=True)


# =========================================================
# 7) 메인 화면: 탭 구성
# =========================================================
tabs = st.tabs(["📊 대시보드", "📝 문제등록", "📅 계획수립", "🛠️ 조치입력", "🔍 조회/관리"])

with tabs[0]: # 대시보드
    raw_tasks = fetch_tasks_all()
    if not raw_tasks:
        st.info("등록된 데이터가 없습니다.")
    else:
        df_all = pd.DataFrame(raw_tasks)
        df_all['issue_date'] = pd.to_datetime(df_all['issue_date'])
        df_all['Year'] = df_all['issue_date'].dt.year
        df_all['YYYY-MM'] = df_all['issue_date'].dt.strftime('%Y-%m')
        df_all['Week_Label'] = df_all['issue_date'].apply(lambda x: f"{x.year}-{x.isocalendar()[1]:02d}주차")

        c1, c2 = st.columns([1, 4])
        with c1: period_mode = st.selectbox("기간 기준", ["월간", "주간", "연간", "기간지정"], index=0)
        
        filtered_df = df_all.copy()
        today = date.today()
        
        with c2:
            if period_mode == "월간":
                all_months = sorted(df_all['YYYY-MM'].unique(), reverse=True)
                this_month = datetime.now().strftime('%Y-%m')
                default_m = [this_month] if this_month in all_months else (all_months[:1] if all_months else [])
                selected_months = st.multiselect("조회할 월 선택", all_months, default=default_m)
                filtered_df = df_all[df_all['YYYY-MM'].isin(selected_months)] if selected_months else df_all.iloc[0:0]
            elif period_mode == "주간":
                all_weeks = sorted(df_all['Week_Label'].unique(), reverse=True)
                this_year, this_week, _ = datetime.now().isocalendar()
                this_week_label = f"{this_year}-{this_week:02d}주차"
                default_w = [this_week_label] if this_week_label in all_weeks else (all_weeks[:1] if all_weeks else [])
                selected_weeks = st.multiselect("조회할 주차 선택", all_weeks, default=default_w)
                filtered_df = df_all[df_all['Week_Label'].isin(selected_weeks)] if selected_weeks else df_all.iloc[0:0]
            elif period_mode == "연간":
                all_years = sorted(df_all['Year'].unique(), reverse=True)
                this_year = datetime.now().year
                default_y = [this_year] if this_year in all_years else (all_years[:1] if all_years else [])
                selected_years = st.multiselect("조회할 연도 선택", all_years, default=default_y)
                filtered_df = df_all[df_all['Year'].isin(selected_years)] if selected_years else df_all.iloc[0:0]
            else: 
                d_col1, d_col2 = st.columns(2)
                start_d = d_col1.date_input("시작", value=today - timedelta(weeks=1))
                end_d = d_col2.date_input("종료", value=today)
                filtered_df = df_all[(df_all['issue_date'].dt.date >= start_d) & (df_all['issue_date'].dt.date <= end_d)]

        st.divider()
        total_cnt = len(filtered_df)
        done_cnt = len(filtered_df[filtered_df['status'] == '완료'])
        rate = (done_cnt / total_cnt * 100) if total_cnt > 0 else 0.0

        m1, m2, m3, m4 = st.columns([1, 1, 1, 2])
        m1.metric("총 발생", f"{total_cnt}건")
        m2.metric("조치 완료", f"{done_cnt}건")
        m3.metric("완료율", f"{rate:.1f}%")
        with m4:
            if st.button("📥 엑셀 다운로드", type="primary", use_container_width=True):
                with st.spinner("생성 중..."):
                    st.download_button("⬇️ 파일 받기", data=export_excel(filtered_df.to_dict('records')), file_name=f"HACCP_{datetime.now().strftime('%Y%m%d')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        st.divider()
        if total_cnt == 0: st.warning("데이터가 없습니다.")
        else:
            col_chart, col_table = st.columns([1, 1])
            filtered_df['공정/장소'] = filtered_df['location'].fillna("미분류").str.strip()
            loc_stats = filtered_df.groupby('공정/장소').agg(발생건수=('id', 'count'), 완료건수=('status', lambda x: (x == '완료').sum())).reset_index()
            loc_stats['개선율'] = (loc_stats['완료건수'] / loc_stats['발생건수'] * 100).round(1)
            loc_stats = loc_stats.sort_values('발생건수', ascending=False)

            with col_chart:
                st.markdown("##### 📊 장소별 현황")
                c_data = loc_stats.melt('공정/장소', value_vars=['발생건수', '완료건수'], var_name='구분', value_name='건수')
                chart = alt.Chart(c_data).mark_bar().encode(
                    x=alt.X('공정/장소:N', sort='-y', axis=alt.Axis(labelAngle=0), title=None),
                    y=alt.Y('건수:Q', title=None),
                    color=alt.Color('구분:N', scale=alt.Scale(domain=['발생건수', '완료건수'], range=['#FF9F36', '#2ECC71'])),
                    xOffset='구분:N', tooltip=['공정/장소', '구분', '건수']
                ).properties(height=300)
                st.altair_chart(chart, use_container_width=True)

            with col_table:
                st.markdown("##### 📋 상세 집계")
                st.dataframe(loc_stats.rename(columns={'공정/장소': '장소'}), use_container_width=True, hide_index=True, height=300)

with tabs[1]: # 문제 등록
    st.subheader("📝 문제 등록")
    with st.form("form_register", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        issue_date = c1.date_input("일시", value=date.today())
        location = c2.text_input("장소", placeholder="예: 포장실")
        reporter = c3.text_input("발견자", placeholder="예: 홍길동")
        issue_text = st.text_area("내용", placeholder="내용 입력", height=100)
        photos = st.file_uploader("사진 (개선 전)", type=["jpg", "png", "webp"], accept_multiple_files=True)
        if st.form_submit_button("등록", type="primary"):
            if not (location and reporter and issue_text):
                st.error("필수 항목 누락")
            else:
                try:
                    tid = insert_task(issue_date, location, issue_text, reporter)
                    if photos:
                        for f in photos: upload_photo(tid, f, photo_type="BEFORE")
                    st.success("저장 완료!")
                except Exception as e: st.error(f"오류: {e}")

with tabs[2]: # 계획 수립
    st.subheader("📅 계획 수립")
    tasks = fetch_tasks_all()
    tasks = [t for t in tasks if t['status'] != '완료'] 
    if not tasks: st.info("대상 과제 없음")
    else:
        opts = [f"[{t['issue_date']}] {t['location']} - {t['issue_text'][:20]}..." for t in tasks]
        sel = st.selectbox("과제 선택", opts)
        t = tasks[opts.index(sel)]
        st.info(f"내용: {t['issue_text']}")
        display_photos_grid(t.get('photos_before', []), "📸 개선 전 사진")
        
        with st.form("form_plan"):
            st.markdown("**✏️ 내용 수정**")
            new_issue_text = st.text_area("개선 필요사항 (내용 수정 가능)", value=t['issue_text'], height=100)
            c1, c2 = st.columns(2)
            assignee = c1.text_input("담당자", value=t.get('assignee') or "")
            plan_due = c2.date_input("계획일정", value=pd.to_datetime(t.get('plan_due')).date() if t.get('plan_due') else date.today())
            plan_text = st.text_area("계획내용", value=t.get('plan_text') or "")
            if st.form_submit_button("저장"):
                update_task(t['id'], {"issue_text": new_issue_text, "assignee": assignee, "plan_due": str(plan_due), "plan_text": plan_text})
                st.success("완료")
                st.rerun()

with tabs[3]: # 조치 입력
    st.subheader("🛠️ 조치 결과 입력")
    all_tasks = fetch_tasks_all()
    target_tasks = [t for t in all_tasks if t['status'] != '완료']

    if not target_tasks:
        st.info("조치할 미완료 과제가 없습니다.")
        if st.button("새로고침"): clear_cache(); st.rerun()
    else:
        assignees = sorted(list(set([t.get('assignee') or "미지정" for t in target_tasks])))
        locations = sorted(list(set([t.get('location') or "미분류" for t in target_tasks])))
        
        c_filter1, c_filter2 = st.columns(2)
        sel_assignee = c_filter1.selectbox("👤 담당자 필터", ["전체"] + assignees)
        sel_location = c_filter2.selectbox("🏢 장소 필터", ["전체"] + locations)
            
        filtered_tasks = target_tasks
        if sel_assignee != "전체":
            if sel_assignee == "미지정": filtered_tasks = [t for t in filtered_tasks if not t.get('assignee')]
            else: filtered_tasks = [t for t in filtered_tasks if t.get('assignee') == sel_assignee]
        if sel_location != "전체":
             filtered_tasks = [t for t in filtered_tasks if (t.get('location') or "미분류") == sel_location]

        if not filtered_tasks: st.warning("조건에 맞는 과제가 없습니다.")
        else:
            task_map = {f"[{t['issue_date']}] {t['location']} ({t.get('assignee') or '미지정'}) - {t['issue_text'][:20]}...": t for t in filtered_tasks}
            sel_label = st.selectbox("대상 과제 선택", list(task_map.keys()))
            t = task_map[sel_label]
            
            st.divider()
            st.info(f"📌 문제 내용: {t['issue_text']}")
            
            # [추가] 계획 내용 표시 (문제 내용과 사진 사이)
            plan_txt = t.get('plan_text')
            if plan_txt:
                st.success(f"📅 계획 내용: {plan_txt}")
            else:
                st.warning("📅 계획 내용: 수립된 계획이 없습니다.")
            
            c_p1, c_p2 = st.columns(2)
            with c_p1: display_photos_grid(t.get('photos_before', []), "🔴 개선 전")
            with c_p2: display_photos_grid(t.get('photos_after', []), "🟢 개선 후 (현재)")

            with st.expander("➕ 개선 완료(After) 사진 추가"):
                act_photos = st.file_uploader("사진 업로드", type=["jpg", "png", "webp"], accept_multiple_files=True, key=f"act_up_{t['id']}")
                if act_photos and st.button("사진 저장", key=f"btn_act_{t['id']}"):
                    for f in act_photos: upload_photo(t['id'], f, photo_type="AFTER")
                    st.success("등록됨")
                    st.rerun()
            
            st.divider()
            with st.form("form_act"):
                action_text = st.text_area("조치내용", value=t.get('action_text') or "")
                action_done_date = st.date_input("완료일", value=pd.to_datetime(t.get('action_done_date')).date() if t.get('action_done_date') else date.today())
                if st.form_submit_button("조치 완료 처리", type="primary"):
                    update_task(t['id'], {"action_text": action_text, "action_done_date": str(action_done_date), "status": "완료"})
                    st.balloons()
                    st.success("완료 처리되었습니다.")
                    st.rerun()

with tabs[4]: # 조회/관리
    st.subheader("🔍 통합 조회 및 관리")
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
        
    if not filtered: st.warning("데이터가 없습니다.")
    else:
        df_list = pd.DataFrame(filtered)
        df_disp = df_list[['issue_date', 'location', 'issue_text', 'status', 'action_done_date']].copy()
        df_disp.columns = ['일시', '장소', '내용', '상태', '완료일']
        
        st.caption("목록을 클릭하면 상세 내용을 볼 수 있습니다.")
        selection = st.dataframe(df_disp, use_container_width=True, hide_index=True, height=250, on_select="rerun", selection_mode="single-row")
        
        if selection.selection.rows:
            target = filtered[selection.selection.rows[0]]
            st.divider()
            st.markdown(f"#### 🔧 상세 관리 : {target['location']}")
            c_l, c_r = st.columns([3, 1])
            c_l.info(f"내용: {target['issue_text']} | 담당: {target.get('assignee') or '-'} | 완료: {target.get('action_done_date') or '-'}")
            if c_r.button("🗑️ 삭제하기", type="primary"):
                delete_task_entirely(target['id'], target.get('photos'))
                st.success("삭제됨")
                st.rerun()

            display_photos_grid(target.get('photos_before', []), "🔴 개선 전")
            display_photos_grid(target.get('photos_after', []), "🟢 개선 후")
            
            all_p = target.get('photos', [])
            if all_p:
                with st.expander("사진 삭제 모드"):
                    cols = st.columns(4)
                    for i, p in enumerate(all_p):
                        with cols[i%4]:
                            ptype = "🟢후" if "/AFTER_" in p.get('storage_path', '') else "🔴전"
                            st.image(p['public_url'], caption=ptype, width=100)
                            if st.button("삭제", key=f"del_{p['photo_id']}"): delete_photo(p['photo_id'], p['storage_path']); st.rerun()
            
            c_add1, c_add2 = st.columns([1, 3])
            add_type = c_add1.radio("추가할 사진 타입", ["개선전(BEFORE)", "개선후(AFTER)"], horizontal=True)
            new_p = c_add2.file_uploader("사진 추가", accept_multiple_files=True, key="add_p_man")
            if new_p and c_add2.button("업로드"):
                pt = "AFTER" if "개선후" in add_type else "BEFORE"
                for f in new_p: upload_photo(target['id'], f, photo_type=pt)
                st.success("완료")
                st.rerun()
