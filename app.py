# ============================================================
# 천안공장 HACCP - Supabase 최종판 (대시보드/등록/계획/완료/보고서/CSV이전)
# - 데이터/사진: Supabase (DB + Storage)
# - 사진: 여러장 업로드, 자동 리사이즈/압축, 교체/삭제 지원
# - CSV 리스트 이전(기존 구글시트 export 등): Supabase DB로 주입
# - 보고서: 주간/월간 선택, 그래프 + 엑셀(사진 링크 포함) 출력
# ============================================================

import io
import json
import time
import uuid
import zipfile
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import altair as alt
from PIL import Image, ImageOps

# supabase python
from supabase import create_client

# -------------------------
# 0) 페이지 설정
# -------------------------
st.set_page_config(page_title="천안공장 HACCP", layout="wide")
st.title("천안공장 HACCP (Supabase)")

# -------------------------
# 1) Secrets 체크 + Supabase 연결
# -------------------------
REQUIRED_SECRETS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET"]

missing = [k for k in REQUIRED_SECRETS if k not in st.secrets]
if missing:
    st.error(f"🚨 Secrets 누락: {', '.join(missing)}")
    st.info(
        "Streamlit Cloud → Settings → Secrets 에 아래 형태로 넣으세요:\n\n"
        'SUPABASE_URL = "https://xxxx.supabase.co"\n'
        'SUPABASE_ANON_KEY = "..."  \n'
        'SUPABASE_SERVICE_KEY = "..."  \n'
        'SUPABASE_BUCKET = "haccp-photos"\n'
    )
    st.stop()

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_ANON_KEY = st.secrets["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_KEY = st.secrets["SUPABASE_SERVICE_KEY"]
SUPABASE_BUCKET = st.secrets["SUPABASE_BUCKET"]

@st.cache_resource
def get_clients():
    # anon: 읽기(대시보드 등)
    sb_anon = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    # service: 쓰기/업데이트/삭제/CSV이전
    sb_srv = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return sb_anon, sb_srv

sb, sb_admin = get_clients()

# -------------------------
# 2) 유틸 함수
# -------------------------
def _norm_str(x: Any) -> str:
    s = "" if x is None else str(x)
    s = s.strip()
    return "" if s.lower() == "nan" else s

def _parse_date_any(x: Any) -> Optional[str]:
    s = _norm_str(x)
    if not s:
        return None
    s = s.replace(".", "-").replace("/", "-")
    try:
        d = pd.to_datetime(s, errors="coerce")
        if pd.isna(d):
            return None
        return d.date().strftime("%Y-%m-%d")
    except Exception:
        return None

def _map_status(s: Any) -> str:
    s = _norm_str(s)
    if s in ["진행중", "계획수립", "완료"]:
        return s
    if "완료" in s:
        return "완료"
    if "계획" in s:
        return "계획수립"
    return "진행중"

def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def compress_images(files: List[Any], max_size: int = 1280, quality: int = 70) -> List[Tuple[str, io.BytesIO]]:
    """
    Streamlit UploadedFile 리스트를 받아서:
    - 회전 보정(exif)
    - RGB 변환
    - 최대 변 1280 리사이즈
    - JPEG quality 70 압축
    return: (파일명, BytesIO) 리스트
    """
    out = []
    for f in files:
        try:
            img = Image.open(f)
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((max_size, max_size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            buf.seek(0)
            name = f"{uuid.uuid4().hex}_{_norm_str(getattr(f, 'name', 'photo.jpg')).replace(' ', '_')}"
            if not name.lower().endswith(".jpg") and not name.lower().endswith(".jpeg"):
                name += ".jpg"
            out.append((name, buf))
        except Exception:
            # 실패하면 원본 그대로라도 올리기
            try:
                buf = io.BytesIO(f.read())
                buf.seek(0)
                name = f"{uuid.uuid4().hex}_{_norm_str(getattr(f, 'name', 'photo.bin'))}"
                out.append((name, buf))
            except Exception:
                continue
    return out

def storage_public_url(path: str) -> str:
    # public bucket 기준. 만약 private이면 signed url 로직 필요.
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{path}"

def upload_photos_to_storage(files: List[Any], folder: str) -> List[str]:
    """
    여러장 업로드 → Storage 경로 리스트 반환
    """
    if not files:
        return []
    compressed = compress_images(files)
    saved_paths = []
    for name, buf in compressed:
        storage_path = f"{folder}/{name}"
        try:
            sb_admin.storage.from_(SUPABASE_BUCKET).upload(
                path=storage_path,
                file=buf.getvalue(),
                file_options={"content-type": "image/jpeg", "upsert": "true"},
            )
            saved_paths.append(storage_path)
        except Exception as e:
            st.warning(f"업로드 실패: {name} / {e}")
    return saved_paths

def delete_photos(paths: List[str]) -> None:
    if not paths:
        return
    try:
        sb_admin.storage.from_(SUPABASE_BUCKET).remove(paths)
    except Exception as e:
        st.warning(f"삭제 실패: {e}")

def fetch_tasks(limit: int = 5000) -> pd.DataFrame:
    """
    haccp_tasks 테이블 전체를 읽어 DataFrame으로
    """
    try:
        res = sb.table("haccp_tasks").select("*").limit(limit).execute()
        data = res.data or []
        df = pd.DataFrame(data)
        if df.empty:
            return df
        # 날짜 컬럼 파싱
        for col in ["issue_date", "plan_due_date", "action_date", "created_at", "updated_at"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        # 주/월/연 파생
        if "issue_date" in df.columns:
            df["Year"] = df["issue_date"].dt.year
            df["Month"] = df["issue_date"].dt.month
            df["Week"] = df["issue_date"].dt.isocalendar().week.astype("Int64")
        return df
    except Exception as e:
        st.error(f"DB 로딩 실패: {e}")
        return pd.DataFrame()

def upsert_task_by_legacy_id(legacy_id: str, payload: Dict[str, Any]) -> None:
    # legacy_id 기준 있으면 update, 없으면 insert
    exists = sb_admin.table("haccp_tasks").select("id").eq("legacy_id", legacy_id).execute().data
    if exists:
        sb_admin.table("haccp_tasks").update(payload).eq("legacy_id", legacy_id).execute()
    else:
        payload2 = dict(payload)
        payload2["legacy_id"] = legacy_id
        sb_admin.table("haccp_tasks").insert(payload2).execute()

def safe_json_list(x: Any) -> List[str]:
    if x is None:
        return []
    if isinstance(x, list):
        return [str(v) for v in x if str(v).strip()]
    # 문자열로 들어온 경우(예: "['a','b']")
    try:
        j = json.loads(x)
        if isinstance(j, list):
            return [str(v) for v in j if str(v).strip()]
    except Exception:
        pass
    # 그냥 단일 문자열
    s = _norm_str(x)
    return [s] if s else []

# -------------------------
# 3) 사이드바 메뉴
# -------------------------
st.sidebar.markdown("## ☁️ 천안공장 HACCP")
menu = st.sidebar.radio(
    "메뉴",
    ["📊 대시보드", "📝 개선과제등록", "🧩 개선계획수립", "✅ 개선완료 입력", "🧾 보고서/출력", "📦 리스트만 이전(CSV)"],
)
st.sidebar.divider()

# -------------------------
# 4) 데이터 로드
# -------------------------
df_all = fetch_tasks()

# -------------------------
# 5) 대시보드
# -------------------------
if menu == "📊 대시보드":
    st.subheader("📊 위생점검/개선 현황")

    if df_all.empty:
        st.warning("데이터가 없습니다. (CSV 이전 또는 등록을 먼저 해주세요)")
        st.stop()

    # 기간 필터
    st.sidebar.markdown("### 📅 기간 필터")
    years = sorted([int(y) for y in df_all["Year"].dropna().unique().tolist()])
    selected_years = st.sidebar.multiselect("연도", years, default=years)

    dff = df_all.copy()
    if selected_years:
        dff = dff[dff["Year"].isin(selected_years)]

    months = sorted([int(m) for m in dff["Month"].dropna().unique().tolist()])
    month_options = [f"{m}월" for m in months]
    selected_months_str = st.sidebar.multiselect("월", month_options, default=month_options)

    if selected_months_str:
        selected_months = [int(m.replace("월", "")) for m in selected_months_str]
        dff = dff[dff["Month"].isin(selected_months)]

    weeks = sorted([int(w) for w in dff["Week"].dropna().unique().tolist()])
    week_options = [f"{w}주차" for w in weeks]
    selected_weeks_str = st.sidebar.multiselect("주차(Week)", week_options, default=week_options)

    if selected_weeks_str:
        selected_weeks = [int(w.replace("주차", "")) for w in selected_weeks_str]
        dff = dff[dff["Week"].isin(selected_weeks)]

    total_count = len(dff)
    done_count = len(dff[dff["status"] == "완료"])
    rate = (done_count / total_count * 100) if total_count else 0

    m1, m2, m3 = st.columns(3)
    m1.metric("총 발굴 건수", f"{total_count}건")
    m2.metric("개선 완료", f"{done_count}건")
    m3.metric("개선율", f"{rate:.1f}%")

    st.divider()

    # 그룹 기준: 여러월 선택이면 Month, 아니면 location
    if len(selected_months_str) > 1:
        group_col = "Month"
        x_title = "월"
        dff2 = dff.copy()
        dff2["Month"] = dff2["Month"].astype("Int64").astype(str) + "월"
        grp = "Month"
    else:
        grp = "location"
        x_title = "장소/실"

    chart_df = dff.groupby(grp).agg(
        총발생=("id", "count"),
        조치완료=("status", lambda x: (x == "완료").sum()),
    ).reset_index()

    chart_df["진행률"] = (chart_df["조치완료"] / chart_df["총발생"] * 100).fillna(0).round(1)
    chart_df["라벨"] = chart_df["진행률"].astype(str) + "%"

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**🔴 총 발생 건수 ({x_title}별)**")
        chart1 = alt.Chart(chart_df).mark_bar().encode(
            x=alt.X(f"{grp}:N", axis=alt.Axis(labelAngle=0, title=None)),
            y=alt.Y("총발생:Q"),
            tooltip=[grp, "총발생"],
        )
        st.altair_chart(chart1, use_container_width=True)

    with c2:
        st.markdown("**🟢 조치 완료율 (%)**")
        base = alt.Chart(chart_df).encode(
            x=alt.X(f"{grp}:N", axis=alt.Axis(labelAngle=0, title=None)),
            y=alt.Y("진행률:Q"),
        )
        bars = base.mark_bar()
        text = base.mark_text(dy=-15).encode(text=alt.Text("라벨:N"))
        st.altair_chart(bars + text, use_container_width=True)

    st.divider()
    st.subheader("📋 상세 내역 (최근 10건)")
    recent = dff.sort_values("issue_date", ascending=False).head(10)

    for _, r in recent.iterrows():
        icon = "✅" if r.get("status") == "완료" else "🔥"
        dstr = r["issue_date"].strftime("%Y-%m-%d") if pd.notnull(r.get("issue_date")) else ""
        summary = _norm_str(r.get("issue_text"))[:25]
        with st.expander(f"{icon} [{_norm_str(r.get('status'))}] {dstr} | {_norm_str(r.get('location'))} - {summary}..."):
            colA, colB, colC = st.columns([1, 1, 2])

            before_paths = safe_json_list(r.get("photos_before"))
            after_paths = safe_json_list(r.get("photos_after"))

            with colA:
                st.caption("❌ 개선 전")
                if before_paths:
                    for p in before_paths:
                        st.image(storage_public_url(p), use_container_width=True)
                else:
                    st.caption("-")

            with colB:
                st.caption("✅ 개선 후")
                if after_paths:
                    for p in after_paths:
                        st.image(storage_public_url(p), use_container_width=True)
                else:
                    st.caption("-")

            with colC:
                st.markdown(f"**내용:** {_norm_str(r.get('issue_text'))}")
                st.markdown(f"**발견자:** {_norm_str(r.get('reporter'))}")
                st.markdown(f"**담당자(계획):** {_norm_str(r.get('plan_assignee'))}")
                if pd.notnull(r.get("plan_due_date")):
                    st.markdown(f"**개선 일정:** {r['plan_due_date'].strftime('%Y-%m-%d')}")
                if _norm_str(r.get("plan_text")):
                    st.info(f"계획: {_norm_str(r.get('plan_text'))}")
                if _norm_str(r.get("action_text")):
                    st.success(f"조치: {_norm_str(r.get('action_text'))}")
                if pd.notnull(r.get("action_date")):
                    st.markdown(f"**완료일:** {r['action_date'].strftime('%Y-%m-%d')}")

# -------------------------
# 6) 개선과제등록
# -------------------------
elif menu == "📝 개선과제등록":
    st.subheader("📝 개선과제등록 (발견자/품질팀)")

    locations = ["전처리실", "입국실", "발효실", "제성실", "병입/포장실", "원료창고", "제품창고", "실험실", "화장실/탈의실", "기타"]

    with st.form("register_form"):
        issue_date = st.date_input("발굴 일자", value=date.today())
        location = st.selectbox("장소", locations)
        reporter = st.text_input("발견자(또는 등록자)")
        issue_text = st.text_area("개선 필요사항(내용)")
        photos_before = st.file_uploader("사진(개선 전) - 여러 장 가능", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)
        submitted = st.form_submit_button("등록 저장")

    if submitted:
        if not _norm_str(issue_text):
            st.warning("내용은 필수입니다.")
            st.stop()

        task_id = uuid.uuid4().hex
        folder = f"tasks/{task_id}/before"

        with st.spinner("사진 업로드/저장 중..."):
            before_paths = upload_photos_to_storage(photos_before or [], folder=folder)

            payload = {
                "issue_date": issue_date.strftime("%Y-%m-%d"),
                "location": location,
                "issue_text": issue_text,
                "reporter": reporter,
                "status": "진행중",
                "plan_assignee": "",
                "plan_due_date": None,
                "plan_text": "",
                "action_text": "",
                "action_date": None,
                "photos_before": before_paths,
                "photos_after": [],
                "updated_at": _now_ts(),
            }
            sb_admin.table("haccp_tasks").insert(payload).execute()

        st.success("✅ 등록 완료!")
        st.balloons()
        st.rerun()

# -------------------------
# 7) 개선계획수립 (관리자: 담당자 선정 + 일정 + 계획)
# -------------------------
elif menu == "🧩 개선계획수립":
    st.subheader("🧩 개선계획수립 (관리자용)")

    if df_all.empty:
        st.info("데이터가 없습니다.")
        st.stop()

    # 진행중인 건만
    tasks = df_all[df_all["status"].isin(["진행중", "계획수립"])].copy()
    if tasks.empty:
        st.success("🎉 계획수립할 항목이 없습니다.")
        st.stop()

    # 선택 UI
    tasks = tasks.sort_values("issue_date", ascending=False)
    options = {r["id"]: f"{_norm_str(r.get('issue_text'))[:30]}... ({_norm_str(r.get('location'))})" for _, r in tasks.iterrows()}
    selected_id = st.selectbox("계획 수립할 항목 선택", options=list(options.keys()), format_func=lambda x: options[x])

    row = tasks[tasks["id"] == selected_id].iloc[0]

    st.divider()
    c1, c2 = st.columns([1, 2])
    with c1:
        st.caption("📸 개선 전")
        for p in safe_json_list(row.get("photos_before")):
            st.image(storage_public_url(p), use_container_width=True)

    with c2:
        st.markdown(f"**발굴일:** {row['issue_date'].strftime('%Y-%m-%d') if pd.notnull(row.get('issue_date')) else ''}")
        st.markdown(f"**장소:** {_norm_str(row.get('location'))}")
        st.info(_norm_str(row.get("issue_text")))

    st.divider()

    with st.form("plan_form"):
        assignee = st.text_input("담당자 지정", value=_norm_str(row.get("plan_assignee")))
        due = st.date_input("개선 일정(목표 완료일)", value=(row["plan_due_date"].date() if pd.notnull(row.get("plan_due_date")) else date.today()))
        plan_text = st.text_area("개선 계획(메모)", value=_norm_str(row.get("plan_text")))
        ok = st.form_submit_button("계획 저장")

    if ok:
        payload = {
            "plan_assignee": assignee,
            "plan_due_date": due.strftime("%Y-%m-%d") if due else None,
            "plan_text": plan_text,
            "status": "계획수립",
            "updated_at": _now_ts(),
        }
        sb_admin.table("haccp_tasks").update(payload).eq("id", selected_id).execute()
        st.success("✅ 개선계획 저장 완료!")
        st.rerun()

# -------------------------
# 8) 개선완료 입력 (조치 내용 + 완료 사진 + 상태 완료)
# -------------------------
elif menu == "✅ 개선완료 입력":
    st.subheader("✅ 개선완료 입력")

    if df_all.empty:
        st.info("데이터가 없습니다.")
        st.stop()

    # 완료가 아닌 건만
    tasks = df_all[df_all["status"] != "완료"].copy()
    if tasks.empty:
        st.success("🎉 조치할 항목이 없습니다.")
        st.stop()

    managers = ["전체"] + sorted([_norm_str(x) for x in tasks["plan_assignee"].fillna("").unique().tolist() if _norm_str(x)])
    selected_manager = st.selectbox("담당자 필터", managers)

    if selected_manager != "전체":
        tasks = tasks[tasks["plan_assignee"] == selected_manager]

    if tasks.empty:
        st.info("해당 담당자 항목이 없습니다.")
        st.stop()

    tasks = tasks.sort_values("issue_date", ascending=False)
    options = {r["id"]: f"{_norm_str(r.get('issue_text'))[:30]}... ({_norm_str(r.get('location'))})" for _, r in tasks.iterrows()}
    selected_id = st.selectbox("완료 처리할 항목 선택", options=list(options.keys()), format_func=lambda x: options[x])

    row = tasks[tasks["id"] == selected_id].iloc[0]

    st.divider()
    c1, c2 = st.columns([1, 2])
    with c1:
        st.caption("📸 개선 전")
        for p in safe_json_list(row.get("photos_before")):
            st.image(storage_public_url(p), use_container_width=True)

    with c2:
        st.markdown(f"**장소:** {_norm_str(row.get('location'))}")
        st.markdown(f"**담당자:** {_norm_str(row.get('plan_assignee'))}")
        st.info(_norm_str(row.get("issue_text")))

    st.divider()

    with st.form("action_form"):
        action_text = st.text_area("조치 내용", value=_norm_str(row.get("action_text")))
        action_date = st.date_input("완료일", value=date.today())
        photos_after = st.file_uploader("조치 후 사진 - 여러 장 가능", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)
        ok = st.form_submit_button("완료 저장")

    if ok:
        if not _norm_str(action_text):
            st.warning("조치 내용은 필수입니다.")
            st.stop()

        folder = f"tasks/{selected_id}/after"
        with st.spinner("저장 중..."):
            after_paths = upload_photos_to_storage(photos_after or [], folder=folder)
            old_after = safe_json_list(row.get("photos_after"))
            merged_after = old_after + after_paths

            payload = {
                "action_text": action_text,
                "action_date": action_date.strftime("%Y-%m-%d") if action_date else None,
                "photos_after": merged_after,
                "status": "완료",
                "updated_at": _now_ts(),
            }
            sb_admin.table("haccp_tasks").update(payload).eq("id", selected_id).execute()

        st.success("✅ 완료 저장!")
        st.balloons()
        st.rerun()

    # 사진 교체/삭제 (선택된 row에 대해)
    st.divider()
    st.markdown("### 🧹 사진 관리 (교체/삭제)")
    st.caption("잘못 올린 사진이 있으면 삭제하거나 새로 추가할 수 있습니다.")

    before_list = safe_json_list(row.get("photos_before"))
    after_list = safe_json_list(row.get("photos_after"))

    colx, coly = st.columns(2)
    with colx:
        st.markdown("**개선 전 사진 삭제**")
        del_before = st.multiselect("삭제할 전 사진 선택", before_list, default=[])
        if st.button("전 사진 삭제 실행"):
            if del_before:
                delete_photos(del_before)
                new_list = [p for p in before_list if p not in del_before]
                sb_admin.table("haccp_tasks").update({"photos_before": new_list, "updated_at": _now_ts()}).eq("id", selected_id).execute()
                st.success("전 사진 삭제 완료")
                st.rerun()
            else:
                st.info("선택된 사진이 없습니다.")

    with coly:
        st.markdown("**개선 후 사진 삭제**")
        del_after = st.multiselect("삭제할 후 사진 선택", after_list, default=[])
        if st.button("후 사진 삭제 실행"):
            if del_after:
                delete_photos(del_after)
                new_list = [p for p in after_list if p not in del_after]
                sb_admin.table("haccp_tasks").update({"photos_after": new_list, "updated_at": _now_ts()}).eq("id", selected_id).execute()
                st.success("후 사진 삭제 완료")
                st.rerun()
            else:
                st.info("선택된 사진이 없습니다.")

    st.markdown("**사진 추가 업로드(기존에 이어붙임)**")
    add_before = st.file_uploader("전 사진 추가", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="add_before")
    add_after = st.file_uploader("후 사진 추가", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="add_after")

    col_add1, col_add2 = st.columns(2)
    with col_add1:
        if st.button("전 사진 추가 저장"):
            paths = upload_photos_to_storage(add_before or [], folder=f"tasks/{selected_id}/before")
            new_list = before_list + paths
            sb_admin.table("haccp_tasks").update({"photos_before": new_list, "updated_at": _now_ts()}).eq("id", selected_id).execute()
            st.success("전 사진 추가 완료")
            st.rerun()

    with col_add2:
        if st.button("후 사진 추가 저장"):
            paths = upload_photos_to_storage(add_after or [], folder=f"tasks/{selected_id}/after")
            new_list = after_list + paths
            sb_admin.table("haccp_tasks").update({"photos_after": new_list, "updated_at": _now_ts()}).eq("id", selected_id).execute()
            st.success("후 사진 추가 완료")
            st.rerun()

# -------------------------
# 9) 보고서/출력 (주간/월간)
# -------------------------
elif menu == "🧾 보고서/출력":
    st.subheader("🧾 보고서/출력")

    if df_all.empty:
        st.warning("데이터가 없습니다.")
        st.stop()

    # showing only meaningful columns
    df = df_all.copy()

    report_unit = st.selectbox("보고 단위", ["주간", "월간"])
    years = sorted([int(y) for y in df["Year"].dropna().unique().tolist()])
    y = st.selectbox("연도 선택", years, index=len(years)-1 if years else 0)

    df = df[df["Year"] == y].copy()

    if report_unit == "주간":
        weeks = sorted([int(w) for w in df["Week"].dropna().unique().tolist()])
        w = st.selectbox("주차 선택", weeks)
        df_r = df[df["Week"] == w].copy()
        title = f"{y}년 {w}주차 보고서"
        period_label = f"{w}주차"
    else:
        months = sorted([int(m) for m in df["Month"].dropna().unique().tolist()])
        m = st.selectbox("월 선택", months)
        df_r = df[df["Month"] == m].copy()
        title = f"{y}년 {m}월 보고서"
        period_label = f"{m}월"

    st.markdown(f"## {title}")

    total = len(df_r)
    done = len(df_r[df_r["status"] == "완료"])
    st.write(f"- 총 발굴건수: **{total}**")
    st.write(f"- 개선완료건수: **{done}**")
    st.write(f"- 개선율: **{(done/total*100):.1f}%**" if total else "- 개선율: -")

    # 전체 그래프: 상태별
    status_df = df_r.groupby("status").size().reset_index(name="count")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("### 상태별 건수")
        chart = alt.Chart(status_df).mark_bar().encode(
            x=alt.X("status:N", axis=alt.Axis(labelAngle=0)),
            y="count:Q",
            tooltip=["status", "count"],
        )
        st.altair_chart(chart, use_container_width=True)

    # 장소별 개선율
    with c2:
        st.markdown("### 장소별 개선율(완료 비율)")
        loc = df_r.groupby("location").agg(
            총발생=("id", "count"),
            완료=("status", lambda x: (x == "완료").sum()),
        ).reset_index()
        loc["개선율(%)"] = (loc["완료"] / loc["총발생"] * 100).fillna(0).round(1)

        loc_chart = alt.Chart(loc).mark_bar().encode(
            x=alt.X("location:N", axis=alt.Axis(labelAngle=0), sort="-y"),
            y=alt.Y("개선율(%):Q"),
            tooltip=["location", "총발생", "완료", "개선율(%)"],
        )
        st.altair_chart(loc_chart, use_container_width=True)

    st.divider()
    st.markdown("### 상세 리스트")
    show_cols = ["issue_date", "location", "issue_text", "reporter", "plan_assignee", "plan_due_date", "status", "action_text", "action_date"]
    for c in show_cols:
        if c not in df_r.columns:
            df_r[c] = None
    st.dataframe(df_r[show_cols].sort_values("issue_date", ascending=False), use_container_width=True)

    st.divider()
    st.markdown("## 📤 엑셀 출력 (사진 링크 포함)")

    def build_excel_with_links(dfx: pd.DataFrame) -> bytes:
        dfx = dfx.copy()
        # 사진 링크 컬럼 생성(여러장 → 줄바꿈)
        def paths_to_links(paths):
            paths = safe_json_list(paths)
            return "\n".join([storage_public_url(p) for p in paths])

        dfx["photos_before_links"] = dfx.get("photos_before", None).apply(paths_to_links) if "photos_before" in dfx.columns else ""
        dfx["photos_after_links"] = dfx.get("photos_after", None).apply(paths_to_links) if "photos_after" in dfx.columns else ""

        cols = [
            "issue_date", "location", "issue_text", "reporter",
            "plan_assignee", "plan_due_date", "plan_text",
            "status", "action_text", "action_date",
            "photos_before_links", "photos_after_links",
        ]
        for c in cols:
            if c not in dfx.columns:
                dfx[c] = ""

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            dfx[cols].to_excel(writer, index=False, sheet_name="report")
            ws = writer.sheets["report"]
            # 보기 좋게 폭
            ws.set_column(0, 0, 12)   # issue_date
            ws.set_column(1, 1, 14)   # location
            ws.set_column(2, 2, 50)   # issue_text
            ws.set_column(3, 3, 16)   # reporter
            ws.set_column(4, 4, 16)   # assignee
            ws.set_column(5, 5, 14)   # due
            ws.set_column(6, 6, 30)   # plan_text
            ws.set_column(7, 7, 10)   # status
            ws.set_column(8, 8, 30)   # action_text
            ws.set_column(9, 9, 14)   # action_date
            ws.set_column(10, 11, 60) # photo links
        output.seek(0)
        return output.getvalue()

    xbytes = build_excel_with_links(df_r)
    st.download_button(
        "⬇️ 엑셀 다운로드",
        data=xbytes,
        file_name=f"HACCP_{period_label}_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# -------------------------
# 10) CSV 리스트만 이전 (구글시트 export 등)
# -------------------------
elif menu == "📦 리스트만 이전(CSV)":
    st.subheader("📦 리스트만 이전 (CSV → Supabase DB)")
    st.info(
        "구글시트에서 CSV로 뽑은 파일을 업로드하면 DB로 들어갑니다.\n"
        "- 사진은 CSV에는 없으므로 나중에 등록/완료 메뉴에서 추가 가능\n"
        "- legacy_id(기존 ID) 기준으로 중복 방지/업데이트됩니다."
    )

    csv_up = st.file_uploader("CSV 업로드", type=["csv"])
    overwrite = st.checkbox("기존 legacy_id가 있으면 덮어쓰기(업데이트)", value=False)

    if csv_up is None:
        st.stop()

    # 미리보기
    try:
        df_csv = pd.read_csv(csv_up)
        st.markdown("### 미리보기(상위 20행)")
        st.dataframe(df_csv.head(20), use_container_width=True)
    except Exception as e:
        st.error(f"CSV 읽기 실패: {e}")
        st.stop()

    # 실행
    if st.button("🚀 리스트 이전 실행"):
        with st.spinner("이전 중..."):
            # 재로딩 위해 다시 read
            csv_up.seek(0)
            df_csv = pd.read_csv(csv_up)

            required_cols = ["ID", "일시", "공정", "개선 필요사항", "담당자", "진행상태"]
            miss = [c for c in required_cols if c not in df_csv.columns]
            if miss:
                st.error(f"필수 컬럼 누락: {', '.join(miss)}")
                st.stop()

            prog = st.progress(0)
            ok = skipped = fail = 0

            for i, r in df_csv.iterrows():
                legacy_id = _norm_str(r.get("ID"))
                if not legacy_id:
                    fail += 1
                    prog.progress((i + 1) / len(df_csv))
                    continue

                try:
                    if not overwrite:
                        exists = sb_admin.table("haccp_tasks").select("id").eq("legacy_id", legacy_id).execute().data
                        if exists:
                            skipped += 1
                            prog.progress((i + 1) / len(df_csv))
                            continue

                    payload = {
                        "issue_date": _parse_date_any(r.get("일시")),
                        "location": _norm_str(r.get("공정")),
                        "issue_text": _norm_str(r.get("개선 필요사항")),
                        "reporter": _norm_str(r.get("발견자")) if "발견자" in df_csv.columns else "",
                        "status": _map_status(r.get("진행상태")),
                        "plan_assignee": _norm_str(r.get("담당자")),
                        "plan_due_date": _parse_date_any(r.get("개선계획(일정)")) if "개선계획(일정)" in df_csv.columns else None,
                        "plan_text": "",
                        "action_text": _norm_str(r.get("개선내용")) if "개선내용" in df_csv.columns else "",
                        "action_date": _parse_date_any(r.get("개선완료일")) if "개선완료일" in df_csv.columns else None,
                        "photos_before": [],
                        "photos_after": [],
                        "updated_at": _now_ts(),
                    }

                    upsert_task_by_legacy_id(legacy_id, payload)
                    ok += 1

                except Exception as e:
                    fail += 1
                    st.warning(f"{i+1}행 실패(ID={legacy_id}): {e}")

                prog.progress((i + 1) / len(df_csv))

        st.success(f"✅ 리스트 이전 완료: 성공 {ok} / 스킵 {skipped} / 실패 {fail}")
        st.rerun()
