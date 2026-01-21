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

# ★ [테스트용] 10도로 낮춰서 무조건 걸리게 함 (퇴근 전 35.0 복구 필수!)
ALARM_CONFIG = {
    "default": (0.0, 10.0)
}

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

# =======================================================
# [2] ★여기가 핵심★ 수다쟁이 알림 함수
# =======================================================
def send_discord_alert(message):
    print("\n----- [🕵️‍♂️ 디스코드 전송 정밀 진단] -----")
    
    # 1. 주소 있는지 확인
    if not DISCORD_WEBHOOK_URL:
        print("❌ [치명적 오류] 웹훅 주소가 없습니다! (None)")
        print("   👉 원인 1: GitHub Secrets에 'DISCORD_WEBHOOK_URL' 이름 오타")
        print("   👉 원인 2: YAML 파일 env 설정 실수")
        return

    print(f"🔑 주소 확인됨: {DISCORD_WEBHOOK_URL[:20]}... (정상)")
    
    payload = {
        "content": message,
        "username": "천안공장 상황실",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/1035/1035689.png"
    }
    
    try:
        # 2. 실제 전송 시도
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        
        # 3. 결과 브리핑
        if res.status_code == 204:
            print("✅ [성공] 디스코드 서버가 '잘 받았다'고 응답함 (204 OK)")
        else:
            print(f"❌ [거절] 디스코드 서버가 거부함! 상태코드: {res.status_code}")
            print(f"📝 거절 사유: {res.text}")
            
    except Exception as e:
        print(f"🔥 [폭발] 전송 도중 에러 발생: {e}")
        
    print("------------------------------------------\n")

# =======================================================
# [3] 메인 로직
# =======================================================
print("🚀 [진단 모드] 수집 시작...")

try:
    if not API_KEY or not SUPABASE_URL:
        print("❌ 필수 키(Tuya/Supabase)가 없습니다.")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    alert_messages = []
    
    for sensor in SENSORS:
        uri = f'/v1.0/devices/{sensor["id"]}/status'
        res = cloud.cloudrequest(uri)
        
        temp = -999
        if res and 'result' in res:
            for item in res['result']:
                if item['code'] == 'temp_current':
                    val = float(item['value'])
                    temp = val / 10.0 if val > 100 else val
        
        if temp != -999:
            place = sensor['place']
            # 무조건 걸리게 테스트 설정 사용
            min_v, max_v = ALARM_CONFIG["default"]
            
            # 기준 이탈 시 메시지 담기
            if temp < min_v or temp > max_v:
                print(f"🚨 {place} 경보 감지! (메시지 바구니에 담음)")
                msg = f"🔥 [TEST] {place} {temp}℃"
                alert_messages.append(msg)
            
            # DB 저장 (에러 방지용)
            data = {"place": sensor['name'], "temperature": temp, "status": "테스트", "created_at": datetime.now(pytz.timezone('Asia/Seoul')).strftime("%Y-%m-%d %H:%M:%S%z"), "room_name": place}
            supabase.table("sensor_logs").insert(data).execute()

    # 메시지가 있으면 발송 시도
    if alert_messages:
        print(f"📢 총 {len(alert_messages)}건의 경보를 발송합니다.")
        final_msg = "## 🕵️‍♂️ 범인 색출 테스트\n" + "\n".join(alert_messages)
        send_discord_alert(final_msg)
    else:
        print("❓ 이상하네요, 경보가 하나도 안 잡혔나요?")

except Exception as e:
    print(f"❌ 전체 시스템 오류: {e}")
