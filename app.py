import io
import uuid
import datetime as dt
from typing import List, Dict, Any, Optional, Tuple

import requests
import pandas as pd
import streamlit as st
import altair as alt
from PIL import Image, ImageOps

from supabase import create_client

# =============================================================================
# 기본 설정
# =============================================================================
st.set_page_config(page_title="천안공장 HACCP", layout="wide")
APP_TITLE = "천안공장 HACCP"

LOCATIONS = [
    "전처리실", "입국실", "발효실", "제성실", "병입/포장실",
    "원료창고", "제품창고", "실험실", "화장실/탈의실", "기타"
]

# =============================================================================
# Secrets 체크
# =============================================================================
REQUIRED = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET", "ADMIN_PASSWORD"]
missing = [k for k in REQUIRED if k not in st.secrets]
if missing:
    st.error(f"🚨 Secrets 누락: {', '.join(missing)}")
    st.stop()

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = st.secrets["SUPABASE_SERVICE_KEY"]
SUPABASE_BUCKET = st.secrets["SUPABASE_BUCKET"]
ADMIN_PASSWORD = st.secrets["ADMIN_PASSWORD"]

# =============================================================================
# Supabase 연결
# =============================================================================
@st.cache_resource
def sb_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

sb = sb_client()

# =============================================================================
# 유틸: 날짜
# =============================================================================
def to_date_str(d: Optional[dt.date]) -> Optional[str]:
    return d.strftime("%Y-%m-%d") if isinstance(d, dt.date) else None

def parse_date(s: Any) -> Optional[dt.date]:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return pd.to_datetime(s, errors="coerce").date()
    except Exception:
        return None

# =============================================================================
# Storage: public URL 생성/업로드/삭제
# =============================================================================
def storage_public_url(object_path: str) -> str:
    res = sb.storage.from_(SUPABASE_BUCKET).get_public_url(object_path)
    if isinstance(res, dict) and "publicUrl" in res:
        return res["publicUrl"]
    if isinstance(res, str):
        return res
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{object_path}"

def compress_images(files, max_size=(1280, 1280), quality=75) -> List[Tuple[str, bytes]]:
    out = []
    for f in files or []:
        try:
            img = Image.open(f)
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail(max_size)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            buf.seek(0)
            out.append((f.name, buf.read()))
        except Exception:
            try:
                out.append((getattr(f, "name", "file"), f.getvalue()))
            except Exception:
                pass
    return out

def upload_images(task_id: str, kind: str, images: List[Tuple[str, bytes]]) -> List[Dict[str, str]]:
    results = []
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    for _, data in images:
        object_path = f"{task_id}/{kind}/{ts}_{uuid.uuid4().hex}.jpg"
        sb.storage.from_(SUPABASE_BUCKET).upload(
            path=object_path,
            file=data,
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )
        results.append({"path": object_path, "url": storage_public_url(object_path)})
    return results

def delete_storage_objects(paths: List[str]) -> None:
    paths = [p for p in (paths or []) if p]
    if not paths:
        return
    sb.storage.from_(SUPABASE_BUCKET).remove(paths)

def fetch_image_bytes(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, timeout=12)
        if r.status_code == 200:
            return r.content
    except Exception:
        return None
    return None

# =============================================================================
# DB 함수
# =============================================================================
def db_list(date_from: Optional[dt.date], date_to: Optional[dt.date]) -> pd.DataFrame:
    q = sb.table("haccp_tasks").select("*").order("created_at", desc=True)
    if date_from:
        q = q.gte("issue_date", str(date_from))
    if date_to:
        q = q.lte("issue_date", str(date_to))
    res = q.execute()
    data = res.data or []
    df = pd.DataFrame(data)
    if df.empty:
        return df

    for col in ["issue_date", "plan_due_date", "action_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    for col in ["photos_before", "photos_after"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: x if isinstance(x, list) else [])

    if "created_at" in df.columns:
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
        df["Year"] = df["created_at"].dt.year
        df["Month"] = df["created_at"].dt.month
        df["Week"] = df["created_at"].dt.isocalendar().week.astype(int)

    return df

def db_insert(payload: Dict[str, Any]) -> Dict[str, Any]:
    res = sb.table("haccp_tasks").insert(payload).execute()
    if not res.data:
        raise RuntimeError("DB insert 실패")
    return res.data[0]

def db_get(task_id: str) -> Dict[str, Any]:
    res = sb.table("haccp_tasks").select("*").eq("id", task_id).single().execute()
    if not res.data:
        raise RuntimeError("DB select 실패")
    return res.data

def db_update(task_id: str, payload: Dict[str, Any]) -> None:
    sb.table("haccp_tasks").update(payload).eq("id", task_id).execute()

# =============================================================================
# 보고서 기간
# =============================================================================
def report_range(mode: str, base: dt.date) -> Tuple[dt.date, dt.date]:
    if mode == "주간":
        start = base - dt.timedelta(days=base.weekday())
        end = start + dt.timedelta(days=6)
        return start, end
    start = base.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1, day=1)
    else:
        next_month = start.replace(month=start.month + 1, day=1)
    end = next_month - dt.timedelta(days=1)
    return start, end

# =============================================================================
# 엑셀(사진 포함)
# =============================================================================
def build_excel_with_images(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df_out = df.copy()

        def first_url(lst):
            if isinstance(lst, list) and lst and isinstance(lst[0], dict):
                return lst[0].get("url", "")
            return ""

        df_out["사진_전_첫URL"] = df_out["photos_before"].apply(first_url) if "photos_before" in df_out else ""
        df_out["사진_후_첫URL"] = df_out["photos_after"].apply(first_url) if "photos_after" in df_out else ""

        cols = [
            "id", "issue_date", "location", "issue_text", "reporter",
            "status", "plan_assignee", "plan_due_date", "plan_text",
            "action_date", "action_text", "사진_전_첫URL", "사진_후_첫URL"
        ]
        cols = [c for c in cols if c in df_out.columns]
        df_out = df_out[cols]

        sheet = "HACCP"
        df_out.to_excel(writer, index=False, sheet_name=sheet)
        ws = writer.sheets[sheet]

        ws.set_default_row(18)
        ws.set_row(0, 20)
        ws.set_column(0, 0, 36)
        ws.set_column(1, 1, 12)
        ws.set_column(2, 2, 14)
        ws.set_column(3, 3, 50)
        ws.set_column(4, 4, 14)
        ws.set_column(5, 5, 10)
        ws.set_column(6, 6, 14)
        ws.set_column(7, 7, 12)
        ws.set_column(8, 10, 35)
        ws.set_column(11, 12, 30)

        photo_before_col = len(cols) + 1
        photo_after_col = len(cols) + 2
        ws.write(0, photo_before_col, "사진(전)")
        ws.write(0, photo_after_col, "사진(후)")
        ws.set_column(photo_before_col, photo_after_col, 22)

        for i in range(len(df)):
            r = df.iloc[i]
            excel_row = i + 1
            ws.set_row(excel_row, 110)

            b_list = r.get("photos_before", []) or []
            a_list = r.get("photos_after", []) or []
            b_url = b_list[0].get("url") if b_list and isinstance(b_list[0], dict) else None
            a_url = a_list[0].get("url") if a_list and isinstance(a_list[0], dict) else None

            if b_url:
                b = fetch_image_bytes(b_url)
                if b:
                    ws.insert_image(excel_row, photo_before_col, "before.jpg", {
                        "image_data": io.BytesIO(b),
                        "x_scale": 0.28, "y_scale": 0.28,
                        "x_offset": 2, "y_offset": 2,
                    })

            if a_url:
                b = fetch_image_bytes(a_url)
                if b:
                    ws.insert_image(excel_row, photo_after_col, "after.jpg", {
                        "image_data": io.BytesIO(b),
                        "x_scale": 0.28, "y_scale": 0.28,
                        "x_offset": 2, "y_offset": 2,
                    })

    output.seek(0)
    return output.read()

# =============================================================================
# UI
# =============================================================================
st.title(APP_TITLE)

with st.sidebar:
    st.markdown("## ☁️ HACCP 개선관리")
    menu = st.radio(
        "메뉴",
        ["📊 대시보드", "📝 개선과제등록", "🧩 개선계획수립", "✅ 개선완료 입력", "🧾 보고서/출력"],
        index=0,
    )
    st.markdown("---")
    if st.button("🔄 새로고침"):
        st.rerun()

# =============================================================================
# 대시보드
# =============================================================================
if menu == "📊 대시보드":
    st.subheader("📊 현황 대시보드")

    today = dt.date.today()
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        d_from = st.date_input("시작일", today - dt.timedelta(days=60))
    with c2:
        d_to = st.date_input("종료일", today)
    with c3:
        st.write("")
        _ = st.button("조회", use_container_width=True)

    df = db_list(d_from, d_to)
    if df.empty:
        st.info("데이터가 없습니다.")
        st.stop()

    total = len(df)
    done = len(df[df["status"] == "완료"])
    planned = len(df[df["status"] == "계획수립"])
    inprog = len(df[df["status"] == "진행중"])
    rate = (done / total * 100) if total else 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("총 발굴 건수", f"{total}건")
    m2.metric("진행중", f"{inprog}건")
    m3.metric("계획수립", f"{planned}건")
    m4.metric("완료/개선율", f"{done}건 / {rate:.1f}%")

    st.divider()

    status_df = df.groupby("status").size().reset_index(name="건수")
    chart1 = alt.Chart(status_df).mark_bar().encode(
        x=alt.X("status:N", title="상태"),
        y=alt.Y("건수:Q", title="건수"),
        tooltip=["status", "건수"]
    )
    st.altair_chart(chart1, use_container_width=True)

    st.markdown("### 🏭 장소(실)별 현황")
    loc_df = df.groupby("location").agg(
        총발굴=("id", "count"),
        완료=("status", lambda x: (x == "완료").sum())
    ).reset_index()
    loc_df["개선율(%)"] = (loc_df["완료"] / loc_df["총발굴"] * 100).fillna(0).round(1)
    st.dataframe(loc_df.sort_values("개선율(%)", ascending=False), hide_index=True, use_container_width=True)

    st.divider()
    st.markdown("### 📋 상세 내역 (최근 15건)")
    for _, r in df.head(15).iterrows():
        issue_date = r.get("issue_date")
        issue_date_str = str(issue_date) if issue_date else "-"
        title = (r.get("issue_text") or "")[:35].replace("\n", " ")
        icon = "✅" if r.get("status") == "완료" else ("🧩" if r.get("status") == "계획수립" else "🔥")

        with st.expander(f"{icon} [{r.get('status')}] {issue_date_str} | {r.get('location')} - {title}..."):
            a, b, c = st.columns([1, 1, 2])
            with a:
                st.caption("📸 개선 전")
                for img in (r.get("photos_before") or [])[:4]:
                    st.image(img.get("url"), use_container_width=True)
            with b:
                st.caption("📸 개선 후")
                for img in (r.get("photos_after") or [])[:4]:
                    st.image(img.get("url"), use_container_width=True)
            with c:
                st.write(f"**등록자:** {r.get('reporter') or '-'}")
                st.write(f"**계획 담당:** {r.get('plan_assignee') or '-'}")
                st.write(f"**계획 기한:** {r.get('plan_due_date') or '-'}")
                if r.get("plan_text"):
                    st.info(f"계획: {r.get('plan_text')}")
                if r.get("action_text"):
                    st.success(f"조치: {r.get('action_text')}")

# =============================================================================
# 개선과제등록
# =============================================================================
elif menu == "📝 개선과제등록":
    st.subheader("📝 개선과제등록")

    with st.form("form_issue"):
        c1, c2 = st.columns(2)
        with c1:
            issue_date = st.date_input("발굴일자", dt.date.today())
        with c2:
            location = st.selectbox("장소(실)", LOCATIONS)

        reporter = st.text_input("등록자(발견자)", placeholder="예: 홍길동(품질팀)")
        issue_text = st.text_area("개선 필요사항(내용)", height=120)

        photos = st.file_uploader(
            "사진(개선 전) - 여러 장 가능",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True
        )

        submitted = st.form_submit_button("✅ 등록")

    if submitted:
        if not issue_text.strip():
            st.warning("내용을 입력해주세요.")
            st.stop()

        with st.spinner("등록 중..."):
            payload = {
                "issue_date": to_date_str(issue_date),
                "location": location,
                "issue_text": issue_text,
                "reporter": reporter,
                "status": "진행중",
                "photos_before": [],
                "photos_after": [],
            }
            row = db_insert(payload)
            task_id = row["id"]

            if photos:
                imgs = compress_images(photos)
                uploaded = upload_images(str(task_id), "before", imgs)
                db_update(str(task_id), {"photos_before": uploaded})

        st.success("등록 완료!")
        st.balloons()

# =============================================================================
# 개선계획수립(관리자)
# =============================================================================
elif menu == "🧩 개선계획수립":
    st.subheader("🧩 개선계획수립 (관리자)")

    pw = st.text_input("관리자 비밀번호", type="password")
    if pw != ADMIN_PASSWORD:
        st.info("관리자 비밀번호가 필요합니다.")
        st.stop()

    df = db_list(None, None)
    target = df[df["status"] == "진행중"].copy() if not df.empty else pd.DataFrame()
    if target.empty:
        st.success("계획 수립할 항목이 없습니다.")
        st.stop()

    options = {
        r["id"]: f"{r.get('issue_date') or '-'} | {r.get('location')} | {(r.get('issue_text') or '')[:30]}..."
        for _, r in target.iterrows()
    }
    task_id = st.selectbox("계획 수립할 과제 선택", list(options.keys()), format_func=lambda x: options[x])

    row = db_get(task_id)

    c1, c2 = st.columns([1, 2])
    with c1:
        st.caption("📸 개선 전(최대 3장)")
        for img in (row.get("photos_before") or [])[:3]:
            st.image(img.get("url"), use_container_width=True)
    with c2:
        st.write(f"**발굴일:** {row.get('issue_date') or '-'}")
        st.write(f"**장소:** {row.get('location') or '-'}")
        st.write(f"**등록자:** {row.get('reporter') or '-'}")
        st.info(row.get("issue_text") or "")

    st.divider()

    with st.form("form_plan"):
        assignee = st.text_input("담당자 지정", value=row.get("plan_assignee") or "")
        due = st.date_input("개선 기한", value=parse_date(row.get("plan_due_date")) or (dt.date.today() + dt.timedelta(days=7)))
        plan_text = st.text_area("개선 계획(내용)", value=row.get("plan_text") or "", height=120)
        save = st.form_submit_button("✅ 계획 저장(상태=계획수립)")

    if save:
        db_update(task_id, {
            "plan_assignee": assignee,
            "plan_due_date": to_date_str(due),
            "plan_text": plan_text,
            "status": "계획수립",
        })
        st.success("계획 저장 완료!")
        st.rerun()

# =============================================================================
# 개선완료 입력(사진 삭제/추가 포함)
# =============================================================================
elif menu == "✅ 개선완료 입력":
    st.subheader("✅ 개선완료 입력")

    df = db_list(None, None)
    target = df[df["status"].isin(["계획수립", "진행중"])].copy() if not df.empty else pd.DataFrame()
    if target.empty:
        st.success("완료 처리할 항목이 없습니다.")
        st.stop()

    managers = ["전체"] + sorted([x for x in target["plan_assignee"].dropna().astype(str).unique().tolist() if x.strip()])
    selected = st.selectbox("담당자 필터", managers)
    if selected != "전체":
        target = target[target["plan_assignee"].astype(str) == selected]

    if target.empty:
        st.info("해당 담당자의 항목이 없습니다.")
        st.stop()

    options = {
        r["id"]: f"{r.get('issue_date') or '-'} | {r.get('location')} | {(r.get('issue_text') or '')[:30]}..."
        for _, r in target.iterrows()
    }
    task_id = st.selectbox("완료 처리할 과제 선택", list(options.keys()), format_func=lambda x: options[x])

    row = db_get(task_id)

    st.markdown("#### 선택 과제 정보")
    c1, c2 = st.columns([1, 2])
    with c1:
        st.caption("📸 개선 전")
        for img in (row.get("photos_before") or [])[:3]:
            st.image(img.get("url"), use_container_width=True)
    with c2:
        st.write(f"**계획 담당:** {row.get('plan_assignee') or '-'}")
        st.write(f"**계선기한:** {row.get('plan_due_date') or '-'}")
        if row.get("plan_text"):
            st.info(f"계획: {row.get('plan_text')}")
        st.warning(row.get("issue_text") or "")

    st.divider()
    st.markdown("### 🧹 사진 관리(삭제/추가)")

    before_list = row.get("photos_before") or []
    after_list = row.get("photos_after") or []

    colA, colB = st.columns(2)
    with colA:
        st.caption("개선 전 사진")
        if before_list:
            for idx, img in enumerate(before_list):
                st.image(img.get("url"), use_container_width=True)
                if st.button(f"🗑 전 사진 삭제 #{idx+1}", key=f"del_before_{idx}"):
                    delete_storage_objects([img.get("path")])
                    before_list = [x for j, x in enumerate(before_list) if j != idx]
                    db_update(task_id, {"photos_before": before_list})
                    st.rerun()
        else:
            st.write("-")

        add_before = st.file_uploader("전 사진 추가(여러 장)", type=["jpg","jpeg","png","webp"], accept_multiple_files=True, key="add_before")
        if add_before and st.button("➕ 전 사진 추가"):
            imgs = compress_images(add_before)
            added = upload_images(str(task_id), "before", imgs)
            before_list = before_list + added
            db_update(task_id, {"photos_before": before_list})
            st.success("전 사진 추가 완료")
            st.rerun()

    with colB:
        st.caption("개선 후 사진")
        if after_list:
            for idx, img in enumerate(after_list):
                st.image(img.get("url"), use_container_width=True)
                if st.button(f"🗑 후 사진 삭제 #{idx+1}", key=f"del_after_{idx}"):
                    delete_storage_objects([img.get("path")])
                    after_list = [x for j, x in enumerate(after_list) if j != idx]
                    db_update(task_id, {"photos_after": after_list})
                    st.rerun()
        else:
            st.write("-")

        add_after = st.file_uploader("후 사진 추가(여러 장)", type=["jpg","jpeg","png","webp"], accept_multiple_files=True, key="add_after")
        if add_after and st.button("➕ 후 사진 추가"):
            imgs = compress_images(add_after)
            added = upload_images(str(task_id), "after", imgs)
            after_list = after_list + added
            db_update(task_id, {"photos_after": after_list})
            st.success("후 사진 추가 완료")
            st.rerun()

    st.divider()

    with st.form("act_form"):
        action_text = st.text_area("개선 완료 내용", value=row.get("action_text") or "", height=120)
        action_date = st.date_input("완료일", value=parse_date(row.get("action_date")) or dt.date.today())
        save = st.form_submit_button("✅ 완료 저장(상태=완료)")

    if save:
        if not action_text.strip():
            st.warning("개선 완료 내용을 입력해주세요.")
            st.stop()
        db_update(task_id, {
            "action_text": action_text,
            "action_date": to_date_str(action_date),
            "status": "완료",
        })
        st.success("완료 저장 완료!")
        st.balloons()
        st.rerun()

# =============================================================================
# 보고서/출력
# =============================================================================
elif menu == "🧾 보고서/출력":
    st.subheader("🧾 보고서/출력")

    mode = st.radio("보고서 단위", ["주간", "월간"], horizontal=True)
    base = st.date_input("기준일", value=dt.date.today())
    d1, d2 = report_range(mode, base)
    st.info(f"선택 기간: {d1} ~ {d2}")

    df = db_list(d1, d2)
    if df.empty:
        st.warning("선택 기간 데이터가 없습니다.")
        st.stop()

    total = len(df)
    done = len(df[df["status"] == "완료"])
    rate = (done / total * 100) if total else 0

    a, b, c = st.columns(3)
    a.metric("총 발굴 건수", f"{total}건")
    b.metric("개선(완료) 건수", f"{done}건")
    c.metric("개선율", f"{rate:.1f}%")

    st.divider()

    # 일자별 추이
    tr = df.copy()
    tr["day"] = tr["issue_date"].astype(str)
    trend = tr.groupby("day").agg(
        발굴=("id", "count"),
        완료=("status", lambda x: (x == "완료").sum())
    ).reset_index()

    st.markdown("### 📈 기간 내 추이")
    chart = alt.Chart(trend).transform_fold(
        ["발굴", "완료"], as_=["구분", "건수"]
    ).mark_line(point=True).encode(
        x=alt.X("day:N", title="일자"),
        y=alt.Y("건수:Q", title="건수"),
        color="구분:N",
        tooltip=["day", "구분", "건수"]
    )
    st.altair_chart(chart, use_container_width=True)

    # 장소별
    st.markdown("### 🏭 장소(실)별 보고")
    loc_df = df.groupby("location").agg(
        총발굴=("id", "count"),
        완료=("status", lambda x: (x == "완료").sum())
    ).reset_index()
    loc_df["개선율(%)"] = (loc_df["완료"] / loc_df["총발굴"] * 100).fillna(0).round(1)

    bar = alt.Chart(loc_df).mark_bar().encode(
        x=alt.X("location:N", title="장소", sort="-y"),
        y=alt.Y("총발굴:Q", title="총 발굴"),
        tooltip=["location", "총발굴", "완료", "개선율(%)"]
    )
    st.altair_chart(bar, use_container_width=True)
    st.dataframe(loc_df.sort_values("개선율(%)", ascending=False), hide_index=True, use_container_width=True)

    st.divider()

    st.markdown("### 📦 엑셀 출력(사진 포함)")
    st.caption("안정성을 위해 각 행당 전/후 사진 1장씩만 엑셀에 삽입합니다.")
    if st.button("📥 엑셀 생성"):
        with st.spinner("엑셀 생성 중..."):
            xbytes = build_excel_with_images(df)
        filename = f"HACCP_Report_{mode}_{d1}_{d2}.xlsx"
        st.download_button(
            "✅ 엑셀 다운로드",
            data=xbytes,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        # =========================
# CSV → Supabase 마이그레이션
# =========================
import re

def extract_drive_file_id(link: str) -> str | None:
    if not isinstance(link, str) or "drive.google.com" not in link:
        return None
    # /d/<id>/
    m = re.search(r"/d/([^/]+)", link)
    if m:
        return m.group(1)
    # id=<id>
    m = re.search(r"id=([^&]+)", link)
    if m:
        return m.group(1)
    return None

def download_drive_file_public(link: str) -> bytes | None:
    """
    Drive 파일이 '링크가 있는 사람 공개'면 uc?export=download 로 받는 방식이 꽤 잘 됨.
    공개가 아니면 403/HTML 반환 -> None
    """
    file_id = extract_drive_file_id(link)
    if not file_id:
        return None

    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return None
        # 공개가 아니면 HTML이 올 때가 많음
        ct = (r.headers.get("content-type") or "").lower()
        if "text/html" in ct and len(r.content) < 200000:
            return None
        return r.content
    except Exception:
        return None

def map_status(s: str) -> str:
    s = (s or "").strip()
    if s in ["완료", "진행중", "계획수립"]:
        return s
    # 혹시 다른 값이 있으면 기본 진행중
    return "진행중"

def try_parse_plan_due(x: str) -> str | None:
    if x is None:
        return None
    t = str(x).strip()
    if not t:
        return None
    t = t.replace(".", "-").replace("/", "-")
    try:
        return pd.to_datetime(t, errors="coerce").date().strftime("%Y-%m-%d")
    except Exception:
        return None

def migrate_csv_to_supabase(csv_file) -> None:
    df = pd.read_csv(csv_file)
    if df.empty:
        st.warning("CSV가 비어있습니다.")
        return

    required_cols = ["일시", "공정", "개선 필요사항", "담당자", "진행상태"]
    for c in required_cols:
        if c not in df.columns:
            st.error(f"CSV 필수 컬럼 누락: {c}")
            return

    progress = st.progress(0)
    ok_cnt, fail_cnt, photo_fail_cnt = 0, 0, 0

    for i, r in df.iterrows():
        try:
            issue_date = None
            try:
                issue_date = pd.to_datetime(str(r.get("일시","")).strip(), errors="coerce").date()
            except Exception:
                issue_date = None

            status = map_status(str(r.get("진행상태","")))
            reporter = str(r.get("발견자","") if "발견자" in df.columns else "").strip()

            payload = {
                "issue_date": issue_date.strftime("%Y-%m-%d") if issue_date else None,
                "location": str(r.get("공정","")).strip(),
                "issue_text": str(r.get("개선 필요사항","")).strip(),
                "reporter": reporter,
                "status": status,

                # 계획
                "plan_assignee": str(r.get("담당자","")).strip(),
                "plan_due_date": try_parse_plan_due(r.get("개선계획(일정)")) if "개선계획(일정)" in df.columns else None,
                "plan_text": "",

                # 완료
                "action_text": str(r.get("개선내용","") if "개선내용" in df.columns else "").strip(),
                "action_date": try_parse_plan_due(r.get("개선완료일")) if "개선완료일" in df.columns else None,

                # 사진(초기 빈 값)
                "photos_before": [],
                "photos_after": [],
            }

            # 1) DB insert 먼저
            inserted = db_insert(payload)
            task_id = str(inserted["id"])

            # 2) 사진_전/후 (구글드라이브 링크) -> 다운로드 -> Storage 업로드 -> DB 업데이트
            before_link = str(r.get("사진_전","") if "사진_전" in df.columns else "").strip()
            after_link  = str(r.get("사진_후","") if "사진_후" in df.columns else "").strip()

            before_list, after_list = [], []

            if before_link and before_link != "nan":
                b = download_drive_file_public(before_link)
                if b:
                    before_list = upload_images(task_id, "before", [("before.jpg", b)])
                else:
                    photo_fail_cnt += 1

            if after_link and after_link != "nan":
                a = download_drive_file_public(after_link)
                if a:
                    after_list = upload_images(task_id, "after", [("after.jpg", a)])
                else:
                    photo_fail_cnt += 1

            if before_list or after_list:
                db_update(task_id, {
                    "photos_before": before_list,
                    "photos_after": after_list
                })

            ok_cnt += 1

        except Exception as e:
            fail_cnt += 1
            st.warning(f"{i+1}번째 행 실패: {e}")

        progress.progress((i + 1) / len(df))

    st.success(f"✅ 이전 완료: 성공 {ok_cnt} / 실패 {fail_cnt} (사진 다운로드 실패 {photo_fail_cnt})")


# =========================
# 📦 리스트만 이전(CSV → Supabase)
# =========================
def _norm_str(x: Any) -> str:
    s = "" if x is None else str(x)
    s = s.strip()
    return "" if s.lower() == "nan" else s

def _map_status(s: str) -> str:
    s = _norm_str(s)
    if s in ["진행중", "계획수립", "완료"]:
        return s
    # 혹시 "완료 " 같은 공백/다른 값이 섞이면 보정
    if "완료" in s:
        return "완료"
    if "계획" in s:
        return "계획수립"
    return "진행중"

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

def upsert_by_legacy(legacy_id: str, payload: Dict[str, Any]) -> None:
    """
    legacy_id가 있으면 update, 없으면 insert.
    (supabase python client에 upsert 제약이 버전에 따라 애매해서 안전하게 2-step)
    """
    legacy_id = str(legacy_id).strip()
    exists = sb.table("haccp_tasks").select("id").eq("legacy_id", legacy_id).execute().data
    if exists:
        sb.table("haccp_tasks").update(payload).eq("legacy_id", legacy_id).execute()
    else:
        payload2 = dict(payload)
        payload2["legacy_id"] = legacy_id
        sb.table("haccp_tasks").insert(payload2).execute()

def migrate_list_only(csv_file) -> None:
    df = pd.read_csv(csv_file)

    if df.empty:
        st.error("CSV가 비어있습니다.")
        return

    # 네 CSV 기준 필수 컬럼
    required_cols = ["ID", "일시", "공정", "개선 필요사항", "담당자", "진행상태"]
    for c in required_cols:
        if c not in df.columns:
            st.error(f"CSV 필수 컬럼 누락: {c}")
            return

    overwrite_existing = st.session_state.get("overwrite_existing", False)

    prog = st.progress(0)
    ok = 0
    skipped = 0
    fail = 0

    for i, r in df.iterrows():
        try:
            legacy_id = _norm_str(r.get("ID"))
            if not legacy_id:
                fail += 1
                continue

            # 기존 존재하면 스킵(덮어쓰기 선택 안 했을 때)
            if not overwrite_existing:
                exists = sb.table("haccp_tasks").select("id").eq("legacy_id", legacy_id).execute().data
                if exists:
                    skipped += 1
                    prog.progress((i + 1) / len(df))
                    continue

            payload = {
                "issue_date": _parse_date_any(r.get("일시")),
                "location": _norm_str(r.get("공정")),
                "issue_text": _norm_str(r.get("개선 필요사항")),
                "reporter": _norm_str(r.get("발견자")) if "발견자" in df.columns else "",

                "status": _map_status(r.get("진행상태")),

                # 계획(일단 담당자/일정만 옮겨두고, plan_text는 빈값)
                "plan_assignee": _norm_str(r.get("담당자")),
                "plan_due_date": _parse_date_any(r.get("개선계획(일정)")) if "개선계획(일정)" in df.columns else None,
                "plan_text": "",

                # 완료
                "action_text": _norm_str(r.get("개선내용")) if "개선내용" in df.columns else "",
                "action_date": _parse_date_any(r.get("개선완료일")) if "개선완료일" in df.columns else None,

                # ✅ 사진은 나중에 앱에서 추가할 거라 빈 배열로 유지
                "photos_before": [],
                "photos_after": [],
            }

            upsert_by_legacy(legacy_id, payload)
            ok += 1

        except Exception as e:
            fail += 1
            st.warning(f"{i+1}행 실패(ID={r.get('ID')}): {e}")

        prog.progress((i + 1) / len(df))

    st.success(f"✅ 리스트 이전 완료: 성공 {ok} / 스킵 {skipped} / 실패 {fail}")

# =========================
# 메뉴 페이지
# =========================
if menu == "📦 리스트만 이전(CSV)":
    st.subheader("📦 리스트만 이전 (CSV → Supabase DB)")
    st.caption("사진은 옮기지 않습니다. 이후 '개선과제등록/개선완료 입력'에서 사진만 추가하면 됩니다.")

    csv_up = st.file_uploader("CSV 업로드", type=["csv"])

    st.session_state["overwrite_existing"] = st.checkbox(
        "기존 legacy_id가 있으면 덮어쓰기(업데이트) 할래요",
        value=False
    )

    if csv_up and st.button("🚀 리스트 이전 실행"):
        with st.spinner("이전 중..."):
            migrate_list_only(csv_up)
        st.success("완료!")


