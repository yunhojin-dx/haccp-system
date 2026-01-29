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

DEFAULT_ALARM_CONFIG = {"default": (0.0, 35.0)}

def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL: return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message, "username": "천안공장 상황실"})
    except: pass

# =======================================================
# [2] 메인 로직
# =======================================================
print("🏭 [DB 이름 매칭 모드] 센서 수집 시작...")

try:
    if not API_KEY or not SUPABASE_URL:
        raise Exception("환경변수(Secrets) 오류")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    # -----------------------------------------------------------
    # 1. DB 설정 로드 (ID가 아니라 '이름'을 기준으로 매핑)
    # -----------------------------------------------------------
    current_mapping = {}
    current_limits = {}

    try:
        res_map = supabase.table("sensor_mapping").select("*").execute()
        if res_map.data:
            # ★ 핵심 수정: DB의 sensor_id가 '1호기'처럼 되어 있으니 그걸 키로 잡음
            current_mapping = {item['sensor_id']: item['room_name'] for item in res_map.data}
            print(f"✅ 위치 DB 로드 완료: {len(current_mapping)}개")
    except: print("⚠️ 위치 DB 로드 실패")

    try:
        res_set = supabase.table("room_settings").select("*").execute()
        if res_set.data:
            for item in res_set.data:
                current_limits[item['room_name']] = (float(item['min_temp']), float(item['max_temp']))
            print(f"✅ 온도 기준 DB 로드 완료: {len(current_limits)}개")
    except: print("⚠️ 온도 기준 DB 로드 실패")
    
    # -----------------------------------------------------------

    kst = pytz.timezone('Asia/Seoul')
    now_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S%z")
    alert_messages = []
    
    for sensor in SENSORS_BASE:
        # ★ [핵심] 로봇은 이제 'ebb...'가 아니라 '1호기'라는 이름으로 DB를 찾습니다.
        # DB에 '1호기'라고 적혀있으면 그 장소 이름을 가져옵니다.
        real_place_name = current_mapping.get(sensor['name'], sensor['place'])
        
        # Tuya 데이터 수집
        uri = f'/v1.0/devices/{sensor["id"]}/status'
        res = cloud.cloudrequest(uri)
        
        temp = -999
        if res and 'result' in res:
            for item in res['result']:
                if item['code'] == 'temp_current':
                    val = float(item['value'])
                    temp = val / 10.0 if val > 40 else val
        
        if temp != -999:
            min_v, max_v = current_limits.get(real_place_name, DEFAULT_ALARM_CONFIG["default"])
            
            # 상태 판단
            current_status = "비정상" if (temp < min_v or temp > max_v) else "정상"
            
            # DB 저장
            supabase.table("sensor_logs").insert({
                "place": sensor['name'], 
                "temperature": temp, 
                "status": current_status, 
                "created_at": now_str, 
                "room_name": real_place_name
            }).execute()

            # 로그 출력
            print(f"🔍 [{sensor['name']}] -> 위치: {real_place_name} | 온도: {temp} | 상태: {current_status}")

            # 알림 로직 (상태 변화 시)
            last_log = supabase.table("sensor_logs").select("status").eq("place", sensor['name']).order("created_at", desc=True).limit(1).execute()
            prev_status = "정상"
            if last_log.data: prev_status = last_log.data[0]['status']

            if current_status == "비정상" and prev_status != "비정상":
                alert_messages.append(f"🔥 **{real_place_name} ({sensor['name']}) 온도 이탈!**\n> 현재: {temp}℃ (기준: {min_v}~{max_v}℃)")
            elif current_status == "정상" and prev_status == "비정상":
                alert_messages.append(f"✅ **{real_place_name} ({sensor['name']}) 온도 복구**\n> 현재: {temp}℃")

    if alert_messages:
        send_discord_alert("## 📢 천안공장 상황 알림\n" + "\n".join(alert_messages))
    else:
        print("🕊️ 특이사항 없음")

except Exception as e:
    print(f"❌ 오류: {e}")
    exit(1)
