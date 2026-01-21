import os
import tinytuya
import requests
import time
from datetime import datetime
import pytz
from supabase import create_client

# =======================================================
# [1] 환경변수 가져오기
# =======================================================
API_KEY = os.environ.get("TUYA_API_KEY")
API_SECRET = os.environ.get("TUYA_API_SECRET")
REGION = "us"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# =======================================================
# [2] 설정
# =======================================================
SENSORS = [
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

ALARM_CONFIG = {
    "쌀창고": (5.0, 25.0),
    "전처리실": (10.0, 30.0),
    "양조실": (20.0, 28.0),
    "제품포장실": (10.0, 30.0),
    "부자재창고": (0.0, 40.0),
    "default": (0.0, 35.0)
}

# =======================================================
# [3] 디스코드 알림 함수
# =======================================================
def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL:
        return
    
    payload = {
        "content": message,
        "username": "천안공장 상황실",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/1035/1035689.png"
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
    except:
        pass

# =======================================================
# [4] 메인 로직
# =======================================================
print(f"🏭 [GitHub Action] 온도 점검 시작...")

try:
    if not API_KEY or not SUPABASE_URL:
        raise Exception("환경변수 누락")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S%z")
    
    alert_messages = []
    
    for sensor in SENSORS:
        # 1. 센서 값 조회
        uri = f'/v1.0/devices/{sensor["id"]}/status'
        response = cloud.cloudrequest(uri)
        
        temp = -999
        humid = -999
        
        if response and 'result' in response:
            for item in response['result']:
                if item['code'] == 'temp_current':
                    val = float(item['value'])
                    temp = val / 10.0 if val > 100 else val
                elif item['code'] == 'humidity_value':
                    val = float(item['value'])
                    humid = val / 10.0 if val > 100 else val
        
        if temp != -999:
            place = sensor['place']
            min_v, max_v = ALARM_CONFIG.get(place, ALARM_CONFIG["default"])
            
            # 2. 현재 상태 판단
            current_status = "정상"
            if temp < min_v or temp > max_v:
                current_status = "비정상"
            
            # 3. [핵심] DB에서 '직전 상태' 조회 (가장 최근 1개)
            # 해당 센서(sensor['name'])의 가장 최신 기록을 가져옴
            last_log = supabase.table("sensor_logs")\
                .select("status")\
                .eq("place", sensor['name'])\
                .order("created_at", desc=True)\
                .limit(1)\
                .execute()
            
            prev_status = "정상" # 기록 없으면 정상으로 간주
            if last_log.data and len(last_log.data) > 0:
                prev_status = last_log.data[0]['status']
            
            # 4. 알림 여부 결정
            # (1) 신규 발생: 전에는 정상이었는데 -> 지금 비정상 (🚨 알림!)
            if current_status == "비정상" and prev_status != "비정상":
                msg = f"🔥 **[발생] {place} 온도 이탈!**\n> 🌡️ 현재: **{temp}℃**\n> 📏 기준: {min_v}~{max_v}℃\n> 🤖 기기: {sensor['name']}"
                alert_messages.append(msg)
                print(f"🚨 {place} 신규 경보 발생!")
                
            # (2) 상황 종료: 전에는 비정상이었는데 -> 지금 정상 (✅ 알림!)
            elif current_status == "정상" and prev_status == "비정상":
                msg = f"✅ **[복구] {place} 온도 정상화**\n> 🌡️ 현재: {temp}℃\n> 🤖 기기: {sensor['name']}"
                alert_messages.append(msg)
                print(f"✅ {place} 상황 종료됨.")
                
            # (3) 지속 중: 비정상 -> 비정상 (🔇 조용히)
            elif current_status == "비정상" and prev_status == "비정상":
                print(f"🔇 {place} 경보 지속 중 (알림 생략)")
            
            # 5. DB 저장
            data = {
                "place": sensor['name'], 
                "temperature": temp,
                "humidity": humid,
                "status": current_status, # 현재 상태 저장
                "created_at": current_time_str,
                "room_name": place 
            }
            supabase.table("sensor_logs").insert(data).execute()
            
        else:
            print(f"⚠️ {sensor['name']} 수신 실패")

    # 6. 알림 발송 (신규 발생 or 복구 건만 모아서)
    if alert_messages:
        final_msg = "## 📢 천안공장 상황 알림\n" + "\n".join(alert_messages) + f"\n\n🕒 {now.strftime('%H:%M:%S')}"
        send_discord_alert(final_msg)
    else:
        print("🕊️ 특이사항 없음 (또는 상태 변화 없음)")

    print("🎉 완료")

except Exception as e:
    print(f"❌ 오류: {e}")
    exit(1)
