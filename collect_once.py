import os
import tinytuya
import requests
import time
from datetime import datetime
import pytz
from supabase import create_client

# =======================================================
# [1] 환경변수 가져오기 (GitHub Secrets)
# =======================================================
# Tuya(센서) 정보
API_KEY = os.environ.get("TUYA_API_KEY")
API_SECRET = os.environ.get("TUYA_API_SECRET")
REGION = "us"

# Supabase(DB) 정보
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ★ 디스코드 웹훅 주소 (가장 중요!)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# =======================================================
# [2] 설정: 센서 목록 & 정상 온도 범위
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

# 장소별 정상 온도 범위 (최소, 최대)
ALARM_CONFIG = {
    "쌀창고": (5.0, 25.0),
    "전처리실": (10.0, 30.0),
    "양조실": (20.0, 28.0),
    "제품포장실": (10.0, 30.0),
    "부자재창고": (0.0, 40.0),
    "default": (0.0, 35.0)
}

# =======================================================
# [3] 디스코드 알림 발송 함수
# =======================================================
def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ 디스코드 주소(Secrets)가 없어서 알림을 못 보냅니다.")
        return
    
    # 디스코드에 보낼 메시지 꾸미기
    payload = {
        "content": message,
        "username": "천안공장 상황실",  # 로봇 이름
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/1035/1035689.png" # 공장 아이콘
    }
    
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        if res.status_code == 204:
            print("📨 디스코드 알림 발송 성공!")
        else:
            print(f"❌ 디스코드 발송 실패: {res.status_code} / {res.text}")
    except Exception as e:
        print(f"❌ 디스코드 오류: {e}")

# =======================================================
# [4] 메인 로직 (온도 측정 -> 판단 -> 알림 -> 저장)
# =======================================================
print(f"🏭 [GitHub Action] 온도 수집 및 경보 점검 시작...")

try:
    if not API_KEY or not SUPABASE_URL:
        raise Exception("필수 환경변수(Secrets)가 없습니다.")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S%z")
    
    alert_messages = [] # 경보 내용을 담을 바구니
    
    for sensor in SENSORS:
        # 1. 센서 데이터 가져오기
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
        
        # 2. 데이터가 정상적이면 처리 시작
        if temp != -999:
            place = sensor['place']
            min_v, max_v = ALARM_CONFIG.get(place, ALARM_CONFIG["default"])
            
            status = "정상"
            
            # 🚨 3. 경보 체크 (범위 벗어나면?)
            if temp < min_v or temp > max_v:
                status = "비정상"
                # 디스코드용 메시지 만들기 (굵게, 이모지 포함)
                msg = f"🔥 **[긴급] {place} 온도 이탈!**\n> 🌡️ 현재: **{temp}℃**\n> 📏 기준: {min_v}~{max_v}℃\n> 🤖 기기: {sensor['name']}"
                alert_messages.append(msg)
                print(f"🚨 {place} 경보 발생! ({temp}℃)")
            
            print(f"✅ {sensor['name']}({place}) : {temp}℃ -> 저장")
            
            # 4. DB에 저장
            data = {
                "place": sensor['name'], 
                "temperature": temp,
                "humidity": humid,
                "status": status,
                "created_at": current_time_str,
                "room_name": place 
            }
            supabase.table("sensor_logs").insert(data).execute()
        else:
            print(f"⚠️ {sensor['name']} : 수신 실패 (건너뜀)")

    # 5. 경보 바구니에 뭐라도 들어있으면 디스코드로 쏘기!
    if alert_messages:
        # 메시지 예쁘게 합치기
        final_msg = "## 🚨 천안공장 긴급 알림 🚨\n" + "\n".join(alert_messages) + f"\n\n🕒 {now.strftime('%H:%M:%S')}"
        send_discord_alert(final_msg)
    else:
        print("🕊️ 모든 구역 온도가 정상입니다.")

    print("🎉 모든 작업 완료!")

except Exception as e:
    print(f"❌ 치명적 오류 발생: {e}")
    # 시스템이 뻗었을 때도 알려주기
    send_discord_alert(f"⚠️ **[시스템 오류]** 수집 로봇 작동 중 에러 발생!\n> 내용: {str(e)}")
    exit(1)
