import os
import io
import uuid
import json
import math
import datetime as dt
from typing import List, Dict, Optional, Tuple

import pandas as pd
import streamlit as st
from PIL import Image

import requests
import matplotlib.pyplot as plt

from supabase import create_client


# =========================
# 기본 설정
# =========================
st.set_page_config(page_title="천안공장 HACCP 개선관리", layout="wide")

REQUIRED_SECRETS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET"]

STATUS_OPTIONS = ["진행중", "완료"]
DATE_FMT = "%Y-%m-%d"


# =========================
# 유틸: 안전 체크
# =========================
def require_secrets():
    missing = [k for k in REQUIRED_SECRETS if k not in st.secrets or not str(st.secrets.get(k, "")).strip()]
    if missing:
        st.error(f"🚨 Secrets 누락: {', '.join(missing)}")
        st.stop()


@st.cache_resource
def get_supabase():
    url = st.secrets["SUPABASE_URL"]
    service_key = st.secrets["SUPABASE_SERVICE_KEY"]
    return create_client(url, service_key)  # 서비스키로 고정(권한 문제 최소화)


def today_date() -> dt.date:
    return dt.date.today()


def to_date(x) -> Optional[dt.date]:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    if isinstance(x, dt.date):
        return x
    if isinstance(x, dt.datetime):
        return x.date()
    s = str(x).strip()
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:10], DATE_FMT).date()
    except Exception:
        return None


def safe_text(x) -> str:
    if x is None:
        return ""
    return str(x)


# =========================
# 이미지: 리사이즈/압축
# =========================
def compress_image(file_bytes: bytes, max_side: int = 1600, quality: int = 82) -> Tuple[bytes, str]:
    """
    - 긴 변 max_side로 축소
    - JPEG로 변환(용량 절감)
    - PNG/WebP 등 들어와도 JPEG로 통일(호환성↑)
    """
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), "jpg"


# =========================
# Supabase: CRUD
# =========================
def db_insert_task(sb, issue_date: dt.date, location: str, issue_text: str, reporter: str) -> str:
    payload = {
        "issue_date": issue_date.isoformat(),
        "location": location,
        "issue_text": issue_text,
        "reporter": reporter,
        "status": "진행중",
    }
    res = sb.table("haccp_tasks").insert(payload).execute()
    # supabase-py v2: res.data
    task_id = res.data[0]["id"]
    return task_id


def db_update_task(sb, task_id: str, patch: Dict):
    # date는 iso string으로
    patch2 = {}
    for k, v in patch.items():
        if isinstance(v, (dt.date, dt.datetime)):
            patch2[k] = v.date().isoformat() if isinstance(v, dt.datetime) else v.isoformat()
        else:
            patch2[k] = v
    sb.table("haccp_tasks").update(patch2).eq("id", task_id).execute()


def db_delete_task(sb, task_id: str):
    # FK cascade로 photos row는 지워짐. storage 파일은 별도 삭제 필요(아래에서 처리)
    sb.table("haccp_tasks").delete().eq("id", task_id).execute()


def db_list_tasks(sb) -> pd.DataFrame:
    res = sb.table("v_haccp_tasks").select("*").order("issue_date", desc=True).execute()
    df = pd.DataFrame(res.data or [])
    if df.empty:
        return df
    # 정리
    df["issue_date"] = pd.to_datetime(df["issue_date"], errors="coerce").dt.date
    df["plan_date"] = pd.to_datetime(df.get("plan_date"), errors="coerce").dt.date
    df["done_date"] = pd.to_datetime(df.get("done_date"), errors="coerce").dt.date
    return df


def db_list_photos(sb, task_id: str) -> pd.DataFrame:
    res = sb.table("haccp_task_photos").select("*").eq("task_id", task_id).order("created_at", desc=False).execute()
    return pd.DataFrame(res.data or [])


def storage_public_url(sb, bucket: str, path: str) -> str:
    # supabase storage public url
    return sb.storage.from_(bucket).get_public_url(path)


def storage_upload_photos(sb, bucket: str, task_id: str, files: List) -> List[Dict]:
    """
    files: streamlit UploadedFile list
    returns rows inserted
    """
    inserted = []
    for uf in files:
        raw = uf.getvalue()
        comp, ext = compress_image(raw, max_side=1600, quality=82)

        file_id = str(uuid.uuid4())
        storage_path = f"{task_id}/{file_id}.{ext}"

        sb.storage.from_(bucket).upload(
            path=storage_path,
            file=comp,
            file_options={"content-type": "image/jpeg", "upsert": "true"},
        )

        url = storage_public_url(sb, bucket, storage_path)
        row = {
            "task_id": task_id,
            "file_path": storage_path,
            "file_url": url,
            "file_name": uf.name,
        }
        res = sb.table("haccp_task_photos").insert(row).execute()
        inserted.append(res.data[0])
    return inserted


def storage_delete_photo(sb, bucket: str, photo_row: Dict):
    # storage 삭제
    path = photo_row["file_path"]
    sb.storage.from_(bucket).remove([path])
    # db 삭제
    sb.table("haccp_task_photos").delete().eq("id", photo_row["id"]).execute()


# =========================
# 리포트(엑셀 + 사진 삽입)
# =========================
def build_report_excel(sb, bucket: str, df: pd.DataFrame, include_images: bool = True) -> bytes:
    """
    xlsxwriter로:
    - 표 출력
    - 첫 사진(대표사진) 다운로드해서 셀에 삽입(가능하면)
    """
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        sheet_name = "보고서"
        cols = [
            "issue_date", "location", "reporter", "status",
            "assignee", "plan_date", "done_date",
            "issue_text", "plan_text", "done_text",
            "photo_count", "first_photo_url"
        ]
        df2 = df.copy()
        for c in cols:
            if c not in df2.columns:
                df2[c] = ""
        df2 = df2[cols]

        df2.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]
        wb = writer.book

        # 보기 좋게
        wrap = wb.add_format({"text_wrap": True, "valign": "top"})
        ws.set_column(0, 0, 12)   # issue_date
        ws.set_column(1, 1, 14)   # location
        ws.set_column(2, 2, 12)   # reporter
        ws.set_column(3, 3, 10)   # status
        ws.set_column(4, 6, 12)   # assignee, plan_date, done_date
        ws.set_column(7, 9, 40, wrap)  # texts
        ws.set_column(10, 11, 18)  # photo count/url

        # 이미지 컬럼 추가(맨 오른쪽)
        img_col = len(cols) + 1
        ws.write(0, img_col, "대표사진")
        ws.set_column(img_col, img_col, 22)

        if include_images:
            for i, row in df2.iterrows():
                url = row.get("first_photo_url", "")
                if not url:
                    continue
                try:
                    r = requests.get(url, timeout=10)
                    if r.status_code != 200:
                        continue
                    img_bytes = r.content

                    # 엑셀은 jpg/png가 안정적 → 이미 저장이 jpg라 그대로 시도
                    imgdata = io.BytesIO(img_bytes)

                    # 행 높이 키우기
                    excel_row = i + 1
                    ws.set_row(excel_row, 110)

                    # 삽입(크기 조정)
                    ws.insert_image(excel_row, img_col, "photo.jpg", {
                        "image_data": imgdata,
                        "x_scale": 0.35,
                        "y_scale": 0.35,
                        "object_position": 1,
                    })
                except Exception:
                    continue

    return output.getvalue()


# =========================
# 기간 필터(주간/월간)
# =========================
def period_range(unit: str, base: dt.date) -> Tuple[dt.date, dt.date]:
    """
    return (start, end_exclusive)
    """
    if unit == "주간":
        # 월요일 시작
        start = base - dt.timedelta(days=base.weekday())
        end = start + dt.timedelta(days=7)
        return start, end
    # 월간
    start = base.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1)
    else:
        end = start.replace(month=start.month + 1, day=1)
    return start, end


# =========================
# 차트(Altair 대신 matplotlib)
# =========================
def chart_by_location(df: pd.DataFrame):
    if df.empty:
        st.info("데이터가 없습니다.")
        return

    # location별 발굴/완료 집계
    g_all = df.groupby("location", dropna=False).size().rename("발굴").reset_index()
    g_done = df[df["status"] == "완료"].groupby("location", dropna=False).size().rename("완료").reset_index()
    g = pd.merge(g_all, g_done, on="location", how="left").fillna(0)
    g["완료"] = g["완료"].astype(int)
    g = g.sort_values("발굴", ascending=False)

    fig = plt.figure()
    x = range(len(g))
    plt.bar(x, g["발굴"])
    plt.xticks(x, g["location"], rotation=30, ha="right")
    plt.title("공정/장소별 발굴 건수")
    st.pyplot(fig, clear_figure=True)

    fig2 = plt.figure()
    x2 = range(len(g))
    plt.bar(x2, g["완료"])
    plt.xticks(x2, g["location"], rotation=30, ha="right")
    plt.title("공정/장소별 완료 건수")
    st.pyplot(fig2, clear_figure=True)

    st.dataframe(g, use_container_width=True)


# =========================
# UI
# =========================
def page_dashboard(sb, bucket: str):
    st.header("대시보드/보고서")

    unit = st.selectbox("기간 단위", ["월간", "주간"], index=0)
    base = st.date_input("기준일(아무 날짜)", value=today_date())

    start, end = period_range(unit, base)

    df = db_list_tasks(sb)
    if df.empty:
        st.info("등록된 데이터가 없습니다.")
        return

    # 기간 필터
    dfp = df[(pd.to_datetime(df["issue_date"]) >= pd.to_datetime(start)) &
             (pd.to_datetime(df["issue_date"]) < pd.to_datetime(end))].copy()

    total = len(dfp)
    done = int((dfp["status"] == "완료").sum()) if total else 0
    rate = (done / total * 100) if total else 0.0

    c1, c2, c3 = st.columns(3)
    c1.metric("총 발굴건수", total)
    c2.metric("개선완료 건수", done)
    c3.metric("완료율", f"{rate:.1f}%")

    st.subheader("공정/장소별 발굴 vs 완료")
    chart_by_location(dfp)

    st.divider()
    st.subheader("보고서 출력(엑셀)")

    include_images = st.checkbox("엑셀에 대표사진도 넣기(느릴 수 있음)", value=True)

    if st.button("📥 보고서 엑셀 생성", type="primary"):
        with st.spinner("엑셀 생성 중..."):
            xlsx = build_report_excel(sb, bucket, dfp, include_images=include_images)
        st.download_button(
            "다운로드",
            data=xlsx,
            file_name=f"HACCP_보고서_{unit}_{start}_{(end - dt.timedelta(days=1))}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


def page_register(sb, bucket: str):
    st.header("개선과제등록 (발굴/등록)")

    with st.form("register_form", clear_on_submit=True):
        issue_date = st.date_input("일시", value=today_date())
        location = st.text_input("공정/장소", placeholder="예: 전처리실")
        reporter = st.text_input("발견자", placeholder="예: 품질보증팀")
        issue_text = st.text_area("개선 필요사항", placeholder="무엇이 문제인지 구체적으로 작성", height=120)

        photos = st.file_uploader(
            "사진 업로드 (여러 장 가능 / 자동 리사이즈·압축)",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True
        )

        submitted = st.form_submit_button("✅ 등록하기")

    if submitted:
        if not location.strip() or not reporter.strip() or not issue_text.strip():
            st.error("공정/장소, 발견자, 개선 필요사항은 필수입니다.")
            return

        try:
            task_id = db_insert_task(sb, issue_date, location.strip(), issue_text.strip(), reporter.strip())

            if photos:
                storage_upload_photos(sb, bucket, task_id, photos)

            st.success("등록 완료!")
            st.info("이제 '개선계획수립'에서 담당자/일정을 입력하세요.")
        except Exception as e:
            st.error("등록 실패")
            st.code(str(e))


def page_plan(sb, bucket: str):
    st.header("개선계획수립 (담당자/일정)")

    df = db_list_tasks(sb)
    if df.empty:
        st.info("데이터가 없습니다.")
        return

    # 진행중 우선
    df_open = df[df["status"] != "완료"].copy()
    if df_open.empty:
        st.info("진행중 과제가 없습니다.")
        return

    df_open["label"] = df_open.apply(
        lambda r: f"{r['issue_date']} | {r['location']} | {safe_text(r['issue_text'])[:30]}... ({r['reporter']})",
        axis=1
    )
    pick = st.selectbox("계획 수립할 과제 선택", df_open["label"].tolist())
    row = df_open[df_open["label"] == pick].iloc[0]
    task_id = row["id"]

    st.write("선택 과제")
    st.dataframe(pd.DataFrame([row.drop(labels=["label"])]), use_container_width=True)

    with st.form("plan_form"):
        assignee = st.text_input("담당자", value=safe_text(row.get("assignee", "")), placeholder="예: 생산팀")
        plan_date = st.date_input("개선계획(일정)", value=to_date(row.get("plan_date")) or today_date())
        plan_text = st.text_area("개선계획 내용(선택)", value=safe_text(row.get("plan_text", "")), height=120)
        ok = st.form_submit_button("✅ 계획 저장")

    if ok:
        try:
            db_update_task(sb, task_id, {
                "assignee": assignee.strip() if assignee else None,
                "plan_date": plan_date,
                "plan_text": plan_text.strip() if plan_text else None,
            })
            st.success("저장 완료!")
        except Exception as e:
            st.error("저장 실패")
            st.code(str(e))


def page_done(sb, bucket: str):
    st.header("개선완료 입력")

    df = db_list_tasks(sb)
    if df.empty:
        st.info("데이터가 없습니다.")
        return

    df_open = df[df["status"] != "완료"].copy()
    if df_open.empty:
        st.info("완료 처리할 진행중 과제가 없습니다.")
        return

    df_open["label"] = df_open.apply(
        lambda r: f"{r['issue_date']} | {r['location']} | {safe_text(r['issue_text'])[:30]}... ({r['reporter']})",
        axis=1
    )
    pick = st.selectbox("완료 입력할 과제 선택", df_open["label"].tolist())
    row = df_open[df_open["label"] == pick].iloc[0]
    task_id = row["id"]

    st.write("선택 과제")
    st.dataframe(pd.DataFrame([row.drop(labels=["label"])]), use_container_width=True)

    with st.form("done_form"):
        done_date = st.date_input("개선완료일", value=today_date())
        done_text = st.text_area("개선내용", height=140, placeholder="무엇을 어떻게 개선했는지 작성")
        more_photos = st.file_uploader(
            "완료 사진 추가 업로드(선택 / 여러 장 가능)",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True
        )
        ok = st.form_submit_button("✅ 완료 처리")

    if ok:
        if not done_text.strip():
            st.error("개선내용은 필수입니다.")
            return
        try:
            db_update_task(sb, task_id, {
                "status": "완료",
                "done_date": done_date,
                "done_text": done_text.strip(),
            })
            if more_photos:
                storage_upload_photos(sb, bucket, task_id, more_photos)

            st.success("완료 처리되었습니다!")
        except Exception as e:
            st.error("완료 처리 실패")
            st.code(str(e))


def page_manage(sb, bucket: str):
    st.header("조회/관리")

    df = db_list_tasks(sb)
    if df.empty:
        st.info("데이터가 없습니다.")
        return

    # 필터
    col1, col2, col3 = st.columns(3)
    with col1:
        f_status = st.selectbox("상태", ["전체"] + STATUS_OPTIONS, index=0)
    with col2:
        f_location = st.text_input("공정/장소 검색", placeholder="예: 전처리실")
    with col3:
        f_reporter = st.text_input("발견자 검색", placeholder="예: 품질보증팀")

    df2 = df.copy()
    if f_status != "전체":
        df2 = df2[df2["status"] == f_status]
    if f_location.strip():
        df2 = df2[df2["location"].fillna("").str.contains(f_location.strip(), case=False)]
    if f_reporter.strip():
        df2 = df2[df2["reporter"].fillna("").str.contains(f_reporter.strip(), case=False)]

    st.caption(f"검색 결과: {len(df2)}건")
    # 화면용 컬럼
    show_cols = ["issue_date", "location", "issue_text", "reporter", "status", "assignee", "plan_date", "done_date", "photo_count"]
    for c in show_cols:
        if c not in df2.columns:
            df2[c] = ""
    st.dataframe(df2[show_cols], use_container_width=True, height=320)

    st.divider()
    st.subheader("상세 보기 / 사진 관리")

    # 상세 선택
    df2 = df2.reset_index(drop=True)
    df2["label"] = df2.apply(lambda r: f"{r['issue_date']} | {r['location']} | {safe_text(r['issue_text'])[:30]}...", axis=1)
    pick = st.selectbox("상세로 볼 과제 선택", df2["label"].tolist())
    row = df2[df2["label"] == pick].iloc[0]
    task_id = row["id"]

    st.write("과제 정보")
    st.json({
        "id": task_id,
        "issue_date": str(row.get("issue_date")),
        "location": row.get("location"),
        "reporter": row.get("reporter"),
        "status": row.get("status"),
        "assignee": row.get("assignee"),
        "plan_date": str(row.get("plan_date")),
        "done_date": str(row.get("done_date")),
        "issue_text": row.get("issue_text"),
        "plan_text": row.get("plan_text"),
        "done_text": row.get("done_text"),
    })

    # 사진 목록
    photos_df = db_list_photos(sb, task_id)
    if photos_df.empty:
        st.info("등록된 사진이 없습니다.")
    else:
        st.write(f"사진 {len(photos_df)}장")
        cols = st.columns(3)
        for i, p in photos_df.iterrows():
            with cols[i % 3]:
                st.image(p["file_url"], caption=safe_text(p.get("file_name", "")), use_container_width=True)
                if st.button("🗑 삭제", key=f"del_{p['id']}"):
                    try:
                        storage_delete_photo(sb, bucket, p.to_dict())
                        st.success("삭제 완료")
                        st.rerun()
                    except Exception as e:
                        st.error("삭제 실패")
                        st.code(str(e))

    st.divider()
    st.subheader("사진 추가(다중 업로드)")
    new_photos = st.file_uploader("추가 사진", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="add_photos")
    if st.button("➕ 사진 추가 업로드"):
        if not new_photos:
            st.warning("추가할 사진을 선택하세요.")
        else:
            try:
                storage_upload_photos(sb, bucket, task_id, new_photos)
                st.success("업로드 완료")
                st.rerun()
            except Exception as e:
                st.error("업로드 실패")
                st.code(str(e))

    st.divider()
    st.subheader("과제 수정 / 삭제")
    with st.form("edit_task"):
        status = st.selectbox("상태", STATUS_OPTIONS, index=STATUS_OPTIONS.index(row.get("status", "진행중")))
        assignee = st.text_input("담당자", value=safe_text(row.get("assignee", "")))
        plan_date = st.date_input("개선계획(일정)", value=to_date(row.get("plan_date")) or today_date())
        done_date = st.date_input("개선완료일(완료 시)", value=to_date(row.get("done_date")) or today_date())
        issue_text = st.text_area("개선 필요사항", value=safe_text(row.get("issue_text", "")), height=120)
        plan_text = st.text_area("개선계획 내용", value=safe_text(row.get("plan_text", "")), height=100)
        done_text = st.text_area("개선내용", value=safe_text(row.get("done_text", "")), height=100)

        ok = st.form_submit_button("💾 저장")

    if ok:
        try:
            patch = {
                "status": status,
                "assignee": assignee.strip() if assignee else None,
                "plan_date": plan_date if plan_date else None,
                "done_date": done_date if (status == "완료") else None,
                "issue_text": issue_text.strip(),
                "plan_text": plan_text.strip() if plan_text else None,
                "done_text": done_text.strip() if done_text else None,
            }
            db_update_task(sb, task_id, patch)
            st.success("저장 완료")
            st.rerun()
        except Exception as e:
            st.error("저장 실패")
            st.code(str(e))

    st.warning("⚠️ 삭제는 되돌릴 수 없습니다.")
    if st.button("🧨 과제 삭제(사진 포함)"):
        try:
            # storage 파일도 지우기 위해 photos 먼저 가져와 삭제
            photos_df2 = db_list_photos(sb, task_id)
            for _, p in photos_df2.iterrows():
                try:
                    sb.storage.from_(bucket).remove([p["file_path"]])
                except Exception:
                    pass
            db_delete_task(sb, task_id)
            st.success("삭제 완료")
            st.rerun()
        except Exception as e:
            st.error("삭제 실패")
            st.code(str(e))


# =========================
# 메인
# =========================
def main():
    st.title("천안공장 HACCP 개선관리")

    require_secrets()
    sb = get_supabase()
    bucket = st.secrets["SUPABASE_BUCKET"]

    with st.expander("✅ 운영 체크(필수 설정 확인)", expanded=False):
        st.write("Supabase URL / Keys / Bucket 이름이 정확한지 확인하세요.")
        st.code(
            "\n".join([
                f"SUPABASE_URL: {'OK' if st.secrets.get('SUPABASE_URL') else 'MISSING'}",
                f"SUPABASE_ANON_KEY: {'OK' if st.secrets.get('SUPABASE_ANON_KEY') else 'MISSING'}",
                f"SUPABASE_SERVICE_KEY: {'OK' if st.secrets.get('SUPABASE_SERVICE_KEY') else 'MISSING'}",
                f"SUPABASE_BUCKET: {st.secrets.get('SUPABASE_BUCKET', '')}",
            ])
        )

    tabs = ["대시보드/보고서", "개선과제등록", "개선계획수립", "개선완료 입력", "조회/관리"]
    choice = st.tabs(tabs)

    with choice[0]:
        page_dashboard(sb, bucket)
    with choice[1]:
        page_register(sb, bucket)
    with choice[2]:
        page_plan(sb, bucket)
    with choice[3]:
        page_done(sb, bucket)
    with choice[4]:
        page_manage(sb, bucket)


if __name__ == "__main__":
    main()
