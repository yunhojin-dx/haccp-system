import os
import tinytuya
import requests
import time
from datetime import datetime
import pytz
from supabase import create_client

# =======================================================
# [1] 환경변수 & 설정
# =======================================================
API_KEY = os.environ.get("TUYA_API_KEY")
API_SECRET = os.environ.get("TUYA_API_SECRET")
REGION = "us"
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# 기본 센서 목록 (DB 연결 실패 시 사용 + ID 정보 포함)
# ★ 이름(name)과 ID(id)는 고정, 장소(place)는 DB에서 불러와서 덮어씀
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

# 알림 기준 (기본값)
ALARM_CONFIG = {
    "부자재창고": (0.0, 40.0), # 10호기 보정 감안
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
print("🏭 [GitHub Action] 센서 수집 시작 (DB 위치 연동)...")

try:
    if not API_KEY or not SUPABASE_URL:
        raise Exception("환경변수(Secrets)가 설정되지 않았습니다.")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    # 1. DB에서 최신 위치 정보 가져오기
    mapping = {}
    try:
        res = supabase.table("sensor_mapping").select("*").execute()
        if res.data:
            mapping = {item['sensor_id']: item['room_name'] for item in res.data}
            print("✅ 최신 위치 정보를 DB에서 가져왔습니다.")
    except Exception as e:
        print(f"⚠️ 위치 정보 로드 실패 (기본값 사용): {e}")

    kst = pytz.timezone('Asia/Seoul')
    now_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S%z")
    alert_messages = []
    
    for sensor in SENSORS_BASE:
        # DB에 설정된 위치가 있으면 그걸 쓰고, 없으면 기본값 사용
        current_place = mapping.get(sensor['name'], sensor['place'])
        
        # Tuya 데이터 수집
        uri = f'/v1.0/devices/{sensor["id"]}/status'
        res = cloud.cloudrequest(uri)
        
        temp = -999
        humid = -999
        
        if res and 'result' in res:
            for item in res['result']:
                if item['code'] == 'temp_current':
                    val = float(item['value'])
                    # ★ [10호기 겨울철 버그 수정] 4.0도(40) 이상이면 10으로 나눔
                    temp = val / 10.0 if val > 40 else val
                elif item['code'] == 'humidity_value':
                    val = float(item['value'])
                    humid = val / 10.0 if val > 100 else val
        
        if temp != -999:
            min_v, max_v = ALARM_CONFIG.get(current_place, ALARM_CONFIG["default"])
            
            # 상태 판단
            current_status = "비정상" if (temp < min_v or temp > max_v) else "정상"
            
            # DB 저장 (업데이트된 장소 이름으로 저장)
            supabase.table("sensor_logs").insert({
                "place": sensor['name'], 
                "temperature": temp, 
                "humidity": humid,
                "status": current_status, 
                "created_at": now_str, 
                "room_name": current_place
            }).execute()

            # 스마트 알림
            last_log = supabase.table("sensor_logs").select("status").eq("place", sensor['name']).order("created_at", desc=True).limit(1).execute()
            prev_status = "정상"
            if last_log.data: prev_status = last_log.data[0]['status']

            if current_status == "비정상" and prev_status != "비정상":
                alert_messages.append(f"🔥 **{current_place} ({sensor['name']}) 온도 이탈!** ({temp}℃)")
            elif current_status == "정상" and prev_status == "비정상":
                alert_messages.append(f"✅ **{current_place} ({sensor['name']}) 온도 복구** ({temp}℃)")

    if alert_messages:
        send_discord_alert("## 📢 천안공장 상황 알림\n" + "\n".join(alert_messages))
    else:
        print("🕊️ 특이사항 없음")

except Exception as e:
    print(f"❌ 오류 발생: {e}")
    exit(1)
