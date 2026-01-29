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

# [기본 목록]
# 여기 적힌 'place'는 DB 연결 안 될 때만 쓰는 '비상용 명찰'입니다.
# DB 연결되면 무조건 DB에 있는 이름으로 덮어씁니다.
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
    if not DISCORD_WEBHOOK_URL: 
        print("❌ 디스코드 주소 없음")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message, "username": "천안공장 상황실"})
        print("📢 디스코드 전송 성공")
    except Exception as e: 
        print(f"❌ 디스코드 전송 실패: {e}")

# =======================================================
# [2] 메인 로직 (테스트 모드)
# =======================================================
print("🏭 [강제 알림 모드] 비정상이면 무조건 알림을 보냅니다.")

try:
    if not API_KEY or not SUPABASE_URL:
        raise Exception("환경변수(Secrets)가 설정되지 않았습니다.")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    # -----------------------------------------------------------
    # 1. DB에서 최신 설정 가져오기 (여기가 핵심!)
    # -----------------------------------------------------------
    current_mapping = {}
    current_limits = {}

    # (1) 위치 정보 로드
    try:
        res_map = supabase.table("sensor_mapping").select("*").execute()
        if res_map.data:
            current_mapping = {item['sensor_id']: item['room_name'] for item in res_map.data}
            print(f"✅ 위치 매핑 로드 성공 ({len(current_mapping)}개)")
            print(f"   👉 매핑 데이터: {current_mapping}") 
    except:
        print("⚠️ 위치 정보 로드 실패 (기본값 사용)")

    # (2) 온도 기준 로드
    try:
        res_set = supabase.table("room_settings").select("*").execute()
        if res_set.data:
            for item in res_set.data:
                current_limits[item['room_name']] = (float(item['min_temp']), float(item['max_temp']))
            print(f"✅ 온도 기준 로드 성공 ({len(current_limits)}개)")
    except:
        print("⚠️ 온도 기준 로드 실패 (기본값 사용)")

    # -----------------------------------------------------------

    kst = pytz.timezone('Asia/Seoul')
    now_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S%z")
    alert_messages = []
    
    for sensor in SENSORS_BASE:
        # ★ [중요] DB에 설정된 이름이 있으면 그걸 쓰고, 없으면 기본값(place) 사용
        # 여기서 공장장님이 앱에서 바꾼 이름이 적용됩니다.
        real_place_name = current_mapping.get(sensor['name'], sensor['place'])
        
        # Tuya 데이터 수집
        uri = f'/v1.0/devices/{sensor["id"]}/status'
        res = cloud.cloudrequest(uri)
        
        temp = -999
        humid = -999
        
        if res and 'result' in res:
            for item in res['result']:
                if item['code'] == 'temp_current':
                    val = float(item['value'])
                    temp = val / 10.0 if val > 40 else val
                elif item['code'] == 'humidity_value':
                    val = float(item['value'])
                    humid = val / 10.0 if val > 100 else val
        
        if temp != -999:
            # 해당 장소의 온도 기준 가져오기 (DB값)
            min_v, max_v = current_limits.get(real_place_name, DEFAULT_ALARM_CONFIG["default"])
            
            # 상태 판단
            current_status = "비정상" if (temp < min_v or temp > max_v) else "정상"
            
            # DB 저장
            supabase.table("sensor_logs").insert({
                "place": sensor['name'], 
                "temperature": temp, 
                "humidity": humid,
                "status": current_status, 
                "created_at": now_str, 
                "room_name": real_place_name
            }).execute()

            # ★ [로그 출력] 공장장님이 눈으로 확인할 부분
            print(f"🔍 [{sensor['name']}] -> 최종위치: {real_place_name} | 온도: {temp}℃ (기준: {min_v}~{max_v}) | 상태: {current_status}")

            # ★ [알림 로직] 과거 기록 무시하고, 지금 비정상이면 무조건 보냄!
            if current_status == "비정상":
                alert_messages.append(f"🔥 **{real_place_name} ({sensor['name']}) 온도 이탈!**\n> 현재: {temp}℃ (기준: {min_v}~{max_v}℃)")

    if alert_messages:
        send_discord_alert("## 📢 강제 알림 테스트\n" + "\n".join(alert_messages))
    else:
        print("🕊️ 모든 센서가 정상 범위입니다. (알림 없음)")

except Exception as e:
    print(f"❌ 오류 발생: {e}")
    exit(1)
