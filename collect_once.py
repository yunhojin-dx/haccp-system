import os
from supabase import create_client

# 환경변수
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# 코드에 적힌 1호기 ID (이게 DB에 있는 ID와 똑같아야 매칭이 됩니다!)
TARGET_ID = "ebb5a8087eed5151f182k1" 

print("🕵️‍♂️ [팩트 체크] DB 설정값 조회 시작...\n")

try:
    if not SUPABASE_URL: raise Exception("환경변수 없음")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 1. 위치 이름표 (sensor_mapping) 확인
    print("--- 1. 위치 이름표 (sensor_mapping) ---")
    res_map = supabase.table("sensor_mapping").select("*").execute()
    
    if not res_map.data:
        print("❌ DB에 데이터가 하나도 없습니다! (앱에서 저장이 안 된 것)")
    else:
        found = False
        for item in res_map.data:
            print(f"   📄 DB 기록: ID[{item['sensor_id']}] -> 이름[{item['room_name']}]")
            if item['sensor_id'] == TARGET_ID:
                found = True
                print(f"   ✅ [매칭 성공] 1호기 ID를 찾았습니다! 이름은 '{item['room_name']}' 입니다.")
        
        if not found:
            print(f"   ⚠️ [매칭 실패] DB에 데이터는 있는데, 1호기 ID({TARGET_ID})가 없습니다.")
            print("   👉 앱에서 1호기의 ID가 정확한지 확인해주세요.")

    print("\n")

    # 2. 온도 기준 (room_settings) 확인
    print("--- 2. 온도 기준 (room_settings) ---")
    res_set = supabase.table("room_settings").select("*").execute()
    
    if not res_set.data:
        print("❌ DB에 온도 기준이 하나도 없습니다!")
    else:
        for item in res_set.data:
            print(f"   🌡️ 장소[{item['room_name']}] : {item['min_temp']}도 ~ {item['max_temp']}도")

except Exception as e:
    print(f"❌ 오류 발생: {e}")
