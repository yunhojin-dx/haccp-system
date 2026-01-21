import os
import tinytuya
import time
from datetime import datetime
import pytz
from supabase import create_client

# ---------------------------------------------------------
# [1] 비밀번호 금고(Secrets)에서 열쇠 꺼내기
# ---------------------------------------------------------
API_KEY = os.environ.get("TUYA_API_KEY")
API_SECRET = os.environ.get("TUYA_API_SECRET")
REGION = "us" # 한국/미국 계정 공통

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ---------------------------------------------------------
# [2] 10개 센서 리스트 (공장장님 장비 ID 그대로 적용)
# ---------------------------------------------------------
SENSORS = [
    {"name": "1호기", "id": "ebb5a8087eed5151f182k1"},
    {"name": "2호기", "id": "ebef0c9ce87b7e7929baam"},
    {"name": "3호기", "id": "eb6b6b314e849b6078juue"},
    {"name": "4호기", "id": "eb10b12a8bbd70fa3d7j0w"},
    {"name": "5호기", "id": "eb6c369e60371c40addr3z"},
    {"name": "6호기", "id": "eba9084fba86a454cbflqo"},
    {"name": "7호기", "id": "eb525a245eaec6b9eftuse"},
    {"name": "8호기", "id": "eba906355738db4525miqb"},
    {"name": "9호기", "id": "eb32026565a040ba90opj8"},
    {"name": "10호기", "id": "ebef6f23e7c1071a83njws"},
]

print("🏭 [GitHub Action] 천안공장 온도 수집 시작...")

try:
    # DB 및 Tuya 연결 시도
    if not API_KEY or not SUPABASE_URL:
        raise Exception("비밀번호(Secrets)가 설정되지 않았습니다!")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    cloud = tinytuya.Cloud(apiRegion=REGION, apiKey=API_KEY, apiSecret=API_SECRET)
    
    # 한국 시간 구하기
    kst = pytz.timezone('Asia/Seoul')
    current_time_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S%z")

    success_count = 0

    # 10개 센서 순회
    for sensor in SENSORS:
        # DP Mode 데이터 조회
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
        
        # 데이터가 정상이면 저장
        if temp != -999:
            status = "정상"
            if temp > 30: status = "고온경보"
            
            print(f"✅ {sensor['name']} : {temp}℃ / {humid}% -> 저장")
            
            data = {
                "place": sensor['name'],
                "temperature": temp,
                "humidity": humid,
                "status": status,
                "created_at": current_time_str
            }
            supabase.table("sensor_logs").insert(data).execute()
            success_count += 1
        else:
            print(f"⚠️ {sensor['name']} : 데이터 수신 실패 (Offline?)")
            
            # 실패해도 기록을 남기고 싶다면 아래 주석 해제
            # supabase.table("sensor_logs").insert({
            #     "place": sensor['name'],
            #     "status": "통신오류",
            #     "created_at": current_time_str
            # }).execute()

    print(f"🎉 총 {success_count}개소 데이터 저장 완료!")

except Exception as e:
    print(f"❌ 치명적 오류 발생: {e}")
    exit(1) # GitHub에게 에러 알림
