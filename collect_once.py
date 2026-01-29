import os
import tinytuya
import requests
import time
from datetime import datetime
import pytz
from supabase import create_client

# =======================================================
# [1] 환경변수 & 기본 설정
# =======================================================
API_KEY = os.environ.get("TUYA_API_KEY")
API_SECRET = os.environ.get("TUYA_API_SECRET")
REGION = "us"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# [기본 센서 목록]
# DB 연결에 실패하거나 초기 상태일 때 사용할 기본값입니다.
# ID는 불변이므로 여기에 고정해둡니다.
SENSORS_BASE = [
    {"name": "1호기", "id": "ebb5a8087eed5151f182k1", "place": "쌀창고"},
    {"name": "2호기", "id": "ebef0c9ce87b7e7929baam", "place": "전처리실"},
    {"name": "3호기", "id": "eb6b6b314e849b6078juue", "place": "전처리실"},
    {"name": "4호기", "id": "eb10b12a8bbd70fa3d7j0w", "place": "전처리실"},
    {"name": "5호기", "id": "eb6c369e60371c40addr3z", "place": "양조실"},
    {"name": "6호기", "id": "eba9084fba86a454cbflqo", "place": "양조실"},
    {"name": "7호기", "id": "eb525a245eaec6b9eftuse", "place": "양조실"},
    {"name": "8호기", "id": "eba906355738db4525miqb", "place": "제품포장실"},
    {"name": "9호기", "id": "eb32026565a040ba90opj8", "place": "제품포장실"},
    {"name": "10호기", "id": "ebef6f23e7c1071a83njws", "place": "부자재창고"},
]

# [기본 알림 기준]
# DB에서 설정을 못 가져올 경우 사용할 안전장치입니다.
DEFAULT_ALARM_CONFIG = {
    "default": (0.0, 35.0)
}

def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL: return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={
            "content": message, 
            "username": "천안공장 상황실"
        })
    except: pass

# =======================================================
# [2] 메인 로직
# =======================================================
print("🏭 [GitHub Action] 센서 수집 시작 (DB 설정 동기화)...")

try:
    if not API_KEY or not SUPABASE_URL:
        raise Exception("환경변수(Secrets)가 설정되지 않았습니다.")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    # -----------------------------------------------------------
    # ★ [핵심] DB에서 최신 설정(위치 & 온도기준) 가져오기
    # -----------------------------------------------------------
    
    # 1. 위치 정보 (Mapping) 가져오기
    # 예: {'1호기': '제2숙성실', ...}
    current_mapping = {}
    try:
        res_map = supabase.table("sensor_mapping").select("*").execute()
        if res_map.data:
            current_mapping = {item['sensor_id']: item['room_name'] for item in res_map.data}
            print(f"✅ 최신 위치 정보 {len(current_mapping)}건 로드 완료")
    except Exception as e:
        print(f"⚠️ 위치 정보 로드 실패 (기본값 사용): {e}")

    # 2. 온도 기준 (Settings) 가져오기
    # 예: {'전처리실': (10.0, 28.0), ...}
    current_limits = {}
    try:
        res_set = supabase.table("room_settings").select("*").execute()
        if res_set.data:
            for item in res_set.data:
                current_limits[item['room_name']] = (float(item['min_temp']), float(item['max_temp']))
            print(f"✅ 최신 온도 기준 {len(current_limits)}건 로드 완료")
    except Exception as e:
        print(f"⚠️ 온도 기준 로드 실패 (기본값 사용): {e}")

    # -----------------------------------------------------------

    kst = pytz.timezone('Asia/Seoul')
    now_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S%z")
    alert_messages = []
    
    for sensor in SENSORS_BASE:
        # 1) 현재 센서의 '진짜 위치' 찾기 (DB값 우선, 없으면 기본값)
        current_place = current_mapping.get(sensor['name'], sensor['place'])
        
        # 2) Tuya 데이터 수집
        uri = f'/v1.0/devices/{sensor["id"]}/status'
        res = cloud.cloudrequest(uri)
        
        temp = -999
        humid = -999
        
        if res and 'result' in res:
            for item in res['result']:
                if item['code'] == 'temp_current':
                    val = float(item['value'])
                    # ★ [10호기 보정] 40(4.0도) 이상이면 10으로 나눔
                    temp = val / 10.0 if val > 40 else val
                elif item['code'] == 'humidity_value':
                    val = float(item['value'])
                    humid = val / 10.0 if val > 100 else val
        
        if temp != -999:
            # 3) 현재 위치에 맞는 '온도 기준' 찾기 (DB값 우선, 없으면 default)
            min_v, max_v = current_limits.get(current_place, DEFAULT_ALARM_CONFIG["default"])
            
            # 4) 상태 판단
            current_status = "비정상" if (temp < min_v or temp > max_v) else "정상"
            
            # 5) DB 저장 (변경된 장소 이름으로 저장됨)
            supabase.table("sensor_logs").insert({
                "place": sensor['name'], 
                "temperature": temp, 
                "humidity": humid,
                "status": current_status, 
                "created_at": now_str, 
                "room_name": current_place
            }).execute()

            # 6) 스마트 알림 (이전 상태와 비교)
            # 가장 최근 기록 1개를 가져와서 비교
            last_log = supabase.table("sensor_logs").select("status")\
                .eq("place", sensor['name'])\
                .order("created_at", desc=True)\
                .limit(1).execute()
            
            prev_status = "정상"
            if last_log.data: prev_status = last_log.data[0]['status']

            if current_status == "비정상" and prev_status != "비정상":
                alert_messages.append(f"🔥 **{current_place} ({sensor['name']}) 온도 이탈!**\n> 현재: {temp}℃ (기준: {min_v}~{max_v}℃)")
            elif current_status == "정상" and prev_status == "비정상":
                alert_messages.append(f"✅ **{current_place} ({sensor['name']}) 온도 복구**\n> 현재: {temp}℃")

    if alert_messages:
        send_discord_alert("## 📢 천안공장 상황 알림\n" + "\n".join(alert_messages))
    else:
        print("🕊️ 특이사항 없음")

except Exception as e:
    print(f"❌ 오류 발생: {e}")
    exit(1)
